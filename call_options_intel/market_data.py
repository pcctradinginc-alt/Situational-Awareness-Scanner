"""
market_data.py
==============
Free market-data adapter with graceful fallback. Default OFFLINE mode reads a
bundled snapshot fixture so the pipeline is deterministic and network-free. LIVE
mode tries free providers in order (yfinance -> stooq) and falls back to the
fixture, then to an empty-but-flagged snapshot. Never raises on data problems.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import MarketSnapshot

logger = logging.getLogger("coi.market")

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES = PKG_DIR / "fixtures"


# ── pure metric computation (unit-tested directly) ─────────────────────────
def compute_snapshot_from_closes(
    ticker: str, closes: list[float], volumes: list[float] | None = None
) -> MarketSnapshot:
    """Derive a MarketSnapshot from a close-price series (oldest -> newest)."""
    snap = MarketSnapshot(ticker=ticker)
    closes = [float(c) for c in closes if c is not None]
    if len(closes) < 2:
        snap.data_flags.append("insufficient_price_history")
        if closes:
            snap.spot = closes[-1]
        return snap

    snap.spot = closes[-1]
    snap.momentum_3m = _pct_return(closes, 63)
    snap.momentum_6m = _pct_return(closes, 126)

    if len(closes) >= 200:
        ma200 = sum(closes[-200:]) / 200.0
        snap.above_200dma = closes[-1] > ma200
    else:
        snap.data_flags.append("lt_200d_history")

    snap.rsi_14 = _rsi(closes, 14)
    peak = max(closes)
    snap.drawdown_from_high = round((closes[-1] - peak) / peak, 4) if peak else None
    # stabilizing: last close above the 10-day average (bouncing off lows)
    if len(closes) >= 10:
        ma10 = sum(closes[-10:]) / 10.0
        snap.stabilizing = closes[-1] >= ma10
    snap.hist_vol_annual = _annual_vol(closes)

    if volumes:
        vols = [float(v) for v in volumes[-20:] if v is not None]
        if vols:
            avg_vol = sum(vols) / len(vols)
            snap.avg_dollar_volume = round(avg_vol * snap.spot, 2)
    return snap


def _pct_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    past = closes[-1 - lookback]
    if past == 0:
        return None
    return round((closes[-1] - past) / past, 4)


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _annual_vol(closes: list[float]) -> float | None:
    if len(closes) < 21:
        return None
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes)) if closes[i - 1]
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round((var ** 0.5) * (252 ** 0.5), 4)


class MarketDataProvider:
    def __init__(self, cfg: dict, mode: str = "offline",
                 fixtures_dir: str | Path | None = None):
        self.cfg = cfg or {}
        self.mode = mode
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
        self._fixture_cache: dict | None = None

    # ── public ─────────────────────────────────────────────────────────
    def get_snapshot(self, ticker: str) -> MarketSnapshot:
        if self.mode == "live":
            snap = self._live_snapshot(ticker)
            if snap is not None:
                return snap
        return self._fixture_snapshot(ticker)

    def get_vix(self) -> float | None:
        vol_cfg = self.cfg.get("volatility", {}) if isinstance(self.cfg, dict) else {}
        if self.mode == "live":
            try:  # pragma: no cover - network
                import yfinance as yf
                hist = yf.Ticker(vol_cfg.get("vix_symbol", "^VIX")).history(period="5d")
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception as exc:  # pragma: no cover - network
                logger.warning("VIX live fetch failed: %s", exc)
        return vol_cfg.get("fixture_vix", 17.5)

    # ── live ────────────────────────────────────────────────────────────
    def _live_snapshot(self, ticker: str) -> MarketSnapshot | None:  # pragma: no cover
        md = self.cfg.get("market_data", {})
        lookback = int(md.get("lookback_days", 260))
        for provider in md.get("providers", ["yfinance"]):
            try:
                if provider == "yfinance":
                    closes, vols = self._yf_history(ticker, lookback)
                elif provider == "stooq":
                    closes, vols = self._stooq_history(ticker, lookback)
                else:
                    continue
                if closes and len(closes) >= 2:
                    snap = compute_snapshot_from_closes(ticker, closes, vols)
                    snap.data_flags.append(f"source:{provider}")
                    return snap
            except Exception as exc:
                logger.warning("Market provider %s failed for %s: %s",
                               provider, ticker, exc)
        return None

    def _yf_history(self, ticker, lookback):  # pragma: no cover - network
        import yfinance as yf
        days = max(lookback + 30, 300)
        hist = yf.Ticker(ticker).history(period=f"{days}d")
        if hist.empty:
            return [], []
        return list(hist["Close"]), list(hist.get("Volume", []))

    def _stooq_history(self, ticker, lookback):  # pragma: no cover - network
        import csv
        import io
        import urllib.request
        url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
        with urllib.request.urlopen(url, timeout=10) as resp:
            text = resp.read().decode("utf-8", "ignore")
        rows = list(csv.DictReader(io.StringIO(text)))
        closes = [float(r["Close"]) for r in rows if r.get("Close") not in (None, "", "N/D")]
        vols = [float(r["Volume"]) for r in rows if r.get("Volume") not in (None, "", "N/D")]
        return closes[-lookback:], vols[-lookback:]

    # ── fixture ──────────────────────────────────────────────────────────
    def _load_fixtures(self) -> dict:
        if self._fixture_cache is not None:
            return self._fixture_cache
        path = self.fixtures_dir / "market" / "market_fixture.json"
        try:
            self._fixture_cache = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Market fixture missing/invalid (%s): %s", path, exc)
            self._fixture_cache = {}
        return self._fixture_cache

    def _fixture_snapshot(self, ticker: str) -> MarketSnapshot:
        data = self._load_fixtures().get(ticker.upper())
        if not data:
            snap = MarketSnapshot(ticker=ticker)
            snap.data_flags.append("no_market_data")
            return snap
        if "closes" in data:  # raw series fixture
            snap = compute_snapshot_from_closes(
                ticker, data["closes"], data.get("volumes"))
            snap.data_flags.append("source:fixture_series")
            return snap
        # snapshot-style fixture
        snap = MarketSnapshot(
            ticker=ticker,
            spot=data.get("spot"),
            momentum_3m=data.get("momentum_3m"),
            momentum_6m=data.get("momentum_6m"),
            above_200dma=data.get("above_200dma"),
            rsi_14=data.get("rsi_14"),
            drawdown_from_high=data.get("drawdown_from_high"),
            avg_dollar_volume=data.get("avg_dollar_volume"),
            hist_vol_annual=data.get("hist_vol_annual"),
            stabilizing=data.get("stabilizing"),
        )
        snap.data_flags.append("source:fixture")
        return snap
