"""Tests for the thesis-cluster proxy map (Goal F)."""
from call_options_intel.person_intel.entities import Confidence
from call_options_intel.person_intel.proxy_map import (
    THESIS_CLUSTERS, Proxy, build_proxy_map, classify_clusters, load_proxy_map,
)


def test_bundled_proxy_map_covers_all_clusters():
    pm = load_proxy_map()
    for c in THESIS_CLUSTERS:
        assert c in pm.clusters, f"missing cluster {c}"


def test_every_cluster_has_falsification():
    pm = load_proxy_map()
    # the hard rule: no cluster may be missing a falsification condition
    assert pm.warnings == [], pm.warnings
    for name in THESIS_CLUSTERS:
        assert pm.falsification_for(name)


def test_proxy_quality_is_order_and_evidence_monotonic():
    p1 = Proxy("AAA", order=1, evidence=Confidence.HIGH)
    p2 = Proxy("BBB", order=2, evidence=Confidence.MEDIUM)
    p3 = Proxy("CCC", order=3, evidence=Confidence.LOW)
    assert p1.quality() > p2.quality() > p3.quality()
    # same order, stronger evidence wins
    assert Proxy("X", 1, Confidence.HIGH).quality() > Proxy("X", 1, Confidence.LOW).quality()
    # same evidence, more direct order wins
    assert Proxy("X", 1, Confidence.HIGH).quality() > Proxy("X", 2, Confidence.HIGH).quality()


def test_first_order_pureplay_scores_high():
    pm = load_proxy_map()
    q = pm.proxy_quality("NVDA", ["compute"])
    assert q is not None and q >= 0.7
    # a 3rd-order proxy is materially weaker
    q3 = pm.proxy_quality("CSCO", ["networking"])
    assert q3 is not None and q3 < q


def test_clusters_for_ticker():
    pm = load_proxy_map()
    cls = dict(pm.clusters_for_ticker("CEG"))
    assert "power_grid" in cls and "nuclear" in cls


def test_classify_clusters_keyword_model():
    text = ("We need far more nuclear baseload power and grid gigawatts to feed "
            "the compute build-out; liquid cooling and export controls matter too.")
    scores = classify_clusters(text)
    assert "power_grid" in scores
    assert "nuclear" in scores
    assert "compute" in scores
    # empty text -> empty mapping (no hallucinated clusters)
    assert classify_clusters("") == {}


def test_missing_falsification_is_warned():
    pm = build_proxy_map({"clusters": {
        "compute": {"description": "x", "proxies": [{"ticker": "NVDA", "order": 1}]}}})
    assert any("falsification" in w for w in pm.warnings)
