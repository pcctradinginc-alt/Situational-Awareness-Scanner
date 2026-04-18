"""
main.py
Vollständige Scanner-Pipeline Orchestrierung.
Einstiegspunkt für GitHub Actions und lokalen Betrieb.

WICHTIG: EDGAR läuft VOR dem Datenfetch damit Filing-Ticker
automatisch dauerhaft in den Ticker-Pool übernommen werden.

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
            logger.info("Step 1: SEC EDGAR monitor")
            sec_data = run_edgar_monitor(sm)

            if args and args.edgar_only:
                logger.info("--edgar-only: stopping after EDGAR check")
                sm.commit_state("EDGAR-only run")
                return

            # ── 2. EDGAR → DAUERHAFTE TICKER-VERWALTUNG (neu) ─────────────────────
            logger.info("Step 2: EDGAR → dynamische Ticker-Update")
            filing_tickers = []
            closed_tickers = []

            for cls in sec_data.get("classifications", []):
                ticker = cls.get("ticker", "").strip()
                if not ticker:
                    continue
                if cls.get("class") in ["A", "B"]:                    # NEW oder INCREASED
                    if ticker not in filing_tickers:
                        filing_tickers.append(ticker)
                elif cls.get("class") == "CLOSED_POSITION" or "CLOSED" in cls.get("description", "").upper():
                    if ticker not in closed_tickers:
                        closed_tickers.append(ticker)

            # Dynamische Liste persistent aktualisieren
            if filing_tickers or closed_tickers:
                Config.ensure_dirs()
                dynamic_path = Config.DYNAMIC_TICKERS_PATH
                
                if dynamic_path.exists():
                    dynamic_data = json.loads(dynamic_path.read_text())
                else:
                    dynamic_data = {"tickers": [], "last_updated": "", "source": "edgar_monitor"}

                current_dynamic = set(dynamic_data.get("tickers", []))

                # Neue Positionen dauerhaft hinzufügen
                for t in filing_tickers:
                    if t not in current_dynamic:
                        current_dynamic.add(t)
                        logger.info(f"✅ DAUERHAFT hinzugefügt: {t} (Aschenbrenner / Smart-Money)")

                # Verkaufte Positionen dauerhaft entfernen
                for t in closed_tickers:
                    if t in current_dynamic:
                        current_dynamic.remove(t)
                        logger.info(f"❌ DAUERHAFT entfernt: {t} (Aschenbrenner hat verkauft)")

                dynamic_data["tickers"] = sorted(list(current_dynamic))
                dynamic_data["last_updated"] = datetime.utcnow().isoformat()
                dynamic_data["source"] = "edgar_monitor"

                dynamic_path.write_text(json.dumps(dynamic_data, indent=2))
                logger.info(f"Dynamische Ticker-Liste aktualisiert → {len(dynamic_data['tickers'])} Ticker")

            # ── 3. Alle Ticker (fest + dynamisch) für diesen Run verwenden ───────
            Config.TARGET_TICKERS = Config.get_all_target_tickers()
            logger.info(
                f"Active TARGET_TICKERS ({len(Config.TARGET_TICKERS)}): "
                f"{Config.TARGET_TICKERS}"
            )

            # ── 4. DATEN FETCHEN ──────────────────────────────────
            logger.info("Step 3: Data fetch")
            fetcher  = DataFetcher(sm)
            all_data = fetcher.fetch_all(sm)

            # ── 5. REGIME BESTIMMEN ───────────────────────────────
            logger.info("Step 4: Regime detection")
            detector = RegimeDetector()
            regime   = detector.detect(all_data, sm)

            iv_avg = regime.get('iv_rank_avg')
            iv_str = f"{iv_avg:.1f}%" if iv_avg is not None else "N/A (warmup)"
            logger.info(
                f"Regime: {regime['mode']} | "
                f"IV-Rank: {iv_str} | "
                f"Energy: {regime.get('energy_breadth', 0.5):.0%} | "
                f"Threshold: {regime.get('conviction_threshold', 7.5)}"
            )

            # ── 6. CLAUDE ANALYSE ─────────────────────────────────
            cards = []
            if not (args and args.no_claude):
                logger.info("Step 5: Claude analysis")
                analyzer = ClaudeAnalyzer()
                cards    = analyzer.run_daily_analysis(
                    all_data, regime, sec_data, sm
                )
            else:
                logger.info("Step 5: Skipped (--no-claude)")

            # ── 7. TRADING CARDS GENERIEREN ───────────────────────
            logger.info("Step 6: Trading card generation")
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

            # ── 8. DASHBOARD ──────────────────────────────────────
            logger.info("Step 7: Dashboard generation")
            build_dashboard(sm, regime)

            # ── 9. EMAIL BENACHRICHTIGUNG ─────────────────────────
            pass_cards = [
                c for c in all_cards
                if c.get("conviction_gate") == "PASS"
            ]
            if pass_cards:
                logger.info(
                    f"Step 8: Sending email for {len(pass_cards)} PASS cards"
                )
                try:
                    send_email(pass_cards, regime)
                except Exception as e:
                    logger.error(f"Email error (non-fatal): {e}")
                    errors.append(f"email: {e}")
            else:
                logger.info("Step 8: No PASS cards — email skipped")

            # ── 10. SUMMARY ────────────────────────────────────────
            duration = (datetime.utcnow() - start).total_seconds()
            logger.info(
                f"=== SA SCANNER DONE: {duration:.0f}s | "
                f"Cards: {n_cards} | "
                f"PASS: {len(pass_cards)} | "
                f"Errors: {len(errors)} | "
                f"Regime: {regime['mode']} | "
                f"Tickers: {len(Config.TARGET_TICKERS)} ==="
            )

            # GitHub Actions Environment Variable
            try:
                env_file = Path("/tmp/scanner_stats.env")
                env_file.write_text(
                    f"SIGNAL_COUNT={len(pass_cards)}\n"
                    f"REGIME_MODE={regime['mode']}\n"
                    f"CANDIDATE_COUNT={len(Config.TARGET_TICKERS)}\n"
                )
            except Exception:
                pass

            # ── 11. GIT COMMIT ────────────────────────────────────
            extra = (
                f"Regime: {regime['mode']} | "
                f"Cards: {n_cards} | "
                f"PASS: {len(pass_cards)}"
            )
            if sec_data.get("new_filings_found", 0) > 0:
                extra += f" | 13F ALERT: {sec_data['new_filings_found']} new filings"
            if filing_tickers:
                extra += f" | New dynamic tickers: {filing_tickers}"
            if closed_tickers:
                extra += f" | Removed tickers: {closed_tickers}"

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
            logger.info("New filings detected — triggering full pipeline")
            run_full_pipeline()
        else:
            sm.commit_state("EDGAR check — no new filings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SA Scanner")
    parser.add_argument("--edgar-only", action="store_true", help="Run only EDGAR 13F check")
    parser.add_argument("--no-claude", action="store_true", help="Skip Claude analysis (data fetch only)")
    parser.add_argument("--ticker", nargs="+", help="Analyze specific tickers only")
    args = parser.parse_args()

    if args.edgar_only:
        run_edgar_only()
    else:
        run_full_pipeline(args)
