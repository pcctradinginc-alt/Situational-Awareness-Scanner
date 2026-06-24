"""End-to-end pipeline tests (offline, deterministic)."""
from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import Pipeline


def test_full_offline_scan_produces_candidates():
    pipe = Pipeline(config=AppConfig(), mode="offline")
    result = pipe.scan()
    assert result.mode == "offline"
    assert result.universe_size > 10
    assert result.candidates, "offline scan should yield candidates"
    assert result.top, "offline scan should yield top picks"
    # top picks are diversified (one contract per ticker)
    top_tickers = [c.ticker for c in result.top]
    assert len(top_tickers) == len(set(top_tickers))
    # every candidate carries explanation + risk transparency
    c = result.candidates[0]
    assert c.thesis_explanation
    assert c.paper_recommendation
    assert c.breakdown.final_score <= 10.0


def test_thesis_separate_from_trade():
    """A strong-thesis name with a contradictory 13F must not auto-rank top."""
    pipe = Pipeline(config=AppConfig(), mode="offline")
    result = pipe.scan()
    by_ticker = {}
    for c in result.candidates:
        by_ticker.setdefault(c.ticker, c)
    # TSM has high thesis but was exited in 13F fixtures -> penalised
    if "TSM" in by_ticker:
        tsm = by_ticker["TSM"]
        assert tsm.breakdown.thesis > 7          # strong thesis
        assert tsm.breakdown.risk_penalty > 0    # but penalised


def test_scan_restricted_tickers():
    pipe = Pipeline(config=AppConfig(), mode="offline")
    result = pipe.scan(only_tickers=["NVDA"])
    assert result.universe_size == 1
    assert all(c.ticker == "NVDA" for c in result.candidates)


def test_degraded_ticker_flagged_not_fatal():
    # TER is intentionally absent from market fixture -> flagged, no crash
    pipe = Pipeline(config=AppConfig(), mode="offline")
    result = pipe.scan()
    assert any("TER" in w for w in result.data_quality_warnings)


def test_explain_single_ticker():
    pipe = Pipeline(config=AppConfig(), mode="offline")
    out = pipe.explain("NVDA")
    assert out is not None
    assert out["evaluation"].ticker == "NVDA"
    assert out["n_contracts"] > 0


def test_explain_unknown_ticker():
    pipe = Pipeline(config=AppConfig(), mode="offline")
    assert pipe.explain("ZZZZ") is None
