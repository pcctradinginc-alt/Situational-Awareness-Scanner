"""
scanner/sources/sec_edgar.py
SEC EDGAR Monitor für alle relevanten Filing-Typen:
- 13F  : Quartalsweise Portfolio-Holdings (SA LP, Thiel Capital, Founders Fund)
- SC 13D: Strategische Beteiligung > 5% (sehr starkes Signal)
- SC 13G: Passive Beteiligung > 5%
- Form 4: Insider-Transaktionen (Echtzeit-Signal)

SIGNAL-STÄRKE:
    SC 13D  → Stärkstes Signal (strategische Beteiligung)
    Form 4  → Starkes Signal (Insider-Kauf/Verkauf in Echtzeit)
    13F     → Quartalsweises Signal (verzögert, aber breitestes Bild)
    SC 13G  → Moderates Signal (passive Beteiligung)
"""

import json
import logging
import requests
import feedparser
from datetime import datetime
from pathlib import Path

from ..utils.config import Config
from ..utils.rate_limiter import rate_limiter
from ..utils.ticker_mapper import TickerMapper

logger = logging.getLogger(__name__)
mapper = TickerMapper()

# Alle relevanten Filing-Typen mit Gewichtung
FILING_TYPES = {
    "13F-HR":  {"weight": 1.0, "description": "Quarterly Portfolio Holdings"},
    "13F-HR/A":{"weight": 0.8, "description": "Amended Quarterly Holdings"},
    "SC 13D":  {"weight": 1.5, "description": "Strategic Stake > 5% (STRONGEST)"},
    "SC 13D/A":{"weight": 1.3, "description": "Amended Strategic Stake"},
    "SC 13G":  {"weight": 0.8, "description": "Passive Stake > 5%"},
    "SC 13G/A":{"weight": 0.6, "description": "Amended Passive Stake"},
    "4":       {"weight": 1.2, "description": "Insider Transaction (REAL-TIME)"},
}

EDGAR_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany"
    "&CIK={cik}"
    "&type={filing_type}"
    "&dateb=&owner=include"
    "&count=5"
    "&search_text=&output=atom"
)

HEADERS = {
    "User-Agent": "SA-Scanner research@example.com",
    "Accept-Encoding": "gzip, deflate",
}

SHULMAN_KEYWORDS = [
    "doubling", "compute", "intelligence explosion",
    "recursive", "algorithmic", "scaling", "robot",
    "energy demand", "power", "sovereign", "ai infrastructure",
]

# Filing-Typ zu Score-Mapping
FILING_CLASS_SCORES = {
    # 13F Klassen
    "A_NEW":     9.5,  # Neue Position
    "A_CLOSED":  9.0,  # Position geschlossen
    "B_LARGE":   8.5,  # > 20% Veränderung
    "B_MEDIUM":  7.5,  # 10-20% Veränderung
    "C":         5.5,  # 5-10% Veränderung
    "D":         3.0,  # < 5% Veränderung (unverändert)
    # Spezielle Filing-Typen
    "SC_13D":    10.0, # Strategische Beteiligung > 5%
    "SC_13D_A":   9.0, # Amendment Strategic Stake
    "SC_13G":     7.0, # Passive Beteiligung > 5%
    "SC_13G_A":   6.0, # Amendment Passive Stake
    "FORM4_BUY":  8.5, # Insider-Kauf
    "FORM4_SELL": 4.0, # Insider-Verkauf (bearisch)
}


