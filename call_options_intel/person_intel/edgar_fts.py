"""
edgar_fts.py
============
EDGAR **Full-Text Search** discovery — the early-signal *vorfeld* source.

The per-CIK feed (:mod:`edgar_fast`) only sees filers we ALREADY track. Full-Text
Search inverts that: it queries the public, free, keyless EDGAR FTS API
(``efts.sec.gov/LATEST/search-index``) for the tracked NAMES (Peter Thiel,
Founders Fund, Mithril, Leopold Aschenbrenner, Situational Awareness, …) and
surfaces every recent filing that *mentions* them — including ones by **filers we
do not yet track**. A hit by an unknown CIK is a **new-entity discovery** (a new
LP / affiliate / fund vehicle), exactly the "neue Entitäten, CIK-Verknüpfungen,
Fund-Namen, neue SEC-File-Numbers" the radar needs.

Only SEC PRIMARY filings flow through here — never media. Each hit is reduced to
the same :class:`~.edgar_fast.FastFilingRef` the rest of the pipeline already
types, classifies and triple-scores, so FTS becomes part of the PRIMARY signal
path rather than a bolt-on.

The HTTP layer is injectable (offline fixtures by default, live efts.sec.gov only
on opt-in), exactly like :mod:`edgar_fast`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import quote_plus

from .edgar_fast import FastFilingRef
from .filings import classify_form, filing_meta

logger = logging.getLogger("coi.person.edgar_fts")

FTS_BASE = "https://efts.sec.gov/LATEST/search-index"
_CIK_IN_NAME_RE = re.compile(r"\(CIK\s*(\d{1,10})\)", re.IGNORECASE)


def _slug(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")


# ── injectable HTTP layer ───────────────────────────────────────────────────
class FTSFetcher(Protocol):
    def get(self, url: str) -> Optional[str]: ...


@dataclass
class UrllibFTSFetcher:
    """Live EDGAR full-text search via stdlib urllib. Used only in live mode."""
    user_agent: str
    timeout_s: int = 15

    def get(self, url: str) -> Optional[str]:  # pragma: no cover - network
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:
            logger.warning("EDGAR FTS fetch failed (%s): %s", url, exc)
            return None


@dataclass
class FixtureFTSFetcher:
    """Offline fetch — maps the query's ``q`` term to a bundled file:

        <fixtures>/edgar_fts/<slug(q)>.json
    """
    fixtures_dir: Path

    def get(self, url: str) -> Optional[str]:
        m = re.search(r"[?&]q=([^&]+)", url)
        if not m:
            return None
        from urllib.parse import unquote_plus
        term = unquote_plus(m.group(1)).strip('"')
        path = Path(self.fixtures_dir) / "edgar_fts" / f"{_slug(term)}.json"
        if not path.exists():
            logger.info("no FTS fixture for %r", term)
            return None
        return path.read_text(encoding="utf-8")


@dataclass
class FtsHit:
    ref: FastFilingRef
    matched_term: str
    principal: str = ""          # which tracked principal the term belongs to


def _norm_cik10(cik: str) -> str:
    return str(cik).strip().lstrip("0").rjust(10, "0") if cik else ""


class EdgarFTSClient:
    def __init__(self, user_agent: str = "", fetcher: Optional[FTSFetcher] = None,
                 base_url: str = FTS_BASE):
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or UrllibFTSFetcher(
            user_agent or "SA-Scanner research (info@pcctradinginc.com)")

    def _url(self, q: str, forms: Optional[list[str]],
             start: Optional[str], end: Optional[str]) -> str:
        parts = [f"q=%22{quote_plus(q)}%22"]
        if forms:
            parts.append("forms=" + quote_plus(",".join(forms)))
        if start:
            parts.append(f"startdt={start}")
        if end:
            parts.append(f"enddt={end}")
        return f"{self.base_url}?{'&'.join(parts)}"

    def search(self, q: str, *, forms: Optional[list[str]] = None,
               start: Optional[str] = None, end: Optional[str] = None,
               principal: str = "") -> list[FtsHit]:
        """Run one full-text query; reduce hits to typed FastFilingRefs."""
        raw = self.fetcher.get(self._url(q, forms, start, end))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("FTS JSON parse failed for %r", q)
            return []
        hits = (((data.get("hits") or {}).get("hits")) or [])
        out: list[FtsHit] = []
        for h in hits:
            src = h.get("_source", {}) or {}
            _id = str(h.get("_id", ""))
            accession = _id.split(":", 1)[0] if _id else ""
            doc = _id.split(":", 1)[1] if ":" in _id else ""
            ciks = src.get("ciks") or []
            names = src.get("display_names") or []
            cik = _norm_cik10(ciks[0]) if ciks else ""
            name = names[0] if names else ""
            # strip the " (CIK 000...)" suffix EDGAR appends to display names
            clean_name = _CIK_IN_NAME_RE.sub("", name).strip()
            form_raw = src.get("form") or src.get("root_form") or ""
            ft = classify_form(form_raw)
            meta = filing_meta(ft)
            url = ""
            if cik and accession:
                folder = accession.replace("-", "")
                url = (f"https://www.sec.gov/Archives/edgar/data/"
                       f"{int(cik)}/{folder}/{doc}")
            out.append(FtsHit(
                ref=FastFilingRef(
                    cik=cik, entity=clean_name, form=form_raw, filing_type=ft,
                    role=meta.role, lag_days=meta.typical_lag_days,
                    filing_date=src.get("file_date", ""), accession=accession,
                    primary_doc=doc, url=url),
                matched_term=q, principal=principal))
        return out

    def discover(self, terms: list[dict], *, since_days: int = 30,
                 as_of: Optional[date] = None) -> list[FtsHit]:
        """Query every configured term, keep recent hits, dedup by accession."""
        as_of = as_of or date.today()
        start = _iso_days_before(as_of, since_days)
        end = as_of.isoformat()
        seen: set[str] = set()
        out: list[FtsHit] = []
        for t in terms or []:
            q = t.get("q") if isinstance(t, dict) else str(t)
            if not q:
                continue
            forms = t.get("forms") if isinstance(t, dict) else None
            principal = t.get("principal", "") if isinstance(t, dict) else ""
            for hit in self.search(q, forms=forms, start=start, end=end,
                                   principal=principal):
                acc = hit.ref.accession
                if not acc or acc in seen:
                    continue
                age = hit.ref.age_days(as_of)
                if age is not None and (age < 0 or age > since_days):
                    continue
                seen.add(acc)
                out.append(hit)
        out.sort(key=lambda h: h.ref.filing_date, reverse=True)
        return out


def _iso_days_before(as_of: date, days: int) -> str:
    from datetime import timedelta
    return (as_of - timedelta(days=max(0, days))).isoformat()


def load_fts_client(data_sources_cfg: dict, mode: str,
                    fixtures_dir: str | Path) -> EdgarFTSClient:
    """Build a client: live → efts.sec.gov network, offline → bundled fixtures."""
    edgar_cfg = (data_sources_cfg or {}).get("edgar_13f", {})
    ua = edgar_cfg.get("user_agent", "SA-Scanner research (info@pcctradinginc.com)")
    if mode == "live":
        return EdgarFTSClient(user_agent=ua)
    return EdgarFTSClient(user_agent=ua,
                          fetcher=FixtureFTSFetcher(Path(fixtures_dir)))
