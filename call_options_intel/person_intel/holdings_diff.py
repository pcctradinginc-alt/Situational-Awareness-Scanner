"""
holdings_diff.py
================
Turn a NEW 13F-HR from a tracked manager into an actual **position-level diff**
(new / add / trim / exit per name) instead of a bare "a 13F was filed" event.

It diffs two 13F information tables at the **CUSIP** level (more robust than a
name heuristic), reusing the typed parser (`parse_13f_infotable_typed`, which
keeps Common vs CALL/PUT/ADR/ETF and a conservative direction) and the person
**CUSIP→ticker** mapper (confidence + `needs_human_review`, never a guess).

Honesty rules carried through:
  * a CALL/PUT 13F line is `direction_unknown` — counted, but flagged, never read
    as naive bullishness;
  * a CUSIP that does not map with confidence stays `needs_human_review` (the
    change is still reported, but its ticker is not asserted);
  * the live document navigation is best-effort and degrades to an event-level
    alert when an information table cannot be located.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .cusip_map import CusipMapper
from .filings import (
    Direction, InstrumentType, Typed13FRow, parse_13f_infotable_typed,
)

logger = logging.getLogger("coi.person.holdings_diff")

# share-change thresholds: below this q/q move is "hold", not add/trim
_MOVE_FLOOR = 0.02


@dataclass
class HoldingAgg:
    cusip: str
    issuer_name: str
    shares: float = 0.0
    value: float = 0.0
    has_option_leg: bool = False        # any CALL/PUT line on this name
    has_common_leg: bool = False


@dataclass
class PositionChange:
    cusip: str
    issuer_name: str
    ticker: Optional[str]
    mapping_confidence: float
    needs_human_review: bool
    action: str                          # new | add | trim | exit | hold
    pct_change: Optional[float]          # q/q share change (None when unknowable)
    portfolio_pct: Optional[float]       # share of current 13F value
    instrument_note: str = ""            # e.g. "includes CALL/PUT leg (direction_unknown)"

    def label(self) -> str:
        t = self.ticker or f"{self.issuer_name[:24]} (CUSIP {self.cusip})"
        flag = " ⚠review" if self.needs_human_review else ""
        pc = "" if self.pct_change is None else f" {self.pct_change:+.0%}"
        return f"{self.action.upper()} {t}{pc}{flag}"


def _aggregate(rows: list[Typed13FRow]) -> dict[str, HoldingAgg]:
    """Aggregate typed rows by CUSIP, summing share/value and noting option legs."""
    out: dict[str, HoldingAgg] = {}
    for r in rows:
        cusip = (r.cusip or "").strip().upper()
        if not cusip:
            continue
        agg = out.get(cusip)
        if agg is None:
            agg = HoldingAgg(cusip=cusip, issuer_name=r.issuer_name)
            out[cusip] = agg
        if r.shares:
            agg.shares += r.shares
        if r.value:
            agg.value += r.value
        if r.instrument in (InstrumentType.CALL, InstrumentType.PUT):
            agg.has_option_leg = True
        else:
            agg.has_common_leg = True
    return out


def _action(cur: Optional[HoldingAgg], prior: Optional[HoldingAgg]
            ) -> tuple[str, Optional[float]]:
    if cur is not None and prior is None:
        return "new", None
    if cur is None and prior is not None:
        return "exit", -1.0
    if cur is None or prior is None:                 # unreachable, keeps type-checker calm
        return "hold", None
    if prior.shares and cur.shares is not None:
        pc = (cur.shares - prior.shares) / prior.shares
        if pc > _MOVE_FLOOR:
            return "add", pc
        if pc < -_MOVE_FLOOR:
            return "trim", pc
        return "hold", pc
    return "hold", None


def diff_infotables(current_xml: str, prior_xml: str, mapper: CusipMapper
                    ) -> list[PositionChange]:
    """Diff two 13F information tables at CUSIP level → typed PositionChanges."""
    cur = _aggregate(parse_13f_infotable_typed(current_xml or ""))
    pre = _aggregate(parse_13f_infotable_typed(prior_xml or ""))
    total_val = sum(a.value for a in cur.values()) or 0.0

    changes: list[PositionChange] = []
    for cusip in set(cur) | set(pre):
        c, p = cur.get(cusip), pre.get(cusip)
        action, pc = _action(c, p)
        if action == "hold":
            continue                                  # only surface real moves
        ref = c or p
        res = mapper.map_cusip(cusip, ref.issuer_name)
        ppct = (c.value / total_val) if (c and total_val) else None
        note = ""
        if ref.has_option_leg and not (ref.has_common_leg):
            note = "OPTION-only line — 13F cannot confirm direction (direction_unknown)"
        elif ref.has_option_leg:
            note = "includes a CALL/PUT leg (direction_unknown)"
        changes.append(PositionChange(
            cusip=cusip, issuer_name=ref.issuer_name, ticker=res.mapped_ticker,
            mapping_confidence=round(res.confidence, 2),
            needs_human_review=res.needs_human_review,
            action=action, pct_change=pc, portfolio_pct=ppct,
            instrument_note=note))

    # most signal-bearing first: new > exit > add > trim, then bigger move
    rank = {"new": 0, "exit": 1, "add": 2, "trim": 3}
    changes.sort(key=lambda x: (rank.get(x.action, 9),
                                -(abs(x.pct_change) if x.pct_change else 0)))
    return changes


def summarize_changes(changes: list[PositionChange], top: int = 6) -> str:
    """One-line human summary of the most signal-bearing moves."""
    if not changes:
        return ""
    return " · ".join(c.label() for c in changes[:top])


# ── live document navigation (best-effort) ──────────────────────────────────
def _accession_index_url(cik_int: str, accession: str) -> str:
    folder = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{folder}/index.json"


def fetch_infotable_xml(fetcher, cik: str, accession: str) -> Optional[str]:
    """Locate and fetch a 13F filing's information-table XML via its index.json.

    Best-effort and offline-safe: returns None if the index or a plausible
    info-table document cannot be found (caller then keeps an event-level alert).
    """
    cik_int = str(int(str(cik).strip().lstrip("0") or "0"))
    raw = fetcher.get(_accession_index_url(cik_int, accession))
    if not raw:
        return None
    try:
        idx = json.loads(raw)
    except json.JSONDecodeError:
        return None
    items = (idx.get("directory", {}) or {}).get("item", []) or []
    # prefer a file whose name signals the holdings table; avoid the cover page
    candidates = []
    for it in items:
        name = str(it.get("name", ""))           # preserve case — SEC paths are case-sensitive
        low = name.lower()
        if not low.endswith(".xml"):
            continue
        if "primary_doc" in low:
            continue
        score = 0
        if "infotable" in low or "information" in low or "form13f" in low:
            score = 2
        elif "table" in low:
            score = 1
        candidates.append((score, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0][1]
    folder = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{folder}/{best}"
    return fetcher.get(url)