def check_new_filings(state_manager) -> list:
    """
    Checkt alle relevanten Filing-Typen für alle CIK-Targets.
    Gibt Liste neuer Filings zurück.
    """
    new_filings = []

    for entity, cik in Config.SEC_CIK_TARGETS.items():
        for filing_type, type_info in FILING_TYPES.items():
            try:
                rate_limiter.wait("sec_edgar")
                url  = EDGAR_RSS.format(
                    cik=cik,
                    filing_type=filing_type.replace(" ", "+")
                )
                feed = feedparser.parse(url, request_headers=HEADERS)

                # Letztes bekanntes Datum für diesen Entity+Type
                key       = f"{entity}_{filing_type.replace(' ', '_')}"
                last_date = state_manager.get_last_filing_date(key) or ""

                for entry in feed.entries:
                    filing_date = entry.get("updated", "")
                    if filing_date > last_date:
                        filing_info = {
                            "entity":       entity,
                            "cik":          cik,
                            "filing_type":  filing_type,
                            "filing_date":  filing_date,
                            "filing_url":   entry.link,
                            "title":        entry.title,
                            "weight":       type_info["weight"],
                            "description":  type_info["description"],
                            "signal_strength": _assess_signal_strength(
                                filing_type, entry.title
                            ),
                        }
                        new_filings.append(filing_info)
                        state_manager.update_filing(
                            key, cik, filing_date,
                            entry.link, filing_type
                        )
                        logger.info(
                            f"NEW FILING: {entity} | {filing_type} | "
                            f"{filing_date} | {entry.title[:60]}"
                        )

            except Exception as e:
                logger.warning(f"EDGAR {entity} {filing_type}: {e}")

    return new_filings


def _assess_signal_strength(filing_type: str, title: str) -> str:
    """Bewertet Signalstärke basierend auf Filing-Typ und Titel."""
    title_lower = title.lower()

    if filing_type in ("SC 13D", "SC 13D/A"):
        return "VERY_STRONG"

    if filing_type == "4":
        if any(w in title_lower for w in ["purchase", "buy", "acquisition"]):
            return "STRONG_BUY"
        if any(w in title_lower for w in ["sale", "sell", "disposition"]):
            return "STRONG_SELL"
        return "MODERATE"

    if filing_type in ("13F-HR", "13F-HR/A"):
        return "QUARTERLY_UPDATE"

    if filing_type in ("SC 13G", "SC 13G/A"):
        return "MODERATE_PASSIVE"

    return "UNKNOWN"


def classify_position_delta(current: dict, previous: dict) -> list:
    """
    Klassifiziert 13F Positions-Änderungen in Klassen A-D.
    Gibt sortierte Liste mit Scores zurück.
    """
    classifications = []
    all_tickers = set(list(current.keys()) + list(previous.keys()))

    for ticker in all_tickers:
        curr_shares = current.get(ticker, 0)
        prev_shares = previous.get(ticker, 0)

        if prev_shares == 0 and curr_shares > 0:
            cls   = "A"
            score = FILING_CLASS_SCORES["A_NEW"]
            desc  = "NEW_POSITION"
        elif curr_shares == 0 and prev_shares > 0:
            cls   = "A"
            score = FILING_CLASS_SCORES["A_CLOSED"]
            desc  = "CLOSED_POSITION"
        elif prev_shares > 0:
            change_pct = abs(curr_shares - prev_shares) / prev_shares * 100
            direction  = "INCREASED" if curr_shares > prev_shares else "REDUCED"

            if change_pct > 20:
                cls   = "B"
                score = FILING_CLASS_SCORES["B_LARGE"]
                desc  = f"{direction}_{change_pct:.0f}pct"
            elif change_pct > 10:
                cls   = "B"
                score = FILING_CLASS_SCORES["B_MEDIUM"]
                desc  = f"{direction}_{change_pct:.0f}pct"
            elif change_pct > 5:
                cls   = "C"
                score = FILING_CLASS_SCORES["C"]
                desc  = "MINOR_CHANGE"
            else:
                cls   = "D"
                score = FILING_CLASS_SCORES["D"]
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
        })

    # Klasse A zuerst, dann nach Score
    classifications.sort(key=lambda x: x["score"], reverse=True)
    return classifications


