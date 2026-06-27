"""Tests for the documented-network helper (entity-graph reachability)."""
from call_options_intel.person_intel.entities import load_graph
from call_options_intel.person_intel.network import (
    fact_path_weight, network_summary,
)


def _graph():
    return load_graph(None)


def test_thiel_network_includes_public_and_private():
    s = network_summary(_graph(), "thiel")
    pub = {c.ticker for c in s.public}
    priv = {c.name.split(" (")[0] for c in s.private}
    assert "PLTR" in pub
    assert any("Anduril" in n for n in priv)
    # private names are flagged private, never public
    assert all(c.is_public is False for c in s.private)
    assert all(c.is_public is True for c in s.public)


def test_themes_are_ranked():
    s = network_summary(_graph(), "thiel")
    assert s.themes                                  # non-empty
    # most common first
    counts = [n for _, n in s.themes]
    assert counts == sorted(counts, reverse=True)


def test_documented_network_uses_fact_edges_only():
    """Aschenbrenner reaches Palantir only via a HYPOTHESIS (ideological) edge to
    Thiel, so his *documented* (fact-only) network must NOT include it."""
    g = _graph()
    # the hypothesis path exists...
    assert (g.path_confidence("aschenbrenner", "palantir") or 0) > 0
    # ...but the fact-only path does not
    assert fact_path_weight(g, "aschenbrenner", "palantir") == 0.0
    assert network_summary(g, "aschenbrenner").context_line() == ""


def test_context_line_marks_private():
    line = network_summary(_graph(), "thiel").context_line()
    assert "public:" in line and "private:" in line and "PLTR" in line
