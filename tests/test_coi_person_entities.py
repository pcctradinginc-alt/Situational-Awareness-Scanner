"""Tests for the person-intelligence entity graph (Goal A)."""
from call_options_intel.person_intel.entities import (
    Confidence, EntityType, build_graph, load_graph,
)


def _sample_cfg():
    return {
        "entities": [
            {"id": "thiel", "name": "Peter Thiel", "type": "person",
             "cik": "0001211060"},
            {"id": "macro", "name": "Thiel Macro LLC", "type": "fund",
             "cik": "0001562087"},
            {"id": "pltr", "name": "Palantir", "type": "portfolio_company"},
            {"id": "ff", "name": "Founders Fund V", "type": "management_company"},
        ],
        "relations": [
            {"source": "thiel", "target": "macro", "kind": "controls",
             "confidence": "confirmed", "is_fact": True},
            {"source": "thiel", "target": "pltr", "kind": "personal_holding",
             "confidence": "confirmed", "is_fact": True},
            {"source": "ff", "target": "pltr", "kind": "fund_exposure",
             "confidence": "medium", "is_fact": False},
        ],
    }


def test_confidence_is_ordered_and_weighted():
    assert Confidence.CONFIRMED.weight > Confidence.HIGH.weight
    assert Confidence.HIGH.weight > Confidence.MEDIUM.weight
    assert Confidence.MEDIUM.weight > Confidence.LOW.weight
    assert Confidence.LOW.weight > Confidence.SPECULATIVE.weight


def test_unknown_confidence_degrades_to_speculative():
    assert Confidence.parse("garbage") is Confidence.SPECULATIVE
    assert Confidence.parse("HIGH") is Confidence.HIGH


def test_facts_and_hypotheses_are_separated():
    g = build_graph(_sample_cfg())
    facts = g.facts()
    hyps = g.hypotheses()
    assert {(r.source, r.target) for r in facts} == {("thiel", "macro"), ("thiel", "pltr")}
    assert {(r.source, r.target) for r in hyps} == {("ff", "pltr")}
    # a hypothesis must never be silently treated as a fact
    assert all(r.is_fact for r in facts)
    assert all(not r.is_fact for r in hyps)


def test_lookup_by_cik_normalises_leading_zeros():
    g = build_graph(_sample_cfg())
    assert g.by_cik("1211060").id == "thiel"
    assert g.by_cik("0001211060").id == "thiel"
    assert g.by_cik("9999999") is None


def test_path_confidence_prefers_confirmed_chain():
    g = build_graph(_sample_cfg())
    # direct confirmed personal holding -> weight 1.0
    direct = g.path_confidence("thiel", "pltr")
    assert direct == 1.0
    # via a hypothesised fund edge it is strictly weaker
    via_fund = g.path_confidence("ff", "pltr")
    assert via_fund is not None and via_fund < direct
    assert g.path_confidence("thiel", "ff") is None or g.path_confidence("thiel", "ff") <= 1.0


def test_entity_type_parsed():
    g = build_graph(_sample_cfg())
    assert g.get("thiel").type is EntityType.PERSON
    assert g.get("macro").type is EntityType.FUND


def test_malformed_rows_are_skipped_not_fatal():
    cfg = {
        "entities": [{"name": "no id here"}, {"id": "ok", "name": "Ok", "type": "fund"}],
        "relations": [{"source": "ok", "target": "ghost", "kind": "x"}],
    }
    g = build_graph(cfg)
    assert "ok" in g.entities and len(g.entities) == 1
    assert g.relations == []          # dangling relation dropped
    assert len(g.warnings) >= 2


def test_bundled_config_loads_thiel_and_aschenbrenner():
    g = load_graph()
    # the shipped graph must contain both tracked principals as facts
    assert g.by_cik("0002045724").id == "sa_lp"          # Situational Awareness LP
    thiel = g.get("thiel")
    assert thiel is not None and thiel.type is EntityType.PERSON
    # Aschenbrenner -> SA LP is a CONFIRMED fact, not a hypothesis
    asch_edges = [r for r in g.facts()
                  if r.source == "aschenbrenner" and r.target == "sa_lp"]
    assert asch_edges and asch_edges[0].confidence is Confidence.CONFIRMED
