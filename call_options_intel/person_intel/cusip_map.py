"""
cusip_map.py
============
Goal D — map a 13F CUSIP (or issuer name) to a ticker WITH a confidence and a
source, and never invent a match. Every result carries:

    mapped_ticker | confidence (0..1) | mapping_source | needs_human_review

Resolution order:
  1. explicit curated map (``config/cusip_map.yml``) — highest confidence,
     cross-checked against the CUSIP check digit;
  2. issuer-name heuristic — medium confidence, ALWAYS ``needs_human_review``
     because name matching is fuzzy;
  3. nothing — ``mapped_ticker=None``, confidence 0.0, ``needs_human_review``.

A CUSIP whose check digit fails is never silently trusted — it is downgraded and
flagged. There is no "match without confidence".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("coi.person.cusip")

_CUSIP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"


@dataclass
class MappingResult:
    cusip: str
    mapped_ticker: Optional[str]
    confidence: float
    mapping_source: str            # explicit_map | name_heuristic | none
    needs_human_review: bool
    reasons: list[str] = field(default_factory=list)


def _char_value(ch: str) -> int:
    return _CUSIP_ALPHABET.index(ch.upper())


def cusip_check_digit(cusip8: str) -> Optional[int]:
    """Compute the CUSIP check digit (modulus-10 double-add-double)."""
    if len(cusip8) < 8:
        return None
    total = 0
    for i, ch in enumerate(cusip8[:8]):
        if ch.upper() not in _CUSIP_ALPHABET:
            return None
        v = _char_value(ch)
        if i % 2 == 1:          # even position (1-indexed) -> double
            v *= 2
        total += v // 10 + v % 10
    return (10 - (total % 10)) % 10


def is_valid_cusip(cusip: str) -> bool:
    cusip = (cusip or "").strip()
    if len(cusip) != 9 or not cusip[-1].isdigit():
        return False
    expected = cusip_check_digit(cusip[:8])
    return expected is not None and expected == int(cusip[-1])


class CusipMapper:
    def __init__(self, explicit_map: dict[str, dict] | None = None,
                 name_hints: dict[str, str] | None = None,
                 mapping_floor: float = 0.6):
        # explicit_map: CUSIP(9) -> {ticker, source, confidence?}
        self.explicit = {self._norm(k): v for k, v in (explicit_map or {}).items()}
        # name_hints: lowercase substring -> ticker
        self.name_hints = {k.lower(): v.upper() for k, v in (name_hints or {}).items()}
        self.mapping_floor = mapping_floor

    @staticmethod
    def _norm(cusip: str) -> str:
        return (cusip or "").strip().upper()

    def map_cusip(self, cusip: str, issuer_name: str | None = None) -> MappingResult:
        cusip = self._norm(cusip)
        reasons: list[str] = []

        # ── 1. explicit curated map ─────────────────────────────────────
        if cusip in self.explicit:
            rec = self.explicit[cusip]
            ticker = str(rec.get("ticker", "")).upper() or None
            conf = float(rec.get("confidence", 0.97))
            valid = is_valid_cusip(cusip)
            if not valid:
                conf = min(conf, 0.55)
                reasons.append("CUSIP check digit INVALID — downgraded")
            reasons.append(f"explicit map -> {ticker} ({rec.get('source', 'curated')})")
            return MappingResult(
                cusip=cusip, mapped_ticker=ticker, confidence=round(conf, 2),
                mapping_source="explicit_map",
                needs_human_review=(conf < self.mapping_floor or ticker is None),
                reasons=reasons)

        # ── 2. issuer-name heuristic (fuzzy -> always human review) ──────
        if issuer_name:
            low = issuer_name.lower()
            for hint, ticker in self.name_hints.items():
                if hint in low:
                    reasons.append(
                        f"name heuristic '{hint}' -> {ticker} (fuzzy, unverified)")
                    return MappingResult(
                        cusip=cusip, mapped_ticker=ticker, confidence=0.5,
                        mapping_source="name_heuristic", needs_human_review=True,
                        reasons=reasons)

        # ── 3. no match — never guess ───────────────────────────────────
        if cusip and not is_valid_cusip(cusip):
            reasons.append("CUSIP check digit invalid and not in map")
        else:
            reasons.append("no curated or heuristic match")
        return MappingResult(
            cusip=cusip, mapped_ticker=None, confidence=0.0,
            mapping_source="none", needs_human_review=True, reasons=reasons)


def load_mapper(config_dir: str | Path | None = None,
                mapping_floor: float = 0.6) -> CusipMapper:
    """Build a :class:`CusipMapper` from ``config/cusip_map.yml``."""
    from ..config_loader import load_yaml
    cfg = load_yaml("cusip_map", Path(config_dir) if config_dir else None)
    explicit = cfg.get("cusips", {}) or {}
    hints = cfg.get("name_hints", {}) or {}
    floor = float(cfg.get("mapping_floor", mapping_floor))
    return CusipMapper(explicit_map=explicit, name_hints=hints, mapping_floor=floor)
