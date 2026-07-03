"""
data_health.py
==============
Live data-quality monitoring. **Data errors often look like opportunities** — a
stale option chain, an empty dataframe, a mis-resolved ticker or a zero-volume
quote can masquerade as a tradeable edge. This turns those into explicit ANOMALY
alerts so they are surfaced and investigated, never silently sized.

It reads what the run already computed (the per-candidate EV decision, which
records exactly why a snapshot was unusable) rather than re-fetching, so it is
free and deterministic. A resolved ticker that could not be priced is the loudest
signal: it is a *pipeline* failure, not a no-trade. Research/paper only.
"""

from __future__ import annotations

from typing import Optional


# EV hard-fail tokens / reason fragments that indicate DATA problems (not a
# legitimate "no edge" verdict). Liquidity/earnings/EV failures are real verdicts
# and are intentionally NOT treated as data anomalies.
_DATA_KINDS = {
    "no_snapshot": ("stale_or_empty_chain",
                    "resolved ticker but NO option snapshot returned — "
                    "stale / empty chain"),
}
_REASON_FRAGMENTS = (
    ("incomplete snapshot", "incomplete_snapshot"),
    ("EV not computable", "uncomputable_snapshot"),
)


def assess_data_health(items: list[dict]) -> dict:
    """``items``: ``[{"ticker": str, "ev": <ev decision dict>}]`` (typically the
    run's trade-candidates). Returns ``{ok, n_checked, anomalies[], note}``."""
    anomalies: list[dict] = []
    for it in items or []:
        tkr = it.get("ticker") or "?"
        ev = it.get("ev") or {}
        hard_fails = set(ev.get("hard_fails", []) or [])
        reasons = ev.get("reasons", []) or []

        for token, (kind, detail) in _DATA_KINDS.items():
            if token in hard_fails:
                anomalies.append({"ticker": tkr, "kind": kind, "detail": detail})

        for frag, kind in _REASON_FRAGMENTS:
            hit = next((r for r in reasons if frag in r), None)
            if hit:
                anomalies.append({"ticker": tkr, "kind": kind, "detail": hit})

    return {
        "ok": not anomalies,
        "n_checked": len(items or []),
        "anomalies": anomalies,
        "note": ("Data errors can look like opportunities — a resolved ticker that "
                 "cannot be priced is a PIPELINE failure, flagged here, never sized."),
    }
