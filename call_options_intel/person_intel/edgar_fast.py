"""
edgar_fast.py
=============
Phase 2 — a FREE, off-by-default adapter for the *fast* EDGAR filings that make a
person-signal EARLY rather than a quarterly confirmation:

    Form 4 (~2d) · SC 13D/A (~5d) · SC 13G/A · Form D/A (~15d)

It reads SEC's free ``submissions`` JSON API (no key; a descriptive User-Agent is
required by SEC fair-access policy). The HTTP layer is INJECTABLE:
  * ``FixtureFetcher`` — reads bundled JSON offline (deterministic, tested);
  * ``UrllibFetcher`` — real network, stdlib only, used only in --live mode.

Nothing here trades or fetches anything by default; a scan stays offline unless a
caller explicitly opts into live mode.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Protocol

from .cusip_map import CusipMapper, is_valid_cusip
from .filings import (
    FilingType, SignalRole, classify_form, filing_meta, parse_form_d_xml,
)

logger = logging.getLogger("coi.person.edgar_fast")

# 9-char CUSIP token; we validate the check digit before trusting any match.
_CUSIP_TOKEN_RE = re.compile(r"\b([0-9A-Z]{9})\b")
_ISSUER_LABEL_RE = re.compile(
    r"Name of Issuer\s*[:)]?\s*\n?\s*([A-Z0-9][^\n<]{2,80})", re.IGNORECASE)

EARLY_FORMS = {
    FilingType.FORM_4, FilingType.SC_13D, FilingType.SC_13D_A,
    FilingType.SC_13G, FilingType.SC_13G_A, FilingType.FORM_D, FilingType.FORM_D_A,
}


# ── injectable HTTP layer ───────────────────────────────────────────────────
class Fetcher(Protocol):
    def get(self, url: str) -> Optional[str]: ...


@dataclass
class UrllibFetcher:
    """Real SEC fetch via stdlib urllib. Used only in live mode."""
    user_agent: str
    timeout_s: int = 15

    def get(self, url: str) -> Optional[str]:  # pragma: no cover - network
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:
            logger.warning("EDGAR fetch failed (%s): %s", url, exc)
            return None


@dataclass
class FixtureFetcher:
    """Offline fetch — maps SEC URLs to bundled files.

      .../submissions/CIK##########.json -> <fixtures>/edgar_submissions/<file>
      .../Archives/.../<doc>             -> <fixtures>/edgar_documents/<doc-basename>
    """
    fixtures_dir: Path

    def get(self, url: str) -> Optional[str]:
        u = url.rstrip("/")
        name = u.split("/")[-1]
        if "/submissions/" in u:
            path = Path(self.fixtures_dir) / "edgar_submissions" / name
        elif "/Archives/" in u:
            path = Path(self.fixtures_dir) / "edgar_documents" / name
        else:
            return None
        if not path.exists():
            logger.info("no fixture for %s", name)
            return None
        return path.read_text(encoding="utf-8")


@dataclass
class FastFilingRef:
    cik: str
    entity: str
    form: str
    filing_type: FilingType
    role: SignalRole
    lag_days: int
    filing_date: str
    accession: str
    primary_doc: str = ""
    url: str = ""

    def age_days(self, as_of: Optional[date] = None) -> Optional[int]:
        try:
            d = datetime.fromisoformat(self.filing_date).date()
        except Exception:
            return None
        return ((as_of or date.today()) - d).days


def _norm_cik10(cik: str) -> str:
    return str(cik).strip().lstrip("0").rjust(10, "0")


# ── subject resolution (Phase 3) ────────────────────────────────────────────
@dataclass
class ResolvedFiling:
    ref: FastFilingRef
    subject_issuer: Optional[str]
    cusip: Optional[str]
    mapped_ticker: Optional[str]
    mapping_confidence: float
    needs_human_review: bool
    detail: str = ""


def extract_cusip(text: str) -> Optional[str]:
    """First check-digit-valid CUSIP token in the document text, if any."""
    if not text:
        return None
    # prefer a token that appears right after a 'CUSIP' label
    for m in re.finditer(r"CUSIP[^0-9A-Z]{0,12}([0-9A-Z]{9})", text, re.IGNORECASE):
        tok = m.group(1).upper()
        if is_valid_cusip(tok):
            return tok
    for m in _CUSIP_TOKEN_RE.finditer(text):
        tok = m.group(1).upper()
        if is_valid_cusip(tok):
            return tok
    return None


def extract_issuer(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ISSUER_LABEL_RE.search(text)
    return m.group(1).strip() if m else None


# Form 4 ownership XML names its subject FIRST-PARTY: <issuerName> and
# <issuerTradingSymbol> are asserted by the filer inside the SEC document —
# reading them is sourcing, not guessing. Placeholder symbols ("NONE", "N/A")
# mean the issuer is unlisted and must stay needs_human_review.
_F4_SYMBOL_RE = re.compile(
    r"<issuerTradingSymbol>\s*([A-Za-z][A-Za-z.\-]{0,9})\s*</issuerTradingSymbol>")
_F4_NAME_RE = re.compile(r"<issuerName>\s*([^<]{1,120}?)\s*</issuerName>")
_F4_PLACEHOLDER_SYMBOLS = {"NONE", "N/A", "NA", "FALSE", "TRUE"}


def extract_form4_issuer(text: str) -> tuple[Optional[str], Optional[str]]:
    """(trading_symbol, issuer_name) from Form 3/4/5 ownership XML, else Nones."""
    if not text:
        return None, None
    name_m = _F4_NAME_RE.search(text)
    name = name_m.group(1).strip() if name_m else None
    sym_m = _F4_SYMBOL_RE.search(text)
    sym = sym_m.group(1).strip().upper() if sym_m else None
    if sym in _F4_PLACEHOLDER_SYMBOLS:
        sym = None
    return sym, name


class EdgarFastClient:
    def __init__(self, user_agent: str = "", fetcher: Optional[Fetcher] = None,
                 base_url: str = "https://data.sec.gov"):
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or UrllibFetcher(
            user_agent or "SA-Scanner research (info@pcctradinginc.com)")

    def submissions_url(self, cik: str) -> str:
        return f"{self.base_url}/submissions/CIK{_norm_cik10(cik)}.json"

    def recent_filings(self, cik: str, entity_name: str = "") -> list[FastFilingRef]:
        """All recent filings for a CIK, typed. Empty list on any failure."""
        raw = self.fetcher.get(self.submissions_url(cik))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("submissions JSON parse failed for CIK %s", cik)
            return []
        name = entity_name or data.get("name", "")
        recent = (data.get("filings", {}) or {}).get("recent", {}) or {}
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        accns = recent.get("accessionNumber", []) or []
        docs = recent.get("primaryDocument", []) or []
        cik_int = str(int(_norm_cik10(cik)))
        out: list[FastFilingRef] = []
        for i, form in enumerate(forms):
            ft = classify_form(form)
            meta = filing_meta(ft)
            accession = accns[i] if i < len(accns) else ""
            doc = docs[i] if i < len(docs) else ""
            url = ""
            if accession:
                folder = accession.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{folder}/{doc}"
            out.append(FastFilingRef(
                cik=_norm_cik10(cik), entity=name, form=form, filing_type=ft,
                role=meta.role, lag_days=meta.typical_lag_days,
                filing_date=dates[i] if i < len(dates) else "",
                accession=accession, primary_doc=doc, url=url))
        return out

    def fast_filings(self, cik: str, entity_name: str = "") -> list[FastFilingRef]:
        """Only filings that are faster than a quarterly 13F (13D/G, Form 4,
        Form D) — the leading person-signals (vs 13F confirmation)."""
        return [r for r in self.recent_filings(cik, entity_name)
                if r.filing_type in EARLY_FORMS]

    def fetch_document(self, ref: FastFilingRef) -> Optional[str]:
        return self.fetcher.get(ref.url) if ref.url else None

    def resolve_subject(self, ref: FastFilingRef, mapper: CusipMapper,
                        doc_text: Optional[str] = None) -> ResolvedFiling:
        """Resolve a fast filing to the PUBLIC subject ticker (13D/G/Form 4) via
        its document's CUSIP, or surface the private issuer (Form D).

        Never guesses: an unmapped/uncertain subject stays needs_human_review.
        """
        text = doc_text if doc_text is not None else (self.fetch_document(ref) or "")

        if ref.filing_type in (FilingType.FORM_D, FilingType.FORM_D_A):
            fd = parse_form_d_xml(text) if text else None
            issuer = fd.issuer_name if fd else None
            return ResolvedFiling(
                ref=ref, subject_issuer=issuer, cusip=None, mapped_ticker=None,
                mapping_confidence=0.0, needs_human_review=True,
                detail="private placement — no public ticker (issuer is the offeror)")

        if ref.filing_type is FilingType.FORM_4:
            sym, issuer_name = extract_form4_issuer(text)
            if not sym and doc_text is None and "/xsl" in (ref.url or ""):
                # the submissions API often lists the XSL-styled view as the
                # primary document; the raw ownership XML lives one level up
                raw = self.fetcher.get(re.sub(r"/xsl[^/]+/", "/", ref.url)) or ""
                sym, issuer_name = extract_form4_issuer(raw)
            if sym:
                return ResolvedFiling(
                    ref=ref, subject_issuer=issuer_name, cusip=None,
                    mapped_ticker=sym, mapping_confidence=0.95,
                    needs_human_review=False,
                    detail="Form 4 subject via issuerTradingSymbol "
                           "(first-party SEC tag in the ownership XML)")
            if issuer_name:
                return ResolvedFiling(
                    ref=ref, subject_issuer=issuer_name, cusip=None,
                    mapped_ticker=None, mapping_confidence=0.0,
                    needs_human_review=True,
                    detail="Form 4 issuer named but no listed trading symbol "
                           "— likely unlisted; verify manually")

        cusip = extract_cusip(text)
        issuer = extract_issuer(text)
        if not cusip:
            return ResolvedFiling(
                ref=ref, subject_issuer=issuer, cusip=None, mapped_ticker=None,
                mapping_confidence=0.0, needs_human_review=True,
                detail="no valid CUSIP found in document")
        res = mapper.map_cusip(cusip, issuer or "")
        return ResolvedFiling(
            ref=ref, subject_issuer=issuer, cusip=cusip,
            mapped_ticker=res.mapped_ticker, mapping_confidence=res.confidence,
            needs_human_review=res.needs_human_review,
            detail=f"{ref.filing_type.value} subject via CUSIP "
                   f"({res.mapping_source})")


class FastFilingMonitor:
    """Aggregate a recent EARLY-filing feed across tracked managers."""

    def __init__(self, client: EdgarFastClient):
        self.client = client

    def feed(self, managers_cfg: list[dict], since_days: int = 30,
             as_of: Optional[date] = None) -> list[FastFilingRef]:
        as_of = as_of or date.today()
        rows: list[FastFilingRef] = []
        for m in managers_cfg or []:
            cik = m.get("cik")
            if not cik:
                continue
            for r in self.client.fast_filings(cik, m.get("name", "")):
                age = r.age_days(as_of)
                if age is not None and 0 <= age <= since_days:
                    rows.append(r)
        # most recent first; ties: more-leading (smaller lag) first
        rows.sort(key=lambda r: (r.filing_date, -r.lag_days), reverse=True)
        return rows


def load_fast_client(data_sources_cfg: dict, mode: str,
                     fixtures_dir: str | Path) -> EdgarFastClient:
    """Build a client: live -> network, offline -> bundled submissions fixtures."""
    edgar_cfg = (data_sources_cfg or {}).get("edgar_13f", {})
    ua = edgar_cfg.get("user_agent", "SA-Scanner research (info@pcctradinginc.com)")
    base = edgar_cfg.get("base_url", "https://data.sec.gov")
    if mode == "live":
        return EdgarFastClient(user_agent=ua, base_url=base)
    return EdgarFastClient(user_agent=ua, base_url=base,
                           fetcher=FixtureFetcher(Path(fixtures_dir)))
