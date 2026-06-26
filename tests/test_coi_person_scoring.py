"""Tests for the split research-vs-trade scoring (Goal G)."""
from call_options_intel.person_intel.filings import FilingType, VerificationStatus
from call_options_intel.person_intel.person_scoring import PersonScorer, PersonSignal


def _strong_signal(**over):
    base = dict(
        ticker="NVDA",
        filing_type=FilingType.SC_13D,                 # EARLY filing
        verification=VerificationStatus.VERIFIED,
        path_confidence=1.0,
        portfolio_pct=0.15,
        proxy_quality=0.9,
        thesis_clusters={"compute": 0.9, "power_grid": 0.4},
        falsification="capex cut >15% q/q",
    )
    base.update(over)
    return PersonSignal(**base)


def test_trade_never_exceeds_research():
    sc = PersonScorer()
    for sig in (_strong_signal(),
                _strong_signal(market_timing=8.0, options_quality=8.0),
                _strong_signal(verification=VerificationStatus.NOT_VERIFIABLE_VIA_13F),
                _strong_signal(market_timing=2.0, options_quality=1.0)):
        r = sc.score(sig)
        assert r.final_trade_candidate_score <= r.final_research_score + 1e-9


def test_thesis_not_equal_trade_when_no_timing():
    sc = PersonScorer()
    r = sc.score(_strong_signal())          # no market_timing / options_quality
    assert r.final_research_score >= 6.5    # strong research
    # …but without timing/options it must NOT be a strong trade candidate
    assert r.final_trade_candidate_score < r.final_research_score
    assert r.trade_label in ("watch", "reject")
    assert any("thesis≠trade" in x for x in r.reasons_against)


def test_full_timing_and_options_lift_trade():
    sc = PersonScorer()
    low = sc.score(_strong_signal())
    high = sc.score(_strong_signal(market_timing=9.0, options_quality=9.0))
    # same research, better timing/options -> strictly better trade candidate
    assert abs(high.final_research_score - low.final_research_score) < 1e-9
    assert high.final_trade_candidate_score > low.final_trade_candidate_score


def test_research_monotonic_in_path_confidence():
    sc = PersonScorer()
    weak = sc.score(_strong_signal(path_confidence=0.2))
    strong = sc.score(_strong_signal(path_confidence=1.0))
    assert strong.final_research_score > weak.final_research_score


def test_falsification_trigger_lowers_scores():
    sc = PersonScorer()
    ok = sc.score(_strong_signal(market_timing=8.0, options_quality=8.0))
    broke = sc.score(_strong_signal(market_timing=8.0, options_quality=8.0,
                                    falsification_triggered=True))
    assert broke.final_research_score < ok.final_research_score
    assert broke.final_trade_candidate_score < ok.final_trade_candidate_score
    assert any("FALSIFICATION" in x for x in broke.reasons_against)


def test_weak_verification_caps_trade():
    sc = PersonScorer()
    # great timing + options, but a 13F option line we cannot verify directionally
    r = sc.score(_strong_signal(verification=VerificationStatus.NOT_VERIFIABLE_VIA_13F,
                                market_timing=9.0, options_quality=9.0))
    assert r.final_trade_candidate_score <= 4.0      # hard verification cap
    assert r.needs_human_review is True


def test_rejected_noise_floors_everything():
    sc = PersonScorer()
    r = sc.score(_strong_signal(verification=VerificationStatus.REJECTED_NOISE,
                                market_timing=9.0, options_quality=9.0))
    assert r.final_trade_candidate_score == 0.0


def test_every_result_has_falsification_and_counterargument():
    sc = PersonScorer()
    r = sc.score(_strong_signal(market_timing=8.0, options_quality=8.0))
    assert r.falsification and r.falsification != ""
    assert r.reasons_against                # never empty
    # components carry reasoning + raw for auditability
    d = r.as_dict()
    assert d["components"]["person_signal"]["reason"]
    assert "filing_role" in d["components"]["person_signal"]["raw"]


def test_early_filing_beats_lagged_13f():
    sc = PersonScorer()
    early = sc.score(_strong_signal(filing_type=FilingType.FORM_4))
    lagged = sc.score(_strong_signal(filing_type=FilingType.FORM_13F_HR))
    assert early.components["person_signal"].value > lagged.components["person_signal"].value
