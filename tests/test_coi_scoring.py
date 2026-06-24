"""Tests for the signal engine: component scores, combine, degradation."""
import pytest

from call_options_intel.config_loader import AppConfig
from call_options_intel.scoring import SignalEngine
from call_options_intel.models import (
    UniverseEntry, ThesisVector, MarketSnapshot, Holding13FChange, OptionContract,
)


@pytest.fixture
def engine():
    cfg = AppConfig()
    return SignalEngine(cfg.scoring, cfg.risk)


@pytest.fixture
def entry():
    return UniverseEntry(ticker="NVDA", name="NVIDIA", category="semiconductors",
                         relevance=0.9, thesis_tags=["compute_scarcity"])


def test_score_thesis_uses_prior_when_no_corpus(engine, entry):
    s = engine.score_thesis(entry, ThesisVector())
    assert abs(s - 9.0) < 0.01  # relevance 0.9 -> 9.0


def test_score_thesis_blends_with_corpus(engine, entry):
    vec = ThesisVector(weights={"compute_scarcity": 1.0}, confidence=0.8)
    s = engine.score_thesis(entry, vec)
    # 0.6*0.9 + 0.4*1.0 = 0.94 -> 9.4
    assert abs(s - 9.4) < 0.01


def test_score_market_uptrend_positive(engine):
    snap = MarketSnapshot(ticker="X", spot=100, momentum_3m=0.15, momentum_6m=0.25,
                          above_200dma=True, rsi_14=55, drawdown_from_high=-0.05,
                          stabilizing=True, hist_vol_annual=0.4)
    score, rfor, ragainst, pen = engine.score_market(snap)
    assert score > 7
    assert not pen


def test_score_market_excessive_drawdown_penalised(engine):
    snap = MarketSnapshot(ticker="X", spot=60, momentum_3m=-0.1, momentum_6m=-0.2,
                          above_200dma=False, rsi_14=35, drawdown_from_high=-0.45,
                          stabilizing=False, hist_vol_annual=0.6)
    score, rfor, ragainst, pen = engine.score_market(snap)
    assert "excessive_drawdown" in pen


def test_score_13f_negative_is_penalised(engine):
    ch = Holding13FChange(ticker="TSM", action="exit", conviction_score=-3.0)
    score, rfor, ragainst, pen = engine.score_13f(ch)
    assert score == 0.0
    assert "contradictory_13f" in pen


def test_no_catalyst_penalty(engine):
    # no catalyst => component ABSENT (None) + penalty (keeps confidence honest)
    score, rfor, pen = engine.score_catalyst([])
    assert score is None
    assert "no_catalyst" in pen
    score2, _, pen2 = engine.score_catalyst(["earnings in ~5d"])
    assert score2 is not None and score2 > 0
    assert "no_catalyst" not in pen2


def test_combine_normalises_missing_components(engine):
    # Only thesis present; weights re-normalise so score ~ thesis minus the
    # weak-data-quality penalty for having most components absent.
    bd = engine.combine({"thesis": 8.0, "thirteen_f": None, "market": None,
                         "options": None, "catalyst": None}, {})
    assert bd.weighted_positive == 8.0
    assert "weak_data_quality" not in bd.components_present
    assert bd.confidence < 0.5            # only 1/5 components
    assert bd.final_score < bd.weighted_positive  # data-quality penalty applied


def test_combine_caps_risk_penalty(engine):
    huge = {f"p{i}": 5.0 for i in range(5)}
    bd = engine.combine({"thesis": 9.0, "market": 9.0}, huge)
    assert bd.risk_penalty <= engine.penalty_cfg["max_total_penalty"]


def test_evaluate_ticker_labels(engine, entry):
    vec = ThesisVector(weights={"compute_scarcity": 1.0}, confidence=0.8)
    snap = MarketSnapshot(ticker="NVDA", spot=200, momentum_3m=0.2, momentum_6m=0.3,
                          above_200dma=True, rsi_14=58, drawdown_from_high=-0.05,
                          stabilizing=True, hist_vol_annual=0.45)
    ch = Holding13FChange(ticker="NVDA", action="add", conviction_score=8.0,
                          managers=["SA LP"])
    contracts = [OptionContract(ticker="NVDA", expiry="2026-12-18", strike=200, dte=90,
                                bid=20, ask=21, iv=0.5, open_interest=5000, volume=1000,
                                spot=200)]
    ev = engine.evaluate_ticker(entry, vec, snap, ch, contracts, ["earnings in ~5d"])
    assert ev.label == "top"
    assert ev.breakdown.confidence_label == "high"
    assert ev.breakdown.final_score > 6.5
