"""
scanner/sources/sec_edgar.py
SEC EDGAR Monitor mit vollständigem 13F-XML-Parser.

WICHTIGE ÄNDERUNG v2:
    Harter Datums-Filter: Nur Filings jünger als MAX_FILING_AGE_DAYS
    werden verarbeitet. Verhindert dass alte Amendments (2012, 2014 etc.)
    beim ersten Run als "neu" erkannt werden und unnötige Claude-Calls
    verursachen.

FILING-TYPEN:
    13F-HR  : Quartalsweise Portfolio-Holdings (mit XML-Parser)
    SC 13D  : Strategische Beteiligung > 5% (stärkstes Signal)
    SC 13G  : Passive Beteiligung > 5%
    Form 4  : Insider-Transaktionen (Echtzeit)
"""

import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import feedparser

from ..utils.config import Config
from ..utils.rate_limiter import rate_limiter
from ..utils.ticker_mapper import TickerMapper

logger = logging.getLogger(__name__)
mapper = TickerMapper()

HEADERS = {
    "User-Agent": "SA-Scanner research@example.com",
    "Accept-Encoding": "gzip, deflate",
}

# Maximales Alter eines Filings in Tagen
# 90 Tage = ein Quartal — alles älter ist keine neue Information
MAX_FILING_AGE_DAYS = 90

FILING_TYPES = {
    "13F-HR":   {"weight": 1.0, "description": "Quarterly Portfolio Holdings"},
    "13F-HR/A": {"weight": 0.8, "description": "Amended Quarterly Holdings"},
    "SC 13D":   {"weight": 1.5, "description": "Strategic Stake >5% (STRONGEST)"},
    "SC 13D/A": {"weight": 1.3, "description": "Amended Strategic Stake"},
    "SC 13G":   {"weight": 0.8, "description": "Passive Stake >5%"},
    "SC 13G/A": {"weight": 0.6, "description": "Amended Passive Stake"},
    "4":        {"weight": 1.2, "description": "Insider Transaction (REAL-TIME)"},
}

EDGAR_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany"
    "&CIK={cik}"
    "&type={filing_type}"
    "&dateb=&owner=include"
    "&count=10"
    "&search_text=&output=atom"
)

# CUSIP → Ticker für häufige KI-Infrastruktur Positionen
CUSIP_TO_TICKER = {
    "67066G104": "NVDA",
    "594918104": "MSFT",
    "02079K305": "GOOGL",
    "023135106": "AMZN",
    "30303M102": "META",
    "92826C839": "PLTR",
    "92763W108": "VST",
    "20825C104": "CEG",
    "65473P105": "NRG",
    "457730109": "TSM",
    "11135F101": "AVGO",
    "526057104": "LMT",
    "75513E101": "RTX",
}

SHULMAN_KEYWORDS = [
    "compute", "energy", "power", "artificial intelligence",
    "sovereign", "infrastructure", "nuclear", "data center",
    "scaling", "algorithmic", "doubling",
]

KNOWN_NAMES = {
    "NVIDIA":        "NVDA",
    "MICROSOFT":     "MSFT",
    "ALPHABET":      "GOOGL",
    "AMAZON":        "AMZN",
    "META PLATFORM": "META",
    "PALANTIR":      "PLTR",
    "VISTRA":        "VST",
    "CONSTELLATION": "CEG",
    "NRG ENERGY":    "NRG",
    "TAIWAN SEMI":   "TSM",
    "BROADCOM":      "AVGO",
    "LOCKHEED":      "LMT",
    "RAYTHEON":      "RTX",
    "ANDURIL":       None,
    "OPENAI":        None,
}


# ── DATUMS-FILTER ─────────────────────────────────────────────────────────────

def get_cutoff_date() -> str:
    """
    Gibt ISO-Datum für maximales Filing-Alter zurück.
    Alles vor diesem Datum wird ignoriert.
    """
    cutoff = datetime.utcnow() - timedelta(days=MAX_FILING_AGE_DAYS)
    return cutoff.isoformat()


