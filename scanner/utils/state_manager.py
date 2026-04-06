"""
scanner/utils/state_manager.py
SQLite als persistenter State + automatischer Git-Commit.
"""

import sqlite3
import subprocess
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        self._log_run_start()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT,
                date            TEXT,
                ticker          TEXT,
                conviction      REAL,
                gate_status     TEXT,
                regime_mode     TEXT,
                bottleneck_type TEXT,
                full_json       TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trading_cards (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT,
                date            TEXT,
                ticker          TEXT,
                conviction      REAL,
                gate_status     TEXT,
                laufzeit_months INTEGER,
                card_json       TEXT,
                html_path       TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS active_positions (
                ticker                  TEXT PRIMARY KEY,
                sector                  TEXT,
                open_date               TEXT,
                conviction_at_open      REAL,
                laufzeit_months         INTEGER,
                strike_pct_otm          REAL,
                entry_premium           REAL,
                expiration_date         TEXT,
                status                  TEXT DEFAULT 'OPEN',
                checkpoint_90d_done     INTEGER DEFAULT 0,
                checkpoint_180d_done    INTEGER DEFAULT 0,
                last_monthly_review     TEXT,
                stop_thesis_trigger     TEXT,
                updated_at              TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS iv_history (
                date    TEXT,
                ticker  TEXT,
                iv      REAL,
                source  TEXT DEFAULT 'tradier',
                PRIMARY KEY (date, ticker)
            );
            CREATE TABLE IF NOT EXISTS regime_history (
                date                    TEXT PRIMARY KEY,
                mode                    TEXT,
                iv_rank_avg             REAL,
                energy_breadth          REAL,
                capex_trend             TEXT,
                regime_stability        REAL,
                conviction_threshold    REAL,
                run_id                  TEXT
            );
            CREATE TABLE IF NOT EXISTS filing_tracker (
                entity              TEXT PRIMARY KEY,
                cik                 TEXT,
                last_filing_date    TEXT,
                last_filing_url     TEXT,
                last_filing_class   TEXT,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS katechon_tracker (
                quarter     TEXT PRIMARY KEY,
                bonus_used  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS run_log (
                run_id          TEXT PRIMARY KEY,
                started_at      TEXT,
                completed_at    TEXT,
                regime_mode     TEXT,
                candidates      INTEGER DEFAULT 0,
                claude_calls    INTEGER DEFAULT 0,
                cards_generated INTEGER DEFAULT 0,
                errors          TEXT,
                git_committed   INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_iv_ticker_date
                ON iv_history(ticker, date);
            CREATE INDEX IF NOT EXISTS idx_signals_date
                ON signals(date, ticker);
        """)
        self.conn.commit()

    def _log_run_start(self):
        self.conn.execute(
            "INSERT OR IGNORE INTO run_log (run_id, started_at) VALUES (?, ?)",
            (self._run_id, datetime.utcnow().isoformat())
        )
        self.conn.commit()

    # ── IV HISTORY ───────────────────────────────────────────────

    def store_iv(self, ticker: str, iv: float, source: str = "tradier"):
        today = datetime.utcnow().date().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO iv_history (date, ticker, iv, source) VALUES (?, ?, ?, ?)",
            (today, ticker, iv, source)
        )
        self.conn.commit()

    def get_iv_rank(self, ticker: str, current_iv: float) -> dict:
        cutoff = (datetime.utcnow() - timedelta(days=Config.IV_RANK_LOOKBACK_DAYS)).date().isoformat()
        rows = self.conn.execute(
            "SELECT iv FROM iv_history WHERE ticker = ? AND date >= ? ORDER BY iv",
            (ticker, cutoff)
        ).fetchall()
        data_points = len(rows)

        if data_points < Config.IV_RANK_MIN_DATAPOINTS:
            # R-03 FIX: None statt IV_RANK_WARMUP_DEFAULT=50.0
            # RegimeDetector muss explizit mit fehlenden Daten umgehen
            # statt einen verzerrenden Default-Wert zu verarbeiten
            return {
                "iv_rank":     None,  # Kein Default — RegimeDetector entscheidet
                "confidence":  "INSUFFICIENT_DATA",
                "data_points": data_points,
                "warning": (
                    f"Only {data_points} datapoints. "
                    f"IV-Rank reliable from {Config.IV_RANK_MIN_DATAPOINTS}. "
                    f"Regime score set to neutral=5.0."
                ),
            }

        confidence = (
            "HIGH"   if data_points >= Config.IV_RANK_HIGH_CONFIDENCE else
            "MEDIUM" if data_points >= 100 else
            "LOW"
        )
        ivs  = [r["iv"] for r in rows]
        rank = sum(1 for iv in ivs if iv < current_iv) / len(ivs) * 100

        return {
            "iv_rank":     round(rank, 1),
            "confidence":  confidence,
            "data_points": data_points,
            "iv_min_52w":  round(min(ivs), 3),
            "iv_max_52w":  round(max(ivs), 3),
            "current_iv":  current_iv,
        }

    # ── REGIME ───────────────────────────────────────────────────

    def store_regime(self, regime: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO regime_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.utcnow().date().isoformat(),
                regime["mode"],
                regime.get("iv_rank_avg", 50),
                regime.get("energy_breadth", 0.5),
                regime.get("capex_trend", "unknown"),
                regime.get("regime_stability", 0.5),
                regime.get("conviction_threshold", 7.5),
                self._run_id,
            )
        )
        self.conn.commit()

    def get_capex_trend(self) -> str:
        rows = self.conn.execute(
            "SELECT capex_trend FROM regime_history ORDER BY date DESC LIMIT ?",
            (Config.CAPEX_FALLING_QUARTERS_FOR_STRESS,)
        ).fetchall()
        if len(rows) < Config.CAPEX_FALLING_QUARTERS_FOR_STRESS:
            return "unknown"
        if all(r["capex_trend"] == "falling" for r in rows):
            return "falling_two_quarters"
        return rows[0]["capex_trend"] if rows else "unknown"

    def get_regime_trend(self, days: int = 30) -> dict:
        cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
        rows = self.conn.execute(
            """SELECT mode, COUNT(*) as count
               FROM regime_history
               WHERE date >= ?
               GROUP BY mode""",
            (cutoff,)
        ).fetchall()
        total = sum(r["count"] for r in rows)
        if total == 0:
            return {"trend": "UNKNOWN", "stress_pct": 0, "normal_pct": 100}
        stress_count = next((r["count"] for r in rows if r["mode"] == "STRESS"), 0)
        stress_pct = stress_count / total * 100
        return {
            "trend":         "DETERIORATING" if stress_pct > 30 else "STABLE",
            "stress_pct":    round(stress_pct, 1),
            "normal_pct":    round(100 - stress_pct, 1),
            "days_analyzed": days,
        }

    # ── FILING TRACKER ────────────────────────────────────────────

    def get_last_filing_date(self, entity: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT last_filing_date FROM filing_tracker WHERE entity = ?",
            (entity,)
        ).fetchone()
        return row["last_filing_date"] if row else None

    def update_filing(self, entity: str, cik: str, date: str,
                      url: str, cls: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO filing_tracker
               (entity, cik, last_filing_date, last_filing_url,
                last_filing_class, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity, cik, date, url, cls, datetime.utcnow().isoformat())
        )
        self.conn.commit()

    # ── KATECHON TRACKER ──────────────────────────────────────────

    def can_use_katechon_bonus(self) -> bool:
        quarter = f"{datetime.utcnow().year}-Q{(datetime.utcnow().month - 1) // 3 + 1}"
        row = self.conn.execute(
            "SELECT bonus_used FROM katechon_tracker WHERE quarter = ?",
            (quarter,)
        ).fetchone()
        if not row:
            return True
        return row["bonus_used"] < Config.KATECHON_BONUS_PER_QUARTER

    def use_katechon_bonus(self):
        quarter = f"{datetime.utcnow().year}-Q{(datetime.utcnow().month - 1) // 3 + 1}"
        self.conn.execute(
            "INSERT INTO katechon_tracker (quarter, bonus_used) VALUES (?, 1) "
            "ON CONFLICT(quarter) DO UPDATE SET bonus_used = bonus_used + 1",
            (quarter,)
        )
        self.conn.commit()

    # ── SIGNALS & CARDS ───────────────────────────────────────────

    def store_signal(self, ticker: str, conviction: float,
                     gate_status: str, regime_mode: str,
                     bottleneck_type: str, full_data: dict):
        self.conn.execute(
            """INSERT INTO signals
               (run_id, date, ticker, conviction, gate_status,
                regime_mode, bottleneck_type, full_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._run_id,
                datetime.utcnow().date().isoformat(),
                ticker, conviction, gate_status,
                regime_mode, bottleneck_type,
                json.dumps(full_data),
            )
        )
        self.conn.commit()

    def store_trading_card(self, ticker: str, conviction: float,
                           gate_status: str, laufzeit: int,
                           card_data: dict, html_path: str = None):
        self.conn.execute(
            """INSERT INTO trading_cards
               (run_id, date, ticker, conviction, gate_status,
                laufzeit_months, card_json, html_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._run_id,
                datetime.utcnow().date().isoformat(),
                ticker, conviction, gate_status,
                laufzeit, json.dumps(card_data), html_path,
            )
        )
        self.conn.commit()

    def get_active_positions(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM active_positions WHERE status = 'OPEN' ORDER BY open_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def check_portfolio_limits(self, new_ticker: str,
                                new_sector: str) -> tuple:
        positions = self.get_active_positions()
        if len(positions) >= Config.MAX_ACTIVE_POSITIONS:
            return False, "MAX_3_POSITIONS_REACHED"
        same_sector = sum(
            1 for p in positions if p.get("sector") == new_sector
        )
        if same_sector >= Config.MAX_SAME_SECTOR_POSITIONS:
            return False, "SECTOR_CONCENTRATION_RISK"
        if positions:
            avg = sum(p["conviction_at_open"] for p in positions) / len(positions)
            if avg < Config.MIN_PORTFOLIO_CONVICTION:
                return False, "PORTFOLIO_AVG_CONVICTION_TOO_LOW"
        return True, "OK"

    # ── RUN LOG ───────────────────────────────────────────────────

    def log_run_stats(self, candidates: int, claude_calls: int,
                      cards_generated: int, regime_mode: str,
                      errors: list = None):
        self.conn.execute(
            """UPDATE run_log SET
               completed_at    = ?,
               regime_mode     = ?,
               candidates      = ?,
               claude_calls    = ?,
               cards_generated = ?,
               errors          = ?
               WHERE run_id = ?""",
            (
                datetime.utcnow().isoformat(),
                regime_mode, candidates,
                claude_calls, cards_generated,
                json.dumps(errors or []),
                self._run_id,
            )
        )
        self.conn.commit()

    # ── GIT COMMIT ────────────────────────────────────────────────

    def commit_state(self, extra_msg: str = "") -> bool:
        try:
            env = os.environ.copy()
            env["GIT_AUTHOR_NAME"]     = "SA Scanner"
            env["GIT_AUTHOR_EMAIL"]    = "scanner@github-actions.local"
            env["GIT_COMMITTER_NAME"]  = "SA Scanner"
            env["GIT_COMMITTER_EMAIL"] = "scanner@github-actions.local"

            subprocess.run(
                ["git", "add", "data/", "dashboard/"],
                check=True, env=env, capture_output=True
            )

            diff = subprocess.run(
                ["git", "diff", "--staged", "--quiet"], env=env
            )
            if diff.returncode == 0:
                logger.info("No changes to commit")
                return True

            row = self.conn.execute(
                """SELECT cards_generated, claude_calls, candidates
                   FROM run_log WHERE run_id = ?""",
                (self._run_id,)
            ).fetchone()

            msg = (
                f"Scanner {self._run_id} | "
                f"Cards: {row['cards_generated'] if row else 0} | "
                f"Claude: {row['claude_calls'] if row else 0} | "
                f"Candidates: {row['candidates'] if row else 0}"
            )
            if extra_msg:
                msg += f" | {extra_msg}"

            subprocess.run(
                ["git", "commit", "-m", msg],
                check=True, env=env, capture_output=True
            )
            subprocess.run(
                ["git", "push"],
                check=True, env=env, capture_output=True
            )

            self.conn.execute(
                "UPDATE run_log SET git_committed = 1 WHERE run_id = ?",
                (self._run_id,)
            )
            self.conn.commit()
            logger.info(f"State committed: {msg}")
            return True

        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if e.stderr else str(e)
            logger.error(f"Git commit failed: {stderr}")
            # R-05 FIX: Kein sys.exit() — SQLite-Daten sind konsistent gespeichert.
            # Git-Fehler (z.B. Netzwerk-Timeout) dürfen Pipeline nicht abbrechen.
            # GitHub Actions markiert nur den Git-Schritt als failed, nicht die Analyse.
            logger.warning(
                "Git push failed — analysis results are saved in SQLite. "
                "Dashboard update will occur on next successful run."
            )
            return False
        except Exception as e:
            logger.error(f"Commit error: {e}")
            return False

    # ── CONTEXT MANAGER ───────────────────────────────────────────

    def close(self):
        if hasattr(self, "conn") and self.conn:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
