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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Protocol

from .filings import FilingType, SignalRole, classify_form, filing_meta

logger = logging.getLogger("coi.person.edgar_fast")

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
    """Offline fetch — maps a submissions URL to a bundled JSON file."""
    fixtures_dir: Path

    def get(self, url: str) -> Optional[str]:
        # .../submissions/CIK##########.json  ->  <fixtures>/edgar_submissions/<file>
        name = url.rstrip("/").split("/")[-1]
        path = Path(self.fixtures_dir) / "edgar_submissions" / name
        if not path.exists():
            logger.info("no submissions fixture for %s", name)
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
