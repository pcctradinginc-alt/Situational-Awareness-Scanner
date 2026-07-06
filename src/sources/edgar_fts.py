"""EDGAR full-text search (EFTS) watcher.

Finds SEC filings by *other* filers that mention Situational Awareness LP —
PIPE 8-Ks, S-1/424B prospectuses, proxy statements, joint 13D/Gs, Form Ds of
feeder vehicles. These often surface an investment months before it appears
in SA LP's own 13F, which makes this the widest early-detection net we have.

Filings filed by the watched CIKs themselves are skipped (they arrive via the
submissions API in sources/sec.py). Seen accessions are persisted in
data/state/fts_seen.json so every hit alerts exactly once.
"""
from __future__ import annotations

import re
import urllib.parse

from ..config import Config
from ..utils import HttpClient, get_logger, read_json, today_iso, utc_now_iso, write_json

log = get_logger("sources.edgar_fts")

FTS_URL = "https://efts.sec.gov/LATEST/search-index?q={query}"
STATE_FILE = "fts_seen.json"

# Only emit events for filings younger than this; older hits are marked seen
# silently (first-run backfill would otherwise flood the alert email).
MAX_AGE_DAYS = 90

_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.,\- ]{0,30})\)")


def _load_seen(cfg: Config) -> set[str]:
    data = read_json(cfg.paths.state / STATE_FILE, default={"accessions": []})
    return set(data.get("accessions", []))


def _save_seen(cfg: Config, seen: set[str]) -> None:
    write_json(
        cfg.paths.state / STATE_FILE,
        {"accessions": sorted(seen), "updated": utc_now_iso()},
    )


def _tickers_from_display(display_names: list[str]) -> list[str]:
    """Extract ticker symbols from EFTS display names like 'Nebius (NBIS) (CIK ...)'."""
    out: list[str] = []
    for name in display_names or []:
        for group in _TICKER_RE.findall(name):
            if group.startswith("CIK"):
                continue
            for t in group.split(","):
                t = t.strip()
                if t and t not in out and 1 <= len(t) <= 6 and t.isalpha():
                    out.append(t)
    return out


def _doc_url(ciks: list[str], accession: str, filename: str) -> str:
    if not ciks:
        return f"https://efts.sec.gov/LATEST/search-index?q={accession}"
    cik_int = str(int(ciks[0]))
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{filename}"


def _is_recent(file_date: str) -> bool:
    """True if file_date (YYYY-MM-DD) is within MAX_AGE_DAYS of today."""
    import datetime

    try:
        d = datetime.date.fromisoformat(file_date)
    except (ValueError, TypeError):
        return True  # no date → be safe, treat as recent
    return (datetime.date.today() - d).days <= MAX_AGE_DAYS


def search_mentions(cfg: Config) -> list[dict]:
    """Run all configured full-text queries; return new (unseen) mention hits.

    Each returned dict has: accession, form, entity, file_date, url,
    ticker_guess, query. The seen-state is updated for everything found —
    including own-CIK and stale hits, which are skipped silently.
    """
    queries: list[str] = list(
        cfg.raw.get("sec", {}).get("fts_queries", [cfg.primary_name])
    )
    watched = {str(int(c)) for c in cfg.all_ciks}
    client = HttpClient(cfg.sec_user_agent, cfg.sec_request_delay)
    seen = _load_seen(cfg)
    first_run = not seen

    new_hits: list[dict] = []
    for q in queries:
        url = FTS_URL.format(query=urllib.parse.quote(f'"{q}"'))
        try:
            data = client.get_json(url, timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("EFTS query failed for %r: %s", q, exc)
            continue

        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            _id = hit.get("_id", "")
            accession, _, filename = _id.partition(":")
            accession = accession or src.get("accession_no", "")
            if not accession or accession in seen:
                continue
            seen.add(accession)

            hit_ciks = [str(int(c)) for c in src.get("ciks", []) if str(c).strip().isdigit()]
            if any(c in watched for c in hit_ciks):
                continue  # our own filing — already handled via submissions API
            file_date = src.get("file_date", "")
            if not _is_recent(file_date):
                log.debug("FTS: skipping stale hit %s (%s).", accession, file_date)
                continue

            display = src.get("display_names") or ["unknown filer"]
            forms = src.get("root_forms") or [src.get("file_type", "?")]
            new_hits.append({
                "accession": accession,
                "form": ", ".join(forms),
                "entity": display[0].split("  (CIK")[0].strip(),
                "file_date": file_date,
                "url": _doc_url(src.get("ciks", []), accession, filename),
                "ticker_guess": _tickers_from_display(display),
                "query": q,
            })

    _save_seen(cfg, seen)
    if first_run and new_hits:
        log.info("FTS first run: %d recent mention(s) surfaced, older hits seeded silently.", len(new_hits))
    log.info("EDGAR FTS: %d new mention(s) across %d query/ies.", len(new_hits), len(queries))
    return new_hits
