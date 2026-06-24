"""Tests for thesis theme extraction."""
from call_options_intel.thesis import ThesisAnalyzer


def test_extracts_themes_and_normalises():
    text = ("Compute scarcity and a GPU shortage drive the data center buildout. "
            "Power and grid constraints and HBM memory bandwidth are bottlenecks. "
            "Semiconductor foundry capacity is the binding constraint.")
    vec = ThesisAnalyzer().analyze_text(text)
    assert vec.weights, "should detect themes"
    assert "compute_scarcity" in vec.weights
    assert "power_grid_constraints" in vec.weights
    # normalised 0..1
    assert all(0.0 <= w <= 1.0 for w in vec.weights.values())
    assert 0.0 <= vec.confidence <= 1.0


def test_empty_text_is_safe():
    vec = ThesisAnalyzer().analyze_text("")
    assert vec.weights == {}
    assert vec.confidence == 0.0


def test_score_for_tags():
    text = "compute scarcity compute scarcity power grid electricity"
    vec = ThesisAnalyzer().analyze_text(text)
    s = vec.score_for_tags(["compute_scarcity", "power_grid_constraints"])
    assert s > 0
    assert vec.score_for_tags([]) == 0.0
    assert vec.score_for_tags(["nonexistent_tag"]) == 0.0


def test_reads_bundled_thesis_dir():
    from call_options_intel.pipeline import DEFAULT_FIXTURES
    vec = ThesisAnalyzer().analyze_paths([DEFAULT_FIXTURES / "thesis"])
    assert vec.confidence > 0
    assert "semiconductor_bottlenecks" in vec.weights
