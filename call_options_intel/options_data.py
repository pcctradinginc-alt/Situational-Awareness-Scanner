"""
options_data.py
===============
Free options-chain adapter with graceful fallback. OFFLINE mode reads a bundled
fixture whose contracts carry a RELATIVE `dte` (days-to-expiry offset) so expiry
dates stay valid no matter when the scan runs. LIVE mode uses yfinance option
chains. Missing greeks (delta) are estimated via Black-Scholes and flagged.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from . import blackscholes as bs
from .models import OptionContract

logger = logging.getLogger("coi.options")

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES = PKG_DIR / "fixtures"


class OptionsDataProvider:
    def __init__(self, cfg: dict, mode: str = "offline",
                 fixtures_dir: str | Path | None = None,
                 today: date | None = None):
        self.cfg = cfg or {}
        self.mode = mode
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
        self.today = today or date.today()
        self._fixture_cache: dict | None = None
        opt = self.cfg.get("options_data", {}) if isinstance(self.cfg, dict) else {}
        self.estimate_greeks = opt.get("estimate_missing_greeks", True)
        self.risk_free = float(opt.get("risk_free_rate", 0.04))

    # ── public ─────────────────────────────────────────────────────────
    def get_call_contracts(
        self, ticker: str, spot: float | None, hist_vol: float | None = None
    ) -> list[OptionContract]:
        if self.mode == "live":
            contracts = self._live_contracts(ticker, spot)
            if contracts:
                return self._post_process(contracts, spot, hist_vol)
        return self._post_process(self._fixture_contracts(ticker, spot), spot, hist_vol)

    # ── post-processing: greeks estimation + spot stamping ──────────────
    def _post_process(self, contracts, spot, hist_vol):
        out: list[OptionContract] = []
        for c in contracts:
            if spot is not None:
                c.spot = spot
            if c.delta is None and self.estimate_greeks and spot:
                sigma = c.iv or hist_vol
                if sigma and c.dte > 0:
                    t = c.dte / 365.0
                    c.delta = round(
                        bs.call_delta(spot, c.strike, t, self.risk_free, sigma), 4)
                    c.delta_estimated = True
            out.append(c)
        return out

    # ── live ────────────────────────────────────────────────────────────
    def _live_contracts(self, ticker, spot):  # pragma: no cover - network
        try:
            import yfinance as yf
        except Exception as exc:
            logger.warning("yfinance unavailable: %s", exc)
            return []
        try:
            tk = yf.Ticker(ticker)
            expiries = list(tk.options or [])
        except Exception as exc:
            logger.warning("Options expiries fetch failed for %s: %s", ticker, exc)
            return []
        contracts: list[OptionContract] = []
        for exp in expiries:
            try:
                dte = (datetime.strptime(exp, "%Y-%m-%d").date() - self.today).days
                if dte < 1:
                    continue
                chain = tk.option_chain(exp)
                for _, row in chain.calls.iterrows():
                    contracts.append(OptionContract(
                        ticker=ticker, expiry=exp, strike=float(row["strike"]),
                        dte=dte, bid=_f(row.get("bid")), ask=_f(row.get("ask")),
                        last=_f(row.get("lastPrice")),
                        iv=_f(row.get("impliedVolatility")),
                        open_interest=_i(row.get("openInterest")),
                        volume=_i(row.get("volume")),
                    ))
            except Exception as exc:
                logger.warning("Chain fetch failed %s %s: %s", ticker, exp, exc)
        return contracts

    # ── fixture ──────────────────────────────────────────────────────────
    def _load_fixtures(self) -> dict:
        if self._fixture_cache is not None:
            return self._fixture_cache
        path = self.fixtures_dir / "options" / "options_fixture.json"
        try:
            self._fixture_cache = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Options fixture missing/invalid (%s): %s", path, exc)
            self._fixture_cache = {}
        return self._fixture_cache

    def _fixture_contracts(self, ticker, spot):
        rows = self._load_fixtures().get(ticker.upper(), [])
        contracts: list[OptionContract] = []
        for r in rows:
            dte = int(r.get("dte", 0))
            if dte <= 0:
                continue
            expiry = (self.today + timedelta(days=dte)).isoformat()
            strike = r.get("strike")
            # support relative strike via moneyness if absolute strike absent
            if strike is None and spot and r.get("moneyness"):
                strike = round(spot * float(r["moneyness"]), 2)
            if strike is None:
                continue
            contracts.append(OptionContract(
                ticker=ticker.upper(), expiry=expiry, strike=float(strike), dte=dte,
                bid=_f(r.get("bid")), ask=_f(r.get("ask")), last=_f(r.get("last")),
                iv=_f(r.get("iv")), open_interest=_i(r.get("open_interest")),
                volume=_i(r.get("volume")), delta=_f(r.get("delta")),
            ))
        return contracts


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None
