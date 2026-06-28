"""Tests for the three-axis scorer + hard AND-gate.

The contract that matters: a TRADE-CANDIDATE may only emerge when ALL THREE
axes (person_signal · freshness · tradeability) clear their gate. Any single
weak axis blocks the trade — no summing a strong axis over a weak one.
"""
from call_options_intel.person_intel.filings import SignalRole
from call_options_intel.person_intel.statements import SourceTier
from call_options_intel.person_intel.triple_score import (
    GateThresholds, TripleInput, TripleScorer,
)

SC = TripleScorer()


def _trade(**kw) -> TripleInput:
    base = dict(ticker="PLTR", path_weight=1.0, verified=True,
                is_primary_source=True, role=SignalRole.EARLY, age_days=4,
                has_public_ticker=True, market_timing=7.0, options_quality=8.0)
    base.update(kw)
    return TripleInput(**base)


def test_all_three_pass_is_a_trade_candidate():
    r = SC.score(_trade())
    assert r.gate_pass is True
    assert r.failing_axes == []
    assert r.label == "TRADE-CANDIDATE"
    # weakest-link: final never exceeds the lowest axis
    assert r.final_trade_score == min(r.person_signal.value, r.freshness.value,
                                      r.tradeability.value)


def test_weak_tradeability_blocks_trade():
    # unverified options liquidity -> tradeability capped -> gate fails
    r = SC.score(_trade(options_quality=None))
    assert r.gate_pass is False
    assert "tradeability" in r.failing_axes
    assert r.final_trade_score == 0.0


def test_no_public_ticker_is_not_tradeable():
    r = SC.score(_trade(ticker=None, has_public_ticker=False))
    assert r.tradeability.value == 0.0
    assert r.gate_pass is False


def test_private_round_is_not_tradeable():
    r = SC.score(_trade(is_private=True, has_public_ticker=False, ticker=None))
    assert r.tradeability.value == 0.0
    assert "tradeability" in r.failing_axes


def test_stale_13f_fails_freshness():
    # a 44-day-old 13F is already broadly priced -> low freshness
    r = SC.score(_trade(role=SignalRole.CONFIRMATION, age_days=44))
    assert r.freshness.value < 5.0
    assert "freshness" in r.failing_axes
    assert r.gate_pass is False


def test_fresh_early_filing_scores_high_freshness():
    r = SC.score(_trade(role=SignalRole.EARLY, age_days=1))
    assert r.freshness.value >= 8.0


def test_weak_path_fails_person_signal():
    # adjacent smart money (weak controlled-path) is not a direct person-move
    r = SC.score(_trade(path_weight=0.3))
    assert r.person_signal.value < 5.0
    assert "person_signal" in r.failing_axes
    assert r.gate_pass is False


def test_unverified_halves_person_signal():
    v = SC.score(_trade(verified=True)).person_signal.value
    u = SC.score(_trade(verified=False)).person_signal.value
    assert u < v


def test_first_party_essay_has_person_directness():
    # an OFFICIAL first-party statement carries real person-directness even with
    # no controlled-holding path (set by the caller via path_weight)
    r = SC.score(TripleInput(
        ticker="VST", path_weight=0.9, verified=True, is_primary_source=True,
        source_tier=SourceTier.OFFICIAL, age_days=2, has_public_ticker=True,
        market_timing=None, options_quality=None))
    assert r.person_signal.value >= 8.0
    assert r.freshness.value > 5.0
    # but a statement alone, with unverified option liquidity, is not a trade
    assert r.gate_pass is False
    assert "tradeability" in r.failing_axes


def test_thresholds_from_config():
    t = GateThresholds.from_config({"person_gates": {"tradeability": 9.0}})
    assert t.tradeability == 9.0
    strict = TripleScorer(t)
    # a normally-passing trade now fails the stricter tradeability bar
    assert strict.score(_trade()).gate_pass is False


def test_weakest_link_semantics():
    # final score equals the minimum axis when the gate passes
    r = SC.score(_trade(market_timing=6.0, options_quality=6.0))  # tradeability 6.0
    assert r.gate_pass is True
    assert r.final_trade_score == min(r.person_signal.value, r.freshness.value,
                                      r.tradeability.value)
