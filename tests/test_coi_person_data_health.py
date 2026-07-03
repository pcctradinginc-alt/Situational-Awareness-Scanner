"""Tests for live data-quality monitoring (anomalies that look like opportunities)."""
from call_options_intel.person_intel.data_health import assess_data_health


def test_healthy_run_has_no_anomalies():
    # a candidate blocked for a LEGITIMATE reason (earnings / no IV history) is a
    # verdict, NOT a data anomaly
    items = [
        {"ticker": "PLTR", "ev": {"hard_fails": ["earnings_in_window"],
                                  "reasons": ["earnings inside holding window"]}},
        {"ticker": "VST", "ev": {"hard_fails": ["no IV history — IV-rank unknown"],
                                 "reasons": ["no IV history — IV-rank unknown"]}},
    ]
    r = assess_data_health(items)
    assert r["ok"] is True
    assert r["anomalies"] == []
    assert r["n_checked"] == 2


def test_no_snapshot_is_a_data_anomaly():
    items = [{"ticker": "NVDA", "ev": {"hard_fails": ["no_snapshot"],
                                       "reasons": ["HARD-GATE: no option snapshot"]}}]
    r = assess_data_health(items)
    assert r["ok"] is False
    assert r["anomalies"][0]["ticker"] == "NVDA"
    assert r["anomalies"][0]["kind"] == "stale_or_empty_chain"


def test_incomplete_snapshot_is_a_data_anomaly():
    items = [{"ticker": "CEG", "ev": {
        "hard_fails": ["incomplete snapshot: iv, dte"],
        "reasons": ["HARD-GATE rejected: incomplete snapshot: iv, dte"]}}]
    r = assess_data_health(items)
    assert r["ok"] is False
    assert r["anomalies"][0]["kind"] == "incomplete_snapshot"


def test_empty_is_ok():
    r = assess_data_health([])
    assert r["ok"] is True and r["n_checked"] == 0
