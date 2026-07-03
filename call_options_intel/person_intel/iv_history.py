"""
iv_history.py
=============
Phase 2 — a persistent IV-history store so the IV-richness signal can graduate
from a realised-vol PROXY to a real **IV percentile**, but only after a warmup.

The base system has no IV history, so it approximates richness as IV vs realised
vol. This module records ATM IV over time (append-only JSONL) and, once enough
observations exist (warmup), reports a true IV rank. Before warmup it transparently
falls back to the realised-vol proxy — never a falsely precise percentile.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from ..scoring import iv_richness_assessment
from .fills import iv_rank_with_warmup

logger = logging.getLogger("coi.person.iv_history")


class IVHistoryStore:
    """Append-only JSONL of {date, ticker, atm_iv}."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, ticker: str, atm_iv: float,
               as_of: Optional[date] = None) -> bool:
        if atm_iv is None or atm_iv <= 0:
            return False
        row = {"date": (as_of or date.today()).isoformat(),
               "ticker": ticker.upper(), "atm_iv": round(float(atm_iv), 4),
               "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True

    def record_many(self, iv_by_ticker: dict[str, float],
                    as_of: Optional[date] = None) -> int:
        """Record at most ONE observation per (date, ticker) — repeated runs on
        the same day (e.g. a twice-daily workflow) must not skew the IV rank."""
        d = (as_of or date.today()).isoformat()
        seen = {(r.get("date"), r.get("ticker")) for r in self.load()}
        return sum(1 for t, iv in iv_by_ticker.items()
                   if (d, t.upper()) not in seen and self.record(t, iv, as_of))

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed IV row")
        return out

    def history(self, ticker: str) -> list[float]:
        t = ticker.upper()
        return [r["atm_iv"] for r in self.load()
                if r.get("ticker") == t and r.get("atm_iv") is not None]

    def rank(self, ticker: str, current_iv: Optional[float],
             warmup_min_obs: int = 20):
        return iv_rank_with_warmup(current_iv, self.history(ticker), warmup_min_obs)


def richness_band(
    current_iv: Optional[float], hist_vol: Optional[float],
    history: list[float] | None, risk_cfg: dict, warmup_min_obs: int = 20,
) -> tuple[Optional[str], Optional[float], str]:
    """Return (band, percentile_or_proxy, basis).

    Uses the real IV percentile once warmed up; otherwise delegates to the
    existing realised-vol richness proxy so behaviour is unchanged pre-warmup.
    """
    rank = iv_rank_with_warmup(current_iv, history, warmup_min_obs)
    if rank.warmed_up:
        iv_cfg = (risk_cfg or {}).get("iv", {})
        exp = iv_cfg.get("expensive_percentile", 80)
        ext = iv_cfg.get("extreme_percentile", 92)
        if rank.rank >= ext:
            band = "extreme"
        elif rank.rank >= exp:
            band = "elevated"
        elif rank.rank <= 40:
            band = "fair"
        else:
            band = "mild"
        return band, rank.rank, "iv_history"
    _, iv_pct, band = iv_richness_assessment(current_iv, hist_vol, risk_cfg)
    return band, iv_pct, "realised_vol_proxy"
