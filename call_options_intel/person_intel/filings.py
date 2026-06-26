"""
filings.py
==========
Goals B and C — differentiate filing types, classify 13F instruments by type,
and assign an honest verification status instead of treating every line as a
bullish signal.

Why this matters for tracking *people* early:
  * **13F-HR** is quarterly and lagged ~45 days — it CONFIRMS a thesis, it is not
    an early indicator.
  * **SC 13D / 13D-A** (activist >5% stake) and **Form 4** (insider trade) are
    fast (days), so they are the genuinely *early* person-signals.
  * **Form D / D-A** is a private-placement notice — an early *private-market*
    footprint that never shows up in 13F.
  * **ADV / IAPD** is an investment-adviser registration. It is **not** an EDGAR
    filing and must go through a separate adapter — it is context, not a trade.

And 13F **options** lines (CALL/PUT) are NOT read as naive direction: a 13F does
not tell you whether a call is an outright long, a hedge, a spread leg or a
covered write — so the inferred direction is ``DIRECTION_UNKNOWN``.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("coi.person.filings")


# ── filing taxonomy (B) ─────────────────────────────────────────────────────
class SignalRole(str, Enum):
    EARLY = "early"               # days-fast, leading
    CONFIRMATION = "confirmation"  # lagged, confirms an existing thesis
    CONTEXT = "context"           # background, not directly actionable


class SourceSystem(str, Enum):
    EDGAR = "edgar"
    IAPD = "iapd"                 # ADV — NOT EDGAR
    OTHER = "other"


class FilingType(str, Enum):
    FORM_13F_HR = "13F-HR"
    FORM_13F_HR_A = "13F-HR/A"
    SC_13D = "SC 13D"
    SC_13D_A = "SC 13D/A"
    SC_13G = "SC 13G"
    SC_13G_A = "SC 13G/A"
    FORM_4 = "Form 4"
    FORM_D = "Form D"
    FORM_D_A = "Form D/A"
    FORM_ADV = "ADV"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FilingMeta:
    filing_type: FilingType
    source: SourceSystem
    typical_lag_days: int          # rough reporting deadline / observed lag
    role: SignalRole
    note: str = ""

    @property
    def is_edgar(self) -> bool:
        return self.source is SourceSystem.EDGAR


# Lag values are deliberate approximations of the *reporting deadline*, not a
# promise — used only to rank timeliness (smaller = earlier signal).
FILING_META: dict[FilingType, FilingMeta] = {
    FilingType.FORM_4: FilingMeta(
        FilingType.FORM_4, SourceSystem.EDGAR, 2, SignalRole.EARLY,
        "Insider transaction; ~2 business days. Fastest person-signal."),
    FilingType.SC_13D: FilingMeta(
        FilingType.SC_13D, SourceSystem.EDGAR, 5, SignalRole.EARLY,
        "Activist >5% stake; ~5 business days (post-2024 rule)."),
    FilingType.SC_13D_A: FilingMeta(
        FilingType.SC_13D_A, SourceSystem.EDGAR, 2, SignalRole.EARLY,
        "Amendment to a 13D (material change)."),
    FilingType.FORM_D: FilingMeta(
        FilingType.FORM_D, SourceSystem.EDGAR, 15, SignalRole.EARLY,
        "Private placement notice; within 15 days of first sale. "
        "Private-market footprint not visible in 13F."),
    FilingType.FORM_D_A: FilingMeta(
        FilingType.FORM_D_A, SourceSystem.EDGAR, 15, SignalRole.EARLY,
        "Amended Form D."),
    FilingType.SC_13G: FilingMeta(
        FilingType.SC_13G, SourceSystem.EDGAR, 45, SignalRole.CONFIRMATION,
        "Passive >5% stake; slower than 13D."),
    FilingType.SC_13G_A: FilingMeta(
        FilingType.SC_13G_A, SourceSystem.EDGAR, 45, SignalRole.CONFIRMATION,
        "Amendment to a 13G."),
    FilingType.FORM_13F_HR: FilingMeta(
        FilingType.FORM_13F_HR, SourceSystem.EDGAR, 45, SignalRole.CONFIRMATION,
        "Quarterly institutional holdings; ~45-day lag. CONFIRMS, not early."),
    FilingType.FORM_13F_HR_A: FilingMeta(
        FilingType.FORM_13F_HR_A, SourceSystem.EDGAR, 45, SignalRole.CONFIRMATION,
        "Amended 13F."),
    FilingType.FORM_ADV: FilingMeta(
        FilingType.FORM_ADV, SourceSystem.IAPD, 90, SignalRole.CONTEXT,
        "Investment-adviser registration via IAPD — NOT an EDGAR filing. "
        "Use the dedicated ADV adapter; context only."),
    FilingType.UNKNOWN: FilingMeta(
        FilingType.UNKNOWN, SourceSystem.OTHER, 999, SignalRole.CONTEXT,
        "Unrecognised form."),
}


_FORM_ALIASES: dict[str, FilingType] = {
    "13f-hr": FilingType.FORM_13F_HR, "13fhr": FilingType.FORM_13F_HR,
    "13f": FilingType.FORM_13F_HR,
    "13f-hr/a": FilingType.FORM_13F_HR_A, "13f-hra": FilingType.FORM_13F_HR_A,
    "sc 13d": FilingType.SC_13D, "schedule 13d": FilingType.SC_13D,
    "13d": FilingType.SC_13D,
    "sc 13d/a": FilingType.SC_13D_A, "13d/a": FilingType.SC_13D_A,
    "sc 13g": FilingType.SC_13G, "13g": FilingType.SC_13G,
    "sc 13g/a": FilingType.SC_13G_A, "13g/a": FilingType.SC_13G_A,
    "form 4": FilingType.FORM_4, "4": FilingType.FORM_4,
    "form d": FilingType.FORM_D, "d": FilingType.FORM_D,
    "form d/a": FilingType.FORM_D_A, "d/a": FilingType.FORM_D_A,
    "adv": FilingType.FORM_ADV, "form adv": FilingType.FORM_ADV,
}


def classify_form(raw: str | None) -> FilingType:
    """Map a raw form string (EDGAR form type / human label) to a FilingType."""
    if not raw:
        return FilingType.UNKNOWN
    key = str(raw).strip().lower()
    if key in _FORM_ALIASES:
        return _FORM_ALIASES[key]
    # tolerate spacing / punctuation noise
    norm = key.replace("sc", "").replace("form", "").replace(".", "").strip()
    norm = " ".join(norm.split())
    return _FORM_ALIASES.get(norm, _FORM_ALIASES.get(norm.replace(" ", ""),
                                                      FilingType.UNKNOWN))


def filing_meta(ft: FilingType) -> FilingMeta:
    return FILING_META.get(ft, FILING_META[FilingType.UNKNOWN])


# ── instrument typing (C) ───────────────────────────────────────────────────
class InstrumentType(str, Enum):
    COMMON = "common"
    CALL = "call"
    PUT = "put"
    ADR = "adr"
    ETF = "etf"
    NOTE = "note"            # convertible / debt
    UNKNOWN = "unknown"


class Direction(str, Enum):
    LONG_EXPOSURE = "long_exposure"
    SHORT_EXPOSURE = "short_exposure"
    DIRECTION_UNKNOWN = "direction_unknown"


_ETF_HINTS = ("etf", "ishares", "spdr", "invesco qqq", "trust", "index fund",
              "select sector")
_ADR_HINTS = ("adr", "american depositary", "sponsored adr")
_NOTE_HINTS = ("note", "conv ", "convertible", "% due", "senior notes")


def classify_instrument(title_of_class: str | None,
                        put_call: str | None,
                        issuer_name: str | None = None) -> InstrumentType:
    """Classify a 13F line by instrument type.

    ``put_call`` is the 13F ``<putCall>`` element (PUT/CALL) and takes priority —
    a line tagged CALL/PUT is an option regardless of the class title.
    """
    pc = (put_call or "").strip().lower()
    if pc == "call":
        return InstrumentType.CALL
    if pc == "put":
        return InstrumentType.PUT
    title = (title_of_class or "").strip().lower()
    name = (issuer_name or "").strip().lower()
    blob = f"{title} {name}"
    if any(h in blob for h in _NOTE_HINTS):
        return InstrumentType.NOTE
    if any(h in blob for h in _ADR_HINTS):
        return InstrumentType.ADR
    if any(h in blob for h in _ETF_HINTS):
        return InstrumentType.ETF
    if "com" in title or "common" in title or "ordinary" in title or "cl " in title \
            or "class" in title or not title:
        return InstrumentType.COMMON
    return InstrumentType.UNKNOWN


def infer_direction(instrument: InstrumentType) -> Direction:
    """Infer directional exposure CONSERVATIVELY.

    Critically, a 13F CALL/PUT line is NOT read as naive long/short: the filing
    cannot distinguish an outright long from a hedge, spread leg or covered
    write. Such lines are ``DIRECTION_UNKNOWN`` by design.
    """
    if instrument in (InstrumentType.COMMON, InstrumentType.ADR, InstrumentType.ETF):
        return Direction.LONG_EXPOSURE
    if instrument in (InstrumentType.CALL, InstrumentType.PUT,
                      InstrumentType.NOTE, InstrumentType.UNKNOWN):
        return Direction.DIRECTION_UNKNOWN
    return Direction.DIRECTION_UNKNOWN


# ── verification status (C) ─────────────────────────────────────────────────
class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    NOT_VERIFIABLE_VIA_13F = "not_verifiable_via_13f"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECTED_NOISE = "rejected_noise"


def assess_verification(
    instrument: InstrumentType,
    direction: Direction,
    mapping_confidence: Optional[float],
    portfolio_pct: Optional[float],
    materiality_floor: float = 0.005,
    mapping_floor: float = 0.6,
) -> tuple[VerificationStatus, list[str]]:
    """Decide how much a single 13F line can actually be trusted as a *signal*.

    Order of checks is intentional — noise first, then unmappable, then the
    options/direction caveat, then materiality, then the clean case.
    """
    reasons: list[str] = []

    # 1. immaterial -> noise (a rounding-error sliver is not a conviction signal)
    if portfolio_pct is not None and 0 <= portfolio_pct < materiality_floor:
        reasons.append(f"position {portfolio_pct:.3%} below materiality "
                       f"floor {materiality_floor:.1%}")
        return VerificationStatus.REJECTED_NOISE, reasons

    # 2. cannot map the CUSIP/ticker with confidence -> human review
    if mapping_confidence is None or mapping_confidence < mapping_floor:
        reasons.append(
            f"ticker mapping confidence "
            f"{'n/a' if mapping_confidence is None else f'{mapping_confidence:.2f}'} "
            f"< {mapping_floor:.2f}")
        return VerificationStatus.NEEDS_HUMAN_REVIEW, reasons

    # 3. options / unknown direction -> a 13F cannot verify directional intent
    if direction is Direction.DIRECTION_UNKNOWN:
        reasons.append(
            f"{instrument.value} line: 13F cannot confirm long/short intent "
            f"(could be hedge / spread / covered write)")
        return VerificationStatus.NOT_VERIFIABLE_VIA_13F, reasons

    # 4. basket exposure (ETF) is real but not single-name conviction
    if instrument is InstrumentType.ETF:
        reasons.append("ETF basket exposure — not single-name conviction")
        return VerificationStatus.PARTIALLY_VERIFIED, reasons

    # 5. clean, mapped, directional, material common/ADR long
    reasons.append("mapped common/ADR long exposure, material & confident")
    return VerificationStatus.VERIFIED, reasons


# ── typed 13F info-table parsing (C) ────────────────────────────────────────
@dataclass
class Typed13FRow:
    issuer_name: str
    cusip: str
    value: Optional[float]
    shares: Optional[float]
    put_call: Optional[str]
    title_of_class: Optional[str]
    instrument: InstrumentType = InstrumentType.UNKNOWN
    direction: Direction = Direction.DIRECTION_UNKNOWN


def _localname(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _to_float(text) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_13f_infotable_typed(xml_text: str) -> list[Typed13FRow]:
    """Parse a 13F information table KEEPING putCall + titleOfClass, so each row
    is typed (Common/CALL/PUT/ADR/ETF/…) with a conservative direction.
    """
    rows: list[Typed13FRow] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("typed 13F parse error: %s", exc)
        return rows

    for el in root.iter():
        if _localname(el.tag) != "infoTable":
            continue
        d: dict = {}
        for child in el.iter():
            n = _localname(child.tag)
            if n == "nameOfIssuer":
                d["name"] = (child.text or "").strip()
            elif n == "cusip":
                d["cusip"] = (child.text or "").strip()
            elif n == "titleOfClass":
                d["title"] = (child.text or "").strip()
            elif n == "putCall":
                d["putcall"] = (child.text or "").strip()
            elif n == "value":
                d["value"] = _to_float(child.text)
            elif n == "sshPrnamt":
                d["shares"] = _to_float(child.text)
        if not d:
            continue
        inst = classify_instrument(d.get("title"), d.get("putcall"), d.get("name"))
        rows.append(Typed13FRow(
            issuer_name=d.get("name", ""), cusip=d.get("cusip", ""),
            value=d.get("value"), shares=d.get("shares"),
            put_call=d.get("putcall"), title_of_class=d.get("title"),
            instrument=inst, direction=infer_direction(inst),
        ))
    return rows


# ── faster filings: SC 13D and Form D parsers (B) ───────────────────────────
@dataclass
class Schedule13DFiling:
    filing_type: FilingType
    subject_issuer: str
    cusip: Optional[str]
    reporting_person: str
    percent_of_class: Optional[float]
    purpose: str = ""
    event_date: Optional[str] = None


@dataclass
class FormDFiling:
    filing_type: FilingType
    issuer_name: str
    total_offering_amount: Optional[float]
    total_amount_sold: Optional[float]
    industry_group: str = ""
    related_persons: list[str] = field(default_factory=list)
    date_of_first_sale: Optional[str] = None


def parse_schedule_13d(data: dict) -> Schedule13DFiling:
    """Parse a simplified SC 13D/G structure (cover-page essentials)."""
    ft = classify_form(data.get("form_type", "SC 13D"))
    return Schedule13DFiling(
        filing_type=ft,
        subject_issuer=str(data.get("subject_issuer", "")).strip(),
        cusip=(str(data["cusip"]).strip() if data.get("cusip") else None),
        reporting_person=str(data.get("reporting_person", "")).strip(),
        percent_of_class=_to_float(data.get("percent_of_class")),
        purpose=str(data.get("purpose", "")).strip(),
        event_date=data.get("event_date"),
    )


def parse_form_d_xml(xml_text: str) -> Optional[FormDFiling]:
    """Parse a Form D ``edgarSubmission`` XML (namespace-tolerant)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Form D parse error: %s", exc)
        return None

    def _find_text(local: str) -> Optional[str]:
        for el in root.iter():
            if _localname(el.tag) == local and el.text and el.text.strip():
                return el.text.strip()
        return None

    issuer = _find_text("entityName") or _find_text("issuerName") or ""
    total = _to_float(_find_text("totalOfferingAmount"))
    sold = _to_float(_find_text("totalAmountSold"))
    industry = _find_text("industryGroupType") or ""
    first_sale = _find_text("dateOfFirstSale")
    persons: list[str] = []
    for el in root.iter():
        if _localname(el.tag) == "relatedPersonInfo":
            first = last = ""
            for c in el.iter():
                if _localname(c.tag) == "firstName":
                    first = (c.text or "").strip()
                elif _localname(c.tag) == "lastName":
                    last = (c.text or "").strip()
            full = " ".join(p for p in (first, last) if p)
            if full:
                persons.append(full)
    return FormDFiling(
        filing_type=FilingType.FORM_D, issuer_name=issuer,
        total_offering_amount=total, total_amount_sold=sold,
        industry_group=industry, related_persons=persons,
        date_of_first_sale=first_sale,
    )