def is_filing_recent(filing_date: str, cutoff: str) -> bool:
    """
    Prüft ob ein Filing-Datum jünger als der Cutoff ist.
    Robustes Parsing für verschiedene Datumsformate von EDGAR.
    """
    if not filing_date:
        return False
    try:
        # EDGAR nutzt verschiedene Formate
        # z.B. "2026-04-05T14:07:00-04:00" oder "2026-04-05"
        date_clean = filing_date[:10]  # Nur YYYY-MM-DD
        cutoff_clean = cutoff[:10]
        return date_clean >= cutoff_clean
    except Exception:
        return False


# ── 13F XML PARSER ────────────────────────────────────────────────────────────

def get_xml_url_from_filing(filing_url: str) -> Optional[str]:
    """
    Lädt Filing-Index und findet URL des 13F Information Table XML.
    """
    try:
        rate_limiter.wait("sec_edgar")
        r = requests.get(filing_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        content = r.text

        patterns = [
            r'href="(/Archives/[^"]+information[Tt]able[^"]*\.xml)"',
            r'href="(/Archives/[^"]+13[fF][^"]*\.xml)"',
            r'href="(/Archives/[^"]+form13f[^"]*\.xml)"',
            r'href="(/Archives/[^"]+\.xml)"',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                return f"https://www.sec.gov{matches[0]}"

        return None

    except Exception as e:
        logger.warning(f"get_xml_url error: {e}")
        return None


def _extract_text(element, tags: list) -> str:
    NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"
    for tag in tags:
        el = element.find(tag)
        if el is not None and el.text:
            return el.text.strip()
        el = element.find(f"{{{NS}}}{tag}")
        if el is not None and el.text:
            return el.text.strip()
    return ""


def _name_to_ticker(name: str) -> str:
    ticker = mapper.name_to_ticker(name)
    if ticker:
        return ticker
    name_upper = name.upper()
    for key, val in KNOWN_NAMES.items():
        if key in name_upper:
            return val or ""
    return ""


def parse_13f_xml(xml_url: str) -> list:
    """Parst 13F Information Table XML."""
    positions = []
    try:
        rate_limiter.wait("sec_edgar")
        r = requests.get(xml_url, headers=HEADERS, timeout=30)
        r.raise_for_status()

        NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"
        root = ET.fromstring(r.content)

        info_tables = (
            root.findall(".//infoTable") or
            root.findall(f".//{{{NS}}}infoTable")
        )

        logger.info(f"13F XML: {len(info_tables)} entries found")

        for entry in info_tables:
            name     = _extract_text(entry, ["nameOfIssuer"])
            cusip    = _extract_text(entry, ["cusip"])
            value    = _extract_text(entry, ["value"])
            put_call = _extract_text(entry, ["putCall"])

            shares_el = (
                entry.find(".//sshPrnamt") or
                entry.find(f".//{{{NS}}}sshPrnamt")
            )
            shares = (shares_el.text.strip()
                      if shares_el is not None and shares_el.text
                      else "0")

            ticker = CUSIP_TO_TICKER.get(cusip, "")
            if not ticker and name:
                ticker = _name_to_ticker(name)

            if not ticker:
                logger.debug(f"No ticker: {name} (CUSIP: {cusip})")
                continue

            try:
                positions.append({
                    "ticker":    ticker,
                    "name":      name,
                    "cusip":     cusip,
                    "shares":    int(shares.replace(",", "")),
                    "value_usd": int(value.replace(",", "")) * 1000
                                 if value else 0,
                    "put_call":  put_call,
                })
            except (ValueError, TypeError):
                continue

        logger.info(
            f"13F XML: {len(positions)} positions with known tickers"
        )
        return positions

    except ET.ParseError as e:
        logger.error(f"13F XML parse error: {e}")
        return []
    except Exception as e:
        logger.error(f"13F XML fetch error: {e}")
        return []


def get_previous_holdings(entity: str) -> dict:
    """Lädt letzte bekannte Holdings aus SQLite."""
    try:
        conn = sqlite3.connect(str(Config.DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filing_holdings (
                entity       TEXT,
                ticker       TEXT,
                filing_date  TEXT,
                shares       INTEGER,
                value_usd    INTEGER,
                cusip        TEXT,
                PRIMARY KEY (entity, ticker, filing_date)
            )
        """)
        conn.commit()

        latest = conn.execute(
            """SELECT MAX(filing_date) as max_date
               FROM filing_holdings WHERE entity = ?""",
            (entity,)
        ).fetchone()

        if not latest or not latest["max_date"]:
            conn.close()
            return {}

        rows = conn.execute(
            """SELECT ticker, shares FROM filing_holdings
               WHERE entity = ? AND filing_date = ?""",
            (entity, latest["max_date"])
        ).fetchall()
        conn.close()
        return {r["ticker"]: r["shares"] for r in rows}

    except Exception as e:
        logger.warning(f"get_previous_holdings error: {e}")
        return {}


def save_current_holdings(entity: str, filing_date: str,
                           positions: list):
    """Speichert aktuelle Holdings in SQLite."""
    try:
        conn = sqlite3.connect(str(Config.DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filing_holdings (
                entity       TEXT,
                ticker       TEXT,
                filing_date  TEXT,
                shares       INTEGER,
                value_usd    INTEGER,
                cusip        TEXT,
                PRIMARY KEY (entity, ticker, filing_date)
            )
        """)
        for pos in positions:
            conn.execute(
                """INSERT OR REPLACE INTO filing_holdings
                   (entity, ticker, filing_date, shares, value_usd, cusip)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entity, pos["ticker"], filing_date,
                 pos["shares"], pos["value_usd"], pos["cusip"])
            )
        conn.commit()
        conn.close()
        logger.info(f"Saved {len(positions)} holdings for {entity}")
    except Exception as e:
        logger.error(f"save_current_holdings error: {e}")


def classify_position_delta(current_positions: list,
                              previous_holdings: dict) -> list:
    """Vergleicht aktuelle mit vorherigen Positionen."""
    classifications = []
    current = {p["ticker"]: p["shares"] for p in current_positions}
    all_tickers = set(list(current.keys()) + list(previous_holdings.keys()))

    for ticker in all_tickers:
        curr_shares = current.get(ticker, 0)
        prev_shares = previous_holdings.get(ticker, 0)

        if prev_shares == 0 and curr_shares > 0:
            cls   = "A"
            score = 9.5
            desc  = "NEW_POSITION"
            logger.info(f"🆕 NEW: {ticker} ({curr_shares:,} shares)")

        elif curr_shares == 0 and prev_shares > 0:
            cls   = "A"
            score = 9.0
            desc  = "CLOSED_POSITION"
            logger.info(f"❌ CLOSED: {ticker}")

        elif prev_shares > 0 and curr_shares > 0:
            change_pct = abs(curr_shares - prev_shares) / prev_shares * 100
            direction  = ("INCREASED" if curr_shares > prev_shares
                          else "REDUCED")
            if change_pct > 20:
                cls   = "B"
                score = min(7.5 + change_pct / 100, 9.0)
                desc  = f"{direction}_{change_pct:.0f}pct"
                logger.info(f"📈 {direction}: {ticker} {change_pct:.0f}%")
            elif change_pct > 10:
                cls   = "B"
                score = 7.5
                desc  = f"{direction}_{change_pct:.0f}pct"
            elif change_pct > 5:
                cls   = "C"
                score = 5.5
                desc  = "MINOR_CHANGE"
            else:
                cls   = "D"
                score = 3.0
                desc  = "UNCHANGED"
        else:
            continue

        classifications.append({
            "ticker":      ticker,
            "class":       cls,
            "score":       round(score, 1),
            "prev_shares": prev_shares,
            "curr_shares": curr_shares,
            "change_pct":  round(
                (curr_shares - prev_shares) / max(prev_shares, 1) * 100, 1
            ),
            "description": desc,
            "sector":      mapper.get_sector(ticker),
            "is_new":      cls == "A" and desc == "NEW_POSITION",
        })

    classifications.sort(key=lambda x: x["score"], reverse=True)
    return classifications


def check_begleittext_for_shulman(text: str) -> dict:
    found = [kw for kw in SHULMAN_KEYWORDS if kw.lower() in text.lower()]
    bonus = Config.SHULMAN_SALP_BEGLEIT_BONUS if len(found) >= 2 else 0.0
    return {"keywords_found": found, "shulman_bonus": bonus}


def _assess_signal_strength(filing_type: str, title: str) -> str:
    t = title.lower()
    if filing_type in ("SC 13D", "SC 13D/A"):
        return "VERY_STRONG"
    if filing_type == "4":
        if any(w in t for w in ["purchase", "buy", "acquisition"]):
            return "STRONG_BUY"
        if any(w in t for w in ["sale", "sell", "disposition"]):
            return "STRONG_SELL"
        return "MODERATE"
    if filing_type in ("13F-HR", "13F-HR/A"):
        return "QUARTERLY_UPDATE"
    if filing_type in ("SC 13G", "SC 13G/A"):
        return "MODERATE_PASSIVE"
    return "UNKNOWN"


# ── MAIN EDGAR MONITOR ───────────────────────────────────────────────────────

def check_new_filings(state_manager) -> list:
    """
    Checkt alle Filing-Typen für alle CIK-Targets.

    DATUMS-FILTER (wichtigste Änderung):
    - Nur Filings jünger als MAX_FILING_AGE_DAYS (90 Tage)
    - Beim ersten Run: verhindert dass historische Amendments
      aus 2012-2022 als "neu" erkannt werden
    - Bei Folge-Runs: nur wirklich neue Filings seit letztem Check
    """
    new_filings = []

    # Harter Cutoff — alles älter als 90 Tage ignorieren
    cutoff = get_cutoff_date()
    logger.info(f"EDGAR date filter: only filings after {cutoff[:10]}")

    for entity, cik in Config.SEC_CIK_TARGETS.items():
        for filing_type, type_info in FILING_TYPES.items():
            try:
                rate_limiter.wait("sec_edgar")
                url  = EDGAR_RSS.format(
                    cik=cik,
                    filing_type=filing_type.replace(" ", "+")
                )
                feed = feedparser.parse(url, request_headers=HEADERS)

                key       = f"{entity}_{filing_type.replace(' ', '_')}"
                last_date = state_manager.get_last_filing_date(key) or cutoff

                skipped_old = 0

                for entry in feed.entries:
                    filing_date = entry.get("updated", "")

                    # FILTER 1: Nicht älter als MAX_FILING_AGE_DAYS
                    if not is_filing_recent(filing_date, cutoff):
                        skipped_old += 1
                        continue

                    # FILTER 2: Neuer als letztes bekanntes Filing
                    if filing_date <= last_date:
                        continue

                    filing_info = {
                        "entity":          entity,
                        "cik":             cik,
                        "filing_type":     filing_type,
                        "filing_date":     filing_date,
                        "filing_url":      entry.link,
                        "title":           entry.title,
                        "weight":          type_info["weight"],
                        "description":     type_info["description"],
                        "signal_strength": _assess_signal_strength(
                            filing_type, entry.title
                        ),
                        "positions":       [],
                        "classifications": [],
                    }

                    # 13F: vollständiger XML-Parse
                    if filing_type in ("13F-HR", "13F-HR/A"):
                        logger.info(
                            f"Parsing 13F XML for {entity}: {entry.link}"
                        )
                        xml_url = get_xml_url_from_filing(entry.link)
                        if xml_url:
                            positions = parse_13f_xml(xml_url)
                            if positions:
                                previous = get_previous_holdings(entity)
                                classifications = classify_position_delta(
                                    positions, previous
                                )
                                save_current_holdings(
                                    entity, filing_date, positions
                                )
                                filing_info["positions"]       = positions
                                filing_info["classifications"] = classifications
                                filing_info["shulman_begleit"] = (
                                    check_begleittext_for_shulman(
                                        entry.get("summary", "") +
                                        entry.title
                                    )
                                )
                                new_pos = sum(
                                    1 for c in classifications
                                    if c["class"] == "A" and c["is_new"]
                                )
                                logger.info(
                                    f"13F {entity}: "
                                    f"{len(positions)} positions | "
                                    f"NEW: {new_pos}"
                                )

                    new_filings.append(filing_info)
                    state_manager.update_filing(
                        key, cik, filing_date,
                        entry.link, filing_type
                    )
                    logger.info(
                        f"NEW FILING: {entity} | {filing_type} | "
                        f"{filing_date[:10]} | {entry.title[:50]}"
                    )

                if skipped_old > 0:
                    logger.debug(
                        f"Skipped {skipped_old} old filings "
                        f"({entity} / {filing_type}) — older than "
                        f"{MAX_FILING_AGE_DAYS} days"
                    )

            except Exception as e:
                logger.warning(f"EDGAR {entity} {filing_type}: {e}")

    return new_filings


def run_edgar_monitor(state_manager) -> dict:
    """Vollständiger EDGAR-Monitor für alle Filing-Typen."""
    logger.info(
        f"Running SEC EDGAR monitor "
        f"(13F-XML + SC13D + SC13G + Form4 | "
        f"max age: {MAX_FILING_AGE_DAYS} days)"
    )

    new_filings = check_new_filings(state_manager)

    # Alle Classifications zusammenführen und deduplizieren
    all_cls = []
    for f in new_filings:
        all_cls.extend(f.get("classifications", []))

    seen = {}
    for cls in all_cls:
        t = cls["ticker"]
        if t not in seen or cls["score"] > seen[t]["score"]:
            seen[t] = cls
    deduplicated = sorted(
        seen.values(), key=lambda x: x["score"], reverse=True
    )

    # Neue Ticker extrahieren
    new_tickers = [
        c["ticker"] for c in deduplicated
        if c["class"] == "A" and c.get("is_new", False)
    ]

    very_strong = [f for f in new_filings
                   if f.get("signal_strength") == "VERY_STRONG"]
    strong      = [f for f in new_filings
                   if f.get("signal_strength") in
                   ("STRONG_BUY", "QUARTERLY_UPDATE")]

    salp_score = 3.0
    if very_strong:
        salp_score = 10.0
    elif new_tickers:
        salp_score = 9.5
    elif deduplicated and deduplicated[0]["class"] == "B":
        salp_score = 8.0
    elif strong:
        salp_score = 7.0

    result = {
        "new_filings_found":   len(new_filings),
        "new_filings":         new_filings,
        "classifications":     deduplicated,
        "new_tickers":         new_tickers,
        "very_strong_signals": very_strong,
        "strong_signals":      strong,
        "checked_entities":    list(Config.SEC_CIK_TARGETS.keys()),
        "checked_types":       list(FILING_TYPES.keys()),
        "salp_score_override": salp_score,
        "trigger_pipeline":    len(new_filings) > 0,
        "date_filter_cutoff":  get_cutoff_date()[:10],
        "checked_at":          datetime.utcnow().isoformat(),
    }

    Config.SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    (Config.SIGNALS_DIR / "sec_filings.json").write_text(
        json.dumps(result, indent=2, default=str)
    )

    if new_filings:
        logger.info(
            f"EDGAR ALERT: {len(new_filings)} new filings | "
            f"New Tickers: {new_tickers} | "
            f"Very Strong: {len(very_strong)}"
        )
    else:
        logger.info(
            f"No new filings within {MAX_FILING_AGE_DAYS} days — "
            f"entities: {list(Config.SEC_CIK_TARGETS.keys())}"
        )

    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    logging.basicConfig(level=logging.INFO)
    from scanner.utils.state_manager import StateManager
    with StateManager() as sm:
        result = run_edgar_monitor(sm)
        print(f"\nCutoff: {result['date_filter_cutoff']}")
        print(f"New Filings: {result['new_filings_found']}")
        print(f"New Tickers: {result['new_tickers']}")
        for cls in result["classifications"][:10]:
            print(f"  {cls['ticker']}: {cls['class']} | {cls['description']}")
