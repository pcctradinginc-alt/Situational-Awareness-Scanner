"""Missing-data behaviour: degrade gracefully, flag, never crash."""
from call_options_intel.config_loader import AppConfig
from call_options_intel.scoring import SignalEngine
from call_options_intel.candidates import CandidateGenerator
from call_options_intel.market_data import MarketDataProvider
from call_options_intel.options_data import OptionsDataProvider
from call_options_intel.models import UniverseEntry, ThesisVector


def test_evaluate_with_everything_missing():
    cfg = AppConfig()
    engine = SignalEngine(cfg.scoring, cfg.risk)
    entry = UniverseEntry(ticker="X", name="X", category="c", relevance=0.7)
    # no market, no 13F, no options, no catalysts
    ev = engine.evaluate_ticker(entry, ThesisVector(), None, None, [], [])
    assert ev.breakdown.final_score >= 0          # no crash, valid score
    assert ev.breakdown.confidence_label == "low"
    assert "no_options_data" in ev.data_flags
    assert ev.breakdown.thesis > 0                # thesis still works from prior


def test_candidates_empty_when_no_contracts():
    cfg = AppConfig()
    engine = SignalEngine(cfg.scoring, cfg.risk)
    gen = CandidateGenerator(engine, cfg.risk)
    entry = UniverseEntry(ticker="X", name="X", category="c", relevance=0.7)
    ev = engine.evaluate_ticker(entry, ThesisVector(), None, None, [], [])
    cands, rej = gen.generate(ev, entry, [], None)
    assert cands == [] and rej == []


def test_market_provider_missing_fixture_dir(tmp_path):
    prov = MarketDataProvider(AppConfig().data_sources, mode="offline",
                              fixtures_dir=tmp_path)
    snap = prov.get_snapshot("NVDA")
    assert "no_market_data" in snap.data_flags    # missing fixture -> flagged


def test_options_provider_missing_fixture_dir(tmp_path):
    prov = OptionsDataProvider(AppConfig().data_sources, mode="offline",
                               fixtures_dir=tmp_path)
    assert prov.get_call_contracts("NVDA", spot=100) == []


def test_pipeline_runs_with_empty_fixtures(tmp_path):
    """Whole scan with no fixtures at all should still complete + report."""
    from call_options_intel.pipeline import Pipeline
    pipe = Pipeline(config=AppConfig(), mode="offline", fixtures_dir=tmp_path)
    result = pipe.scan(only_tickers=["NVDA", "MU"])
    assert result.universe_size == 2
    # thesis-only evaluations still produced, just low confidence / no candidates
    assert result.evaluations
    assert all(e.breakdown.confidence_label == "low" for e in result.evaluations)