def parse_form_d(data: dict) -> FormDFiling:
    """Parse a simplified Form D JSON structure."""
    return FormDFiling(
        filing_type=classify_form(data.get("form_type", "Form D")),
        issuer_name=str(data.get("issuer_name", "")).strip(),
        total_offering_amount=_to_float(data.get("total_offering_amount")),
        total_amount_sold=_to_float(data.get("total_amount_sold")),
        industry_group=str(data.get("industry_group", "")).strip(),
        related_persons=list(data.get("related_persons", []) or []),
        date_of_first_sale=data.get("date_of_first_sale"),
    )


# ── ADV / IAPD adapter (B) — explicitly NOT EDGAR ───────────────────────────
@dataclass
class ADVRecord:
    adviser_name: str
    crd_number: Optional[str]
    source: SourceSystem = SourceSystem.IAPD
    note: str = "Context only — IAPD registration, not an EDGAR filing."
    needs_human_review: bool = True


class ADVAdapter:
    """Separate adapter for ADV/IAPD data. It deliberately does NOT touch EDGAR
    and never upgrades a registration into a trade signal — every record is
    ``CONTEXT`` and ``needs_human_review``.
    """

    source = SourceSystem.IAPD

    def parse_records(self, rows: list[dict] | None) -> list[ADVRecord]:
        out: list[ADVRecord] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            out.append(ADVRecord(
                adviser_name=str(r.get("adviser_name", "")).strip(),
                crd_number=(str(r["crd_number"]) if r.get("crd_number") else None),
            ))
        return out
