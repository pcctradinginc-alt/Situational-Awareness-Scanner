"""
main.py
Vollständige Scanner-Pipeline Orchestrierung.
Einstiegspunkt für GitHub Actions und lokalen Betrieb.

WICHTIG: EDGAR läuft VOR dem Datenfetch damit Filing-Ticker
automatisch in die Tradier-Abfrage aufgenommen werden.

Usage:
    python main.py                      # Vollständiger Daily Run
    python main.py --edgar-only         # Nur EDGAR-Check
    python main.py --ticker VST PLTR    # Spezifische Ticker
    python main.py --no-claude          # Nur Daten, kein Claude
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def run_full_pipeline(args=None):
    from scanner.utils.config import Config
    from scanner.utils.state_manager import StateManager
    from scanner.sources.data_fetcher import DataFetcher
    from scanner.sources.sec_edgar import run_edgar_monitor
    from scanner.signals.regime_detector import RegimeDetector
    from scanner.analysis.claude_analyzer import ClaudeAnalyzer
    from scanner.output.trading_card_generator import generate_all_cards
    from scanner.output.dashboard_generator import build_dashboard
    from scanner.output.email_notifier import (
        load_todays_cards, send_email
    )

    missing = Config.validate()
    if missing:
        logger.warning(f"Missing API keys: {missing}")

    Config.ensure_dirs()

    errors = []
    start  = datetime.utcnow()
    logger.info(f"=== SA SCANNER START: {start.isoformat()} ===")

    with StateManager() as sm:
        try:
            # ── 1. SEC EDGAR MONITOR ─────────────────────────────
            # MUSS vor dem Datenfetch laufen damit Filing-Ticker
            # in die Tradier-Abfrage aufgenommen werden
            logger.info("Step 1: SEC EDGAR monitor")
            sec_data = run_edgar_monitor(sm)

            if args and args.edgar_only:
                logger.info("--edgar-only: stopping after EDGAR check")
                sm.commit_state("EDGAR-only run")
                return

            # ── 2. FILING-TICKER IN TARGET_TICKERS INJIZIEREN ────
            # Klasse A und B Ticker aus neuen Filings automatisch
            # hinzufügen damit Tradier-Daten für sie abgerufen werden
            filing_tickers = []
            for cls in sec_data.get("classifications", []):
                ticker = cls.get("ticker", "")
                if (cls.get("class") in ["A", "B"] and
                        ticker and
                        ticker not in Config.TARGET_TICKERS):
                    filing_tickers.append(ticker)

            # Auch Ticker aus sehr starken Signalen (SC 13D, Form4)
            for signal in sec_data.get("very_strong_signals", []):
                title = signal.get("title", "")
                # Einfacher Ticker-Extraktor aus Filing-Titel
                import re
                found = re.findall(r'\b([A-Z]{2,5})\b', title)
                for t in found:
                    if (len(t) >= 2 and
                            t not in Config.TARGET_TICKERS and
                            t not in ["SEC", "LLC", "INC", "CORP",
                                      "LTD", "LP", "ETF", "USA"]):
                        filing_tickers.append(t)

            if filing_tickers:
                filing_tickers = list(set(filing_tickers))
                logger.info(
                    f"Filing-Ticker injected into TARGET_TICKERS: "
                    f"{filing_tickers}"
                )
                Config.TARGET_TICKERS = list(
                    set(Config.TARGET_TICKERS + filing_tickers)
                )

            # Ticker-Override aus CLI
            if args and args.ticker:
                Config.TARGET_TICKERS = args.ticker
                logger.info(f"Ticker override: {args.ticker}")

            logger.info(
                f"Active TARGET_TICKERS ({len(Config.TARGET_TICKERS)}): "
                f"{Config.TARGET_TICKERS}"
            )

            # ── 3. DATEN FETCHEN ──────────────────────────────────
            # Jetzt mit allen Tickern inkl. Filing-Ticker
            logger.info("Step 2: Data fetch")
            fetcher  = DataFetcher(sm)
            all_data = fetcher.fetch_all(sm)

            # ── 4. REGIME BESTIMMEN ───────────────────────────────
            logger.info("Step 3: Regime detection")
            detector = RegimeDetector()
            regime   = detector.detect(all_data, sm)

            logger.info(
                f"Regime: {regime['mode']} | "
                f"IV-Rank: {regime.get('iv_rank_avg', 50):.1f}% | "
                f"Energy: {regime.get('energy_breadth', 0.5):.0%} | "
                f"Threshold: {regime.get('conviction_threshold', 7.5)}"
            )

            # ── 5. CLAUDE ANALYSE ─────────────────────────────────
            cards = []
            if not (args and args.no_claude):
                logger.info("Step 4: Claude analysis")
                analyzer = ClaudeAnalyzer()
                cards    = analyzer.run_daily_analysis(
                    all_data, regime, sec_data, sm
                )
            else:
                logger.info("Step 4: Skipped (--no-claude)")

            # ── 6. TRADING CARDS GENERIEREN ───────────────────────
            logger.info("Step 5: Trading card generation")
            import sqlite3
            conn = sqlite3.connect(str(Config.DB_PATH))
            conn.row_factory = sqlite3.Row
            today    = datetime.utcnow().date().isoformat()
            db_cards = conn.execute(
                """SELECT card_json FROM trading_cards
                   WHERE date = ?
                   AND gate_status IN ('PASS', 'WATCHLIST')""",
                (today,)
            ).fetchall()
            conn.close()

            all_cards = [json.loads(r["card_json"]) for r in db_cards]
            n_cards   = generate_all_cards(all_cards)
            logger.info(f"Generated {n_cards} HTML cards")

            # ── 7. DASHBOARD ──────────────────────────────────────
            logger.info("Step 6: Dashboard generation")
            build_dashboard(sm, regime)

            # ── 8. EMAIL BENACHRICHTIGUNG ─────────────────────────
            # Nur wenn PASS-Cards vorhanden
            pass_cards = [
                c for c in all_cards
                if c.get("conviction_gate") == "PASS"
            ]
            if pass_cards:
                logger.info(
                    f"Step 7: Sending email for {len(pass_cards)} PASS cards"
                )
                try:
                    send_email(pass_cards, regime)
                except Exception as e:
                    logger.error(f"Email error (non-fatal): {e}")
                    errors.append(f"email: {e}")
            else:
                logger.info("Step 7: No PASS cards — email skipped")

            # ── 9. SUMMARY ────────────────────────────────────────
            duration = (datetime.utcnow() - start).total_seconds()
            logger.info(
                f"=== SA SCANNER DONE: {duration:.0f}s | "
                f"Cards: {n_cards} | "
                f"PASS: {len(pass_cards)} | "
                f"Errors: {len(errors)} | "
                f"Regime: {regime['mode']} | "
                f"Tickers: {len(Config.TARGET_TICKERS)} ==="
            )

            # GitHub Actions Environment Variable für Commit-Message
            try:
                env_file = Path("/tmp/scanner_stats.env")
                env_file.write_text(
                    f"SIGNAL_COUNT={len(pass_cards)}\n"
                    f"REGIME_MODE={regime['mode']}\n"
                    f"CANDIDATE_COUNT={len(Config.TARGET_TICKERS)}\n"
                )
            except Exception:
                pass

            # ── 10. GIT COMMIT ────────────────────────────────────
            extra = (
                f"Regime: {regime['mode']} | "
                f"Cards: {n_cards} | "
                f"PASS: {len(pass_cards)}"
            )
            if sec_data.get("new_filings_found", 0) > 0:
                extra += (
                    f" | 13F ALERT: "
                    f"{sec_data['new_filings_found']} new filings"
                )
            if filing_tickers:
                extra += f" | Filing-Ticker: {filing_tickers}"

            sm.commit_state(extra)

        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            errors.append(str(e))
            try:
                sm.commit_state(f"Error run: {str(e)[:100]}")
            except Exception:
                pass
            sys.exit(1)


def run_edgar_only():
    from scanner.utils.state_manager import StateManager
    from scanner.sources.sec_edgar import run_edgar_monitor

    logger.info("=== EDGAR-ONLY RUN ===")
    with StateManager() as sm:
        result = run_edgar_monitor(sm)
        if result.get("trigger_pipeline"):
            logger.info(
                "New filings detected — triggering full pipeline"
            )
            run_full_pipeline()
        else:
            sm.commit_state("EDGAR check — no new filings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SA Scanner")
    parser.add_argument(
        "--edgar-only", action="store_true",
        help="Run only EDGAR 13F check"
    )
    parser.add_argument(
        "--no-claude", action="store_true",
        help="Skip Claude analysis (data fetch only)"
    )
    parser.add_argument(
        "--ticker", nargs="+",
        help="Analyze specific tickers only"
    )
    args = parser.parse_args()

    if args.edgar_only:
        run_edgar_only()
    else:
        run_full_pipeline(args)
