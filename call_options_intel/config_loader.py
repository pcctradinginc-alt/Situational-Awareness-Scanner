"""
config_loader.py
================
Loads the YAML configuration files with sane built-in defaults. Missing files
or missing keys never crash: defaults fill the gaps and a warning flag is
recorded. This keeps the pipeline runnable out-of-the-box.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import yaml
    _HAVE_YAML = True
except Exception:  # pragma: no cover - yaml is a hard dep but degrade anyway
    _HAVE_YAML = False

logger = logging.getLogger("coi.config")

# Repo root = parent of the package directory.
PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
CONFIG_DIR = REPO_ROOT / "config"


_DEFAULTS: dict[str, dict] = {
    "scoring_weights": {
        "weights": {
            "thesis": 0.28, "thirteen_f": 0.18, "market": 0.22,
            "options": 0.18, "catalyst": 0.14,
        },
        "risk_penalty": {
            "max_total_penalty": 4.0, "illiquid_options": 1.0, "wide_spread": 1.0,
            "extreme_iv": 0.8, "near_expiry_lottery": 0.8, "weak_data_quality": 1.0,
            "no_catalyst": 0.5, "contradictory_13f": 0.7, "excessive_drawdown": 0.6,
            "event_iv_risk": 0.7,
        },
        "confidence": {"labels": {"high": 0.80, "medium": 0.55, "low": 0.0}},
        "gates": {
            "top_candidate_min_score": 6.5, "watchlist_min_score": 4.5,
            "reject_below_score": 0.0,
        },
    },
    "risk_thresholds": {
        "dte": {"min": 30, "max": 200, "preferred_min": 60, "preferred_max": 180},
        "strike_zones": {
            "near_the_money": [0.97, 1.03], "slightly_otm": [1.03, 1.12],
            "longer_dated_otm": [1.10, 1.30],
        },
        "liquidity": {
            "min_open_interest": 50, "min_volume": 5, "max_bid_ask_spread_pct": 0.18,
        },
        "iv": {
            "expensive_percentile": 80, "extreme_percentile": 92,
            "warmup_default_percentile": 50,
            "richness_to_percentile_slope": 100, "fair_richness_max": 1.15,
        },
        "market": {
            "max_drawdown_without_stabilization": 0.35, "min_avg_dollar_volume": 5.0e7,
        },
        "earnings": {"flag_window_days": 10},
        "data_quality": {"min_acceptable": 0.30, "low_confidence_below": 0.55},
    },
    "data_sources": {
        "mode": "offline",
        "market_data": {"providers": ["yfinance", "stooq", "fixture"],
                        "lookback_days": 260, "request_timeout_s": 10},
        "options_data": {"providers": ["yfinance", "fixture"], "request_timeout_s": 10,
                         "estimate_missing_greeks": True, "risk_free_rate": 0.04},
        "volatility": {"vix_symbol": "^VIX", "fixture_vix": 17.5},
        "edgar_13f": {"user_agent": "SA-Scanner research (set-your-email@example.com)",
                      "base_url": "https://data.sec.gov", "request_timeout_s": 15,
                      "rate_limit_per_sec": 5},
        "news_rss": {"enabled": False, "feeds": []},
        "earnings_calendar": {"provider": "yfinance"},
    },
    "investors_13f": {
        "managers": [],
        "scoring": {
            "new_position_bonus": 2.0, "add_bonus_per_pct_change": 0.04,
            "add_bonus_cap": 3.0, "reduce_penalty_per_pct_change": 0.03,
            "exit_penalty": 2.5, "concentration_weight": 2.0,
            "persistence_bonus_per_quarter": 0.3, "persistence_cap": 1.5,
        },
    },
    "report_settings": {
        "report": {
            "title": "AI Infrastructure CALL-Options Intelligence",
            "default_formats": ["markdown", "csv", "json"],
            "output_dir": "reports", "top_candidates": 10, "watchlist_size": 15,
            "rejected_sample": 20,
            "disclaimers": [
                "RESEARCH / PAPER MODE ONLY. This system never executes trades.",
                "Not investment advice. Options can expire worthless (100% loss).",
            ],
            "always_show_risk": True, "always_show_data_quality": True,
            "paper_trading_recommendation": True,
        },
    },
    "ai_infra_universe": {"meta": {}, "categories": [], "tickers": []},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    """Load `<name>.yml` merged over built-in defaults. Never raises."""
    cdir = Path(config_dir) if config_dir else CONFIG_DIR
    default = _DEFAULTS.get(name, {})
    path = cdir / f"{name}.yml"
    if not _HAVE_YAML:
        logger.warning("PyYAML unavailable — using built-in defaults for %s", name)
        return dict(default)
    if not path.exists():
        logger.warning("Config %s not found at %s — using defaults", name, path)
        return dict(default)
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            logger.warning("Config %s is not a mapping — using defaults", name)
            return dict(default)
        return _deep_merge(default, loaded)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to parse %s (%s) — using defaults", path, exc)
        return dict(default)


class AppConfig:
    """Container that lazily loads and caches all config files."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self.universe = load_yaml("ai_infra_universe", self.config_dir)
        self.scoring = load_yaml("scoring_weights", self.config_dir)
        self.risk = load_yaml("risk_thresholds", self.config_dir)
        self.data_sources = load_yaml("data_sources", self.config_dir)
        self.investors = load_yaml("investors_13f", self.config_dir)
        self.report = load_yaml("report_settings", self.config_dir)

    @property
    def mode(self) -> str:
        return self.data_sources.get("mode", "offline")

    def validate(self) -> list[str]:
        """Return a list of human-readable config warnings (never raises)."""
        warnings: list[str] = []
        w = self.scoring.get("weights", {})
        if not w:
            warnings.append("scoring_weights.weights is empty")
        if not self.universe.get("tickers"):
            warnings.append("ai_infra_universe has no tickers")
        dte = self.risk.get("dte", {})
        if dte.get("min", 0) >= dte.get("max", 0):
            warnings.append("risk_thresholds.dte min >= max")
        return warnings
