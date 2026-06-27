"""Tests for the optional LLM statement classifier (Phase 3.2)."""
import pytest

from call_options_intel.person_intel.llm_classify import (
    AnthropicClassifier, StatementClassifier, _parse_scores,
)
from call_options_intel.person_intel.statements import make_statement_ref


class _FakeLLM:
    def __init__(self, scores, raise_exc=False):
        self.scores = scores
        self.raise_exc = raise_exc
        self.calls = 0

    def classify(self, text, clusters):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("simulated API failure")
        return self.scores


def test_llm_used_when_present():
    sc = StatementClassifier(_FakeLLM({"compute": 0.9, "power_grid": 0.3}))
    res = sc.classify_detailed("anything")
    assert res.method == "llm"
    assert res.clusters["compute"] == 0.9
    assert res.advisory is True and res.needs_human_review is True


def test_falls_back_to_keyword_on_error():
    sc = StatementClassifier(_FakeLLM({}, raise_exc=True))
    res = sc.classify_detailed("nuclear power grid gigawatts for compute")
    assert res.method == "keyword_fallback"
    assert "power_grid" in res.clusters and "compute" in res.clusters


def test_no_llm_uses_keyword():
    sc = StatementClassifier(None)
    res = sc.classify_detailed("liquid cooling and networking interconnect")
    assert res.method == "keyword_fallback"
    assert "cooling" in res.clusters or "networking" in res.clusters


def test_empty_llm_result_falls_back():
    sc = StatementClassifier(_FakeLLM({}))     # returns no clusters
    res = sc.classify_detailed("nuclear reactor baseload")
    assert res.method == "keyword_fallback"
    assert "nuclear" in res.clusters


def test_integration_with_statement_ref_stays_advisory():
    sc = StatementClassifier(_FakeLLM({"defense_ai": 0.8}))
    ref = make_statement_ref(
        "https://x/post", "Src", "official", "2026-06-01",
        "defense AI national security", classifier=sc.classify)
    assert ref.thesis_clusters == {"defense_ai": 0.8}
    assert ref.needs_human_review is True       # LLM never the final word


def test_parse_scores_is_tolerant():
    body = 'Here you go: {"compute": 0.7, "bogus": 1.0, "nuclear": "0.4"} done'
    out = _parse_scores(body, ["compute", "nuclear"])
    assert out == {"compute": 0.7, "nuclear": 0.4}     # unknown cluster dropped
    assert _parse_scores("no json", ["compute"]) == {}


def test_anthropic_classifier_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicClassifier().classify("text", ["compute"])
