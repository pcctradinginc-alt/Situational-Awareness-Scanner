"""Tests for the 13F parser, change detection and conviction scoring."""
from pathlib import Path

from call_options_intel.edgar_13f import ThirteenFParser, ThirteenFMonitor
from call_options_intel.config_loader import AppConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_information_table_xml():
    xml = (FIXTURES / "sample_13f_infotable.xml").read_text()
    holdings = ThirteenFParser().parse_information_table_xml(xml, "TestFund")
    by_t = {h.ticker: h for h in holdings}
    assert "NVDA" in by_t and "MU" in by_t
    assert by_t["NVDA"].shares == 150000
    # portfolio_pct computed from relative value (18M of 24M)
    assert 0.7 < by_t["NVDA"].portfolio_pct < 0.8


def test_parse_json_holdings_portfolio_pct():
    data = {"manager": "F", "holdings": [
        {"ticker": "NVDA", "shares": 100, "value_usd": 75},
        {"ticker": "MU", "shares": 100, "value_usd": 25},
    ]}
    h = ThirteenFParser().parse_json_holdings(data)
    pct = {x.ticker: x.portfolio_pct for x in h}
    assert abs(pct["NVDA"] - 0.75) < 1e-9


def test_detect_changes_actions():
    p = ThirteenFParser()
    prior = p.parse_json_holdings({"manager": "F", "holdings": [
        {"ticker": "NVDA", "shares": 100}, {"ticker": "TSM", "shares": 50},
        {"ticker": "MU", "shares": 100}]})
    current = p.parse_json_holdings({"manager": "F", "holdings": [
        {"ticker": "NVDA", "shares": 150},  # add +50%
        {"ticker": "MU", "shares": 60},     # reduce -40%
        {"ticker": "CEG", "shares": 10}]})  # new; TSM exited
    ch = p.detect_changes(current, prior)
    assert ch["NVDA"]["action"] == "add"
    assert ch["MU"]["action"] == "reduce"
    assert ch["CEG"]["action"] == "new"
    assert ch["TSM"]["action"] == "exit"


def test_score_changes_new_beats_exit():
    p = ThirteenFParser()
    scoring = AppConfig().investors["scoring"]
    changes = [{
        "NVDA": {"action": "new", "pct_change": None, "portfolio_pct": 0.3, "manager": "F"},
        "TSM": {"action": "exit", "pct_change": -1.0, "portfolio_pct": 0.0, "manager": "F"},
    }]
    scored = p.score_changes(changes, scoring)
    assert scored["NVDA"].conviction_score > 0
    assert scored["TSM"].conviction_score < 0


def test_monitor_from_fixtures():
    from call_options_intel.pipeline import DEFAULT_FIXTURES
    cfg = AppConfig()
    scored = ThirteenFMonitor().run_from_fixtures(DEFAULT_FIXTURES, cfg.investors)
    assert "NVDA" in scored
    # NVDA is accumulated across all three funds -> strongly positive
    assert scored["NVDA"].conviction_score > 2
    # TSM was exited by SA LP -> negative
    assert scored["TSM"].conviction_score < 0


def test_monitor_missing_dir_is_safe(tmp_path):
    cfg = AppConfig()
    scored = ThirteenFMonitor().run_from_fixtures(tmp_path, cfg.investors)
    assert scored == {}
