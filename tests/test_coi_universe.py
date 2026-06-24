"""Tests for the universe builder."""
from call_options_intel.universe import UniverseBuilder


def test_build_basic():
    cfg = {"categories": ["semiconductors"], "tickers": [
        {"ticker": "nvda", "name": "NVIDIA", "category": "semiconductors",
         "relevance": 0.98, "thesis_tags": ["compute_scarcity"]},
    ]}
    entries = UniverseBuilder(cfg).build()
    assert len(entries) == 1
    e = entries[0]
    assert e.ticker == "NVDA"            # upper-cased
    assert e.relevance == 0.98
    assert e.thesis_tags == ["compute_scarcity"]


def test_skips_bad_rows_and_dups():
    cfg = {"tickers": [
        {"ticker": "NVDA"},
        {"ticker": "NVDA"},                 # duplicate -> skipped
        {"name": "no ticker"},              # missing ticker -> skipped
        "not a dict",                        # malformed -> skipped
        {"ticker": "MU", "relevance": "bad"},  # bad relevance -> default 0.5
    ]}
    entries = UniverseBuilder(cfg).build()
    tickers = [e.ticker for e in entries]
    assert tickers == ["NVDA", "MU"]
    assert entries[1].relevance == 0.5


def test_relevance_clamped():
    cfg = {"tickers": [{"ticker": "X", "relevance": 5.0}]}
    assert UniverseBuilder(cfg).build()[0].relevance == 1.0


def test_filter_tickers():
    cfg = {"tickers": [{"ticker": "NVDA"}, {"ticker": "MU"}, {"ticker": "TSM"}]}
    entries = UniverseBuilder(cfg).build()
    filt = UniverseBuilder.filter_tickers(entries, ["mu", "tsm"])
    assert {e.ticker for e in filt} == {"MU", "TSM"}
