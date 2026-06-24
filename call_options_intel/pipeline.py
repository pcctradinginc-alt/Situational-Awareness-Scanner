"""
pipeline.py
===========
End-to-end orchestration of a scan. Pure-Python coordination layer; every stage
degrades gracefully (missing data -> flagged, never fatal). Default OFFLINE mode
runs entirely on bundled fixtures so a scan always produces a report.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .candidates import CandidateGenerator
from .config_loader import AppConfig
from .edgar_13f import ThirteenFMonitor
from .market_data import MarketDataProvider
from .models import ScanResult, ThesisVector
from .options_data import OptionsDataProvider
from .scoring import SignalEngine
from .thesis import ThesisAnalyzer
from .universe import UniverseBuilder

logger = logging.getLogger("coi.pipeline")

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES = PKG_DIR / "fixtures"


class Pipeline:
    def __init__(
        self,
        config: Optional[AppConfig] = None,
        mode: Optional[str] = None,
        fixtures_dir: str | Path | None = None,
        thesis_paths: Optional[list[str]] = None,
        today: Optional[date] = None,
    ):
        self.cfg = config or AppConfig()
        self.mode = mode or self.cfg.mode
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
        self.today = today or date.today()
        self.thesis_paths = thesis_paths

        self.universe_builder = UniverseBuilder(self.cfg.universe)
        self.thesis_analyzer = ThesisAnalyzer()
        self.market = MarketDataProvider(self.cfg.data_sources, self.mode, self.fixtures_dir)
        self.options = OptionsDataProvider(
            self.cfg.data_sources, self.mode, self.fixtures_dir, today=self.today)
        self.engine = SignalEngine(self.cfg.scoring, self.cfg.risk)
        self.candidate_gen = CandidateGenerator(self.engine, self.cfg.risk)
        self.f13_monitor = ThirteenFMonitor()

    # ── thesis ─────────────────────────────────────────────────────────
    def _build_thesis(self) -> ThesisVector:
        if self.thesis_paths:
            return self.thesis_analyzer.analyze_paths(self.thesis_paths)
        default_dir = self.fixtures_dir / "thesis"
        if default_dir.exists():
            return self.thesis_analyzer.analyze_paths([default_dir])
        return ThesisVector()

    # ── 13F ────────────────────────────────────────────────────────────
    def _build_13f(self) -> dict:
        try:
            return self.f13_monitor.run_from_fixtures(self.fixtures_dir, self.cfg.investors)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("13F monitor failed: %s", exc)
            return {}

    # ── earnings calendar ──────────────────────────────────────────────
    def _earnings_days(self) -> dict[str, int]:
        if self.mode == "live":
            return {}  # live earnings lookup is per-ticker; kept minimal/free
        import json
        path = self.fixtures_dir / "calendar" / "earnings_fixture.json"
        try:
            return {k.upper(): int(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    # ── regime ─────────────────────────────────────────────────────────
    def _regime(self) -> dict:
        vix = self.market.get_vix()
        if vix is None:
            label = "unknown"
        elif vix < 15:
            label = "calm"
        elif vix < 22:
            label = "normal"
        elif vix < 30:
            label = "elevated"
        else:
            label = "stress"
        return {"vix": round(vix, 2) if vix is not None else None, "label": label,
                "note": "calm/normal favours buying calls; elevated/stress = "
                        "richer IV, prefer spreads or wait."}

    # ── catalysts ──────────────────────────────────────────────────────
    def _catalysts(self, ticker: str, change, earnings_days: dict[str, int]) -> list[str]:
        out: list[str] = []
        ed = earnings_days.get(ticker)
        if ed is not None:
            out.append(f"earnings in ~{ed}d")
        if change is not None and change.action in ("new", "add") and change.conviction_score > 0:
            out.append(f"13F {change.action} ({change.conviction_score:+.1f})")
        return out

    # ── main scan ──────────────────────────────────────────────────────
    def scan(self, only_tickers: Optional[list[str]] = None) -> ScanResult:
        cfg_warnings = self.cfg.validate()
        universe = self.universe_builder.build()
        if only_tickers:
            universe = UniverseBuilder.filter_tickers(universe, only_tickers)

        thesis_vec = self._build_thesis()
        changes = self._build_13f()
        earnings_days = self._earnings_days()
        regime = self._regime()

        result = ScanResult(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mode=self.mode, universe_size=len(universe), regime=regime,
            thesis_vector=thesis_vec, thirteen_f_changes=changes,
            config_warnings=cfg_warnings,
        )

        dq_warnings: list[str] = []
        for entry in universe:
            snap = self.market.get_snapshot(entry.ticker)
            contracts = self.options.get_call_contracts(
                entry.ticker, snap.spot, snap.hist_vol_annual)
            change = changes.get(entry.ticker)
            catalysts = self._catalysts(entry.ticker, change, earnings_days)

            ev = self.engine.evaluate_ticker(
                entry, thesis_vec, snap, change, contracts, catalysts)
            result.evaluations.append(ev)

            if "no_market_data" in (snap.data_flags or []):
                dq_warnings.append(f"{entry.ticker}: no market data")
            if not contracts:
                dq_warnings.append(f"{entry.ticker}: no options chain")

            if ev.label == "watchlist":
                result.watchlist.append(ev)

            cands, rejected = self.candidate_gen.generate(
                ev, entry, contracts, snap.hist_vol_annual, earnings_days.get(entry.ticker))
            result.candidates.extend(cands)
            result.rejected_contracts.extend(rejected)

        result.candidates.sort(key=lambda c: c.final_score, reverse=True)
        top_min = self.engine.gates.get("top_candidate_min_score", 6.5)
        # Top list = the single best contract PER TICKER above the gate, so the
        # headline picks are diversified across names rather than 10 strikes of
        # one ticker. The full ranked list stays in `candidates` (CSV/JSON).
        best_per_ticker: dict[str, object] = {}
        for c in result.candidates:
            if c.final_score < top_min:
                continue
            if c.ticker not in best_per_ticker:
                best_per_ticker[c.ticker] = c
        top_n = self.cfg.report.get("report", {}).get("top_candidates", 10)
        result.top = sorted(best_per_ticker.values(),
                            key=lambda c: c.final_score, reverse=True)[:top_n]
        result.watchlist.sort(key=lambda e: e.breakdown.final_score, reverse=True)
        result.data_quality_warnings = dq_warnings
        logger.info("Scan complete: %d candidates, %d top, %d watchlist, %d rejected",
                    len(result.candidates), len(result.top), len(result.watchlist),
                    len(result.rejected_contracts))
        return result

    # ── explain one ticker ─────────────────────────────────────────────
    def explain(self, ticker: str):
        universe = self.universe_builder.build()
        entries = UniverseBuilder.filter_tickers(universe, [ticker])
        if not entries:
            return None
        entry = entries[0]
        thesis_vec = self._build_thesis()
        changes = self._build_13f()
        earnings_days = self._earnings_days()
        snap = self.market.get_snapshot(entry.ticker)
        contracts = self.options.get_call_contracts(
            entry.ticker, snap.spot, snap.hist_vol_annual)
        change = changes.get(entry.ticker)
        catalysts = self._catalysts(entry.ticker, change, earnings_days)
        ev = self.engine.evaluate_ticker(
            entry, thesis_vec, snap, change, contracts, catalysts)
        cands, rejected = self.candidate_gen.generate(
            ev, entry, contracts, snap.hist_vol_annual, earnings_days.get(entry.ticker))
        return {"evaluation": ev, "candidates": cands, "rejected": rejected,
                "snapshot": snap, "n_contracts": len(contracts)}
