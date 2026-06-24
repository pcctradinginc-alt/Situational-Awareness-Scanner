"""
tests/test_api_connections.py
Testet API-Verbindungen ohne echte Calls (Mocking).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock


class TestTradierClient:
    def test_liquidity_check_pass(self):
        from scanner.sources.tradier_client import TradierClient
        client = TradierClient()

        mock_chain = [{
            "strike": 140.0,
            "option_type": "call",
            "volume": 1000,
            "bid": 8.0,
            "ask": 8.20,
            "open_interest": 5000,
            "greeks": {"delta": 0.42, "vega": 0.10, "theta": -0.03, "gamma": 0.02},
        }]

        with patch.object(client, 'get_option_chain', return_value=mock_chain), \
             patch.object(client, 'get_quote', return_value={"last": 130.0}):
            result = client.check_liquidity("VST", 140.0, "2026-09-19")
            assert result["passed"] is True
            assert result["volume"] == 1000

    def test_liquidity_check_fail_volume(self):
        from scanner.sources.tradier_client import TradierClient
        client = TradierClient()

        mock_chain = [{
            "strike": 140.0,
            "option_type": "call",
            "volume": 50,          # Unter MIN_DAILY_VOLUME=500
            "bid": 8.0,
            "ask": 8.20,
            "open_interest": 500,
            "greeks": {"delta": 0.42, "vega": 0.05, "theta": -0.02, "gamma": 0.01},
        }]

        with patch.object(client, 'get_option_chain', return_value=mock_chain), \
             patch.object(client, 'get_quote', return_value={"last": 130.0}):
            result = client.check_liquidity("VST", 140.0, "2026-09-19")
            assert result["passed"] is False
            assert any("Volume" in issue for issue in result["issues"])

    def test_put_call_ratio_calculation(self):
        from scanner.sources.tradier_client import TradierClient
        client = TradierClient()

        mock_calls = [{"volume": 1000, "open_interest": 5000, "option_type": "call"}]
        mock_puts  = [{"volume": 500,  "open_interest": 2000, "option_type": "put"}]

        with patch.object(client, 'get_expirations', return_value=["2026-06-20", "2026-09-19"]), \
             patch.object(client, 'get_option_chain',
                          side_effect=lambda t, e, ot: mock_calls if ot == "call" else mock_puts):
            result = client.get_options_statistics("VST")
            assert result["put_call_volume"] is not None
            assert result["put_call_volume"] == pytest.approx(0.5, abs=0.01)
            assert result["options_bullish"] is True


class TestStateManager:
    def test_iv_rank_warmup(self, tmp_path):
        from scanner.utils.state_manager import StateManager
        sm = StateManager(db_path=tmp_path / "test.db")

        # Weniger als MIN_DATAPOINTS → INSUFFICIENT_DATA (R-03 FIX: kein
        # verzerrender 50.0-Default mehr; iv_rank ist None und der
        # RegimeDetector behandelt fehlende Daten explizit als neutral).
        result = sm.get_iv_rank("VST", 0.35)
        assert result["confidence"] == "INSUFFICIENT_DATA"
        assert result["iv_rank"] is None
        sm.close()

    def test_iv_rank_calculation(self, tmp_path):
        from scanner.utils.state_manager import StateManager
        from datetime import date, timedelta
        sm = StateManager(db_path=tmp_path / "test.db")

        # 50 historische IV-Werte einfügen
        base = date.today()
        for i in range(50):
            d  = (base - timedelta(days=i)).isoformat()
            iv = 0.20 + i * 0.003  # Steigend: 0.20 bis 0.347
            sm.conn.execute(
                "INSERT OR REPLACE INTO iv_history (date, ticker, iv) VALUES (?, ?, ?)",
                (d, "VST", iv)
            )
        sm.conn.commit()

        # IV von 0.20 sollte Rank ~0% haben (niedrigste)
        result_low = sm.get_iv_rank("VST", 0.19)
        assert result_low["iv_rank"] < 10

        # IV von 0.35 sollte hohen Rank haben
        result_high = sm.get_iv_rank("VST", 0.35)
        assert result_high["iv_rank"] > 90

        sm.close()

    def test_katechon_bonus_quarterly_limit(self, tmp_path):
        from scanner.utils.state_manager import StateManager
        sm = StateManager(db_path=tmp_path / "test.db")

        # Erster Bonus sollte möglich sein
        assert sm.can_use_katechon_bonus() is True
        sm.use_katechon_bonus()

        # Zweiter im selben Quartal nicht
        assert sm.can_use_katechon_bonus() is False
        sm.close()

    def test_portfolio_limits(self, tmp_path):
        from scanner.utils.state_manager import StateManager
        from datetime import datetime
        sm = StateManager(db_path=tmp_path / "test.db")

        # 3 Positionen einfügen
        for t in ["VST", "CEG", "NRG"]:
            sm.conn.execute(
                """INSERT INTO active_positions
                   (ticker, sector, open_date, conviction_at_open,
                    laufzeit_months, status)
                   VALUES (?, ?, ?, ?, ?, 'OPEN')""",
                (t, "energy_infrastructure",
                 datetime.utcnow().date().isoformat(), 8.5, 9)
            )
        sm.conn.commit()

        ok, reason = sm.check_portfolio_limits("PLTR", "sovereign_ai_defense")
        assert ok is False
        assert "MAX_3" in reason
        sm.close()


class TestRegimeDetector:
    def test_normal_mode(self):
        from scanner.signals.regime_detector import RegimeDetector

        class MockSM:
            def get_capex_trend(self): return "rising"
            def store_regime(self, r): pass

        det = RegimeDetector()
        all_data = {
            "energy_breadth": {"energy_breadth": 0.70},
            "hyperscaler_capex": {"capex_trend": "rising"},
            "options": {
                "VST": {"iv_rank": {"iv_rank": 35.0, "confidence": "HIGH"}},
            },
            "eia": {"growth_yoy": 0.06},
            "fred": {},
        }
        result = det.detect(all_data, MockSM())
        assert result["mode"] == "NORMAL"
        assert result["conviction_threshold"] == 7.5

    def test_stress_mode_iv(self):
        from scanner.signals.regime_detector import RegimeDetector

        class MockSM:
            def get_capex_trend(self): return "stable"
            def store_regime(self, r): pass

        det = RegimeDetector()
        all_data = {
            "energy_breadth": {"energy_breadth": 0.40},
            "hyperscaler_capex": {"capex_trend": "stable"},
            "options": {
                "VST": {"iv_rank": {"iv_rank": 72.0, "confidence": "HIGH"}},
            },
            "eia": {"growth_yoy": 0.01},
            "fred": {},
        }
        result = det.detect(all_data, MockSM())
        assert result["mode"] == "STRESS"
        assert result["conviction_threshold"] == 8.0


class TestContrarianGate:
    def test_gate_blocked(self):
        from scanner.signals.contrarian_gate import ContrarianGate
        gate = ContrarianGate()

        articles = [
            {"title": "AI winter approaching as software efficiency dominates",
             "summary": "New models show compute efficient approaches reducing GPU demand software overhang",
             "tickers": [], "credibility": 0.85},
            {"title": "AI regulation moratorium proposed by EU",
             "summary": "European AI ban ai moratorium could restrict frontier models",
             "tickers": [], "credibility": 0.90},
            {"title": "Scaling plateau reached according to researchers",
             "summary": "AI slower than expected diminishing returns ai winter",
             "tickers": [], "credibility": 0.88},
        ]

        result = gate.evaluate(articles, "VST", {})
        # 3 Gegenthesen → score ≤ -4 → blocked
        assert result["gate_blocked"] is True
        assert result["contrarian_score"] <= -3.0

    def test_gate_open_no_gegenthesen(self):
        from scanner.signals.contrarian_gate import ContrarianGate
        gate = ContrarianGate()

        articles = [
            {"title": "Palantir wins Pentagon contract",
             "summary": "Defense AI contract government sovereign systems",
             "tickers": ["PLTR"], "credibility": 0.92},
        ]

        result = gate.evaluate(articles, "PLTR",
                               {"PLTR": {"rsi": 55.0, "options_flow": {}}})
        assert result["gate_blocked"] is False
        assert result["contrarian_score"] >= -3.0