def check_begleittext_for_shulman(text: str) -> dict:
    """Prüft Filing-Begleittext auf Shulman-Konzepte."""
    found = [kw for kw in SHULMAN_KEYWORDS if kw.lower() in text.lower()]
    bonus = Config.SHULMAN_SALP_BEGLEIT_BONUS if len(found) >= 2 else 0.0
    return {
        "keywords_found": found,
        "shulman_bonus":  bonus,
        "relevant":       len(found) >= 2,
    }


def get_form4_signal(filing_url: str) -> dict:
    """
    Liest Form 4 Detail für Insider-Transaktion.
    Gibt Kauf/Verkauf und Volumen zurück.
    """
    try:
        rate_limiter.wait("sec_edgar")
        r = requests.get(filing_url, headers=HEADERS, timeout=15)
        content = r.text.lower()

        is_buy  = any(w in content for w in
                      ["p - purchase", "acquisition", "exercise"])
        is_sell = any(w in content for w in
                      ["s - sale", "disposition", "sold"])

        return {
            "transaction_type": "BUY" if is_buy else "SELL" if is_sell else "UNKNOWN",
            "score": (FILING_CLASS_SCORES["FORM4_BUY"] if is_buy else
                      FILING_CLASS_SCORES["FORM4_SELL"]),
        }
    except Exception as e:
        logger.warning(f"Form 4 detail fetch error: {e}")
        return {"transaction_type": "UNKNOWN", "score": 5.0}


def run_edgar_monitor(state_manager) -> dict:
    """
    Vollständiger EDGAR-Monitor für alle Filing-Typen.
    Gibt strukturiertes Result-Dict zurück.
    """
    logger.info("Running SEC EDGAR monitor (13F + SC13D + SC13G + Form4)")
    new_filings = check_new_filings(state_manager)

    # Nach Signalstärke gruppieren
    very_strong = [f for f in new_filings
                   if f.get("signal_strength") == "VERY_STRONG"]
    strong      = [f for f in new_filings
                   if f.get("signal_strength") in
                   ("STRONG_BUY", "QUARTERLY_UPDATE")]
    moderate    = [f for f in new_filings
                   if f.get("signal_strength") not in
                   ("VERY_STRONG", "STRONG_BUY", "QUARTERLY_UPDATE")]

    # Höchster SALP-Score aus neuen Filings
    salp_score = 3.0  # Default: kein Filing
    if very_strong:
        salp_score = 10.0
    elif strong:
        salp_score = 8.5
    elif moderate:
        salp_score = 6.0

    result = {
        "new_filings_found":  len(new_filings),
        "new_filings":        new_filings,
        "very_strong_signals":very_strong,
        "strong_signals":     strong,
        "moderate_signals":   moderate,
        "checked_entities":   list(Config.SEC_CIK_TARGETS.keys()),
        "checked_types":      list(FILING_TYPES.keys()),
        "salp_score_override":salp_score,
        "trigger_pipeline":   len(new_filings) > 0,
        "checked_at":         datetime.utcnow().isoformat(),
        # Für Scoring-Engine
        "classifications":    [],
    }

    # Output schreiben
    out = Config.SIGNALS_DIR / "sec_filings.json"
    Config.SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))

    if new_filings:
        logger.info(
            f"EDGAR ALERT: {len(new_filings)} new filings | "
            f"Very Strong: {len(very_strong)} | "
            f"Strong: {len(strong)} | "
            f"Moderate: {len(moderate)}"
        )
        for f in new_filings[:5]:
            logger.info(
                f"  → {f['entity']} | {f['filing_type']} | "
                f"{f['signal_strength']} | {f['title'][:50]}"
            )
    else:
        logger.info(
            f"No new filings for: {list(Config.SEC_CIK_TARGETS.keys())}"
        )

    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    logging.basicConfig(level=logging.INFO)
    from scanner.utils.state_manager import StateManager
    with StateManager() as sm:
        result = run_edgar_monitor(sm)
        print(json.dumps(result, indent=2, default=str))
