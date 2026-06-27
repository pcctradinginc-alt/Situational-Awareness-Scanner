"""Tests for the person-intelligence report panel + scan wiring (Phase 2)."""
import json

from call_options_intel.cli import main
from call_options_intel.config_loader import AppConfig
from call_options_intel.report import ReportGenerator
from call_options_intel.person_intel.runner import run_person_intel


def _gen():
    return ReportGenerator(AppConfig().report)


def test_person_intel_markdown_has_panel_and_falsification():
    rows = run_person_intel(only_tickers=["NVDA", "CEG", "TSM"])
    md = _gen().person_intel_markdown(rows)
    assert "Person-Intelligence" in md
    assert "thesis" in md.lower() and "trade" in md.lower()
    assert "Falsification" in md
    # every surfaced ticker appears in the table
    for r in rows:
        assert r.score.ticker in md


def test_person_intel_json_is_valid_and_split_scored():
    rows = run_person_intel(only_tickers=["NVDA", "CEG"])
    data = json.loads(_gen().person_intel_json(rows))
    assert isinstance(data, list) and data
    for d in data:
        assert "final_research_score" in d
        assert "final_trade_candidate_score" in d
        assert d["final_trade_candidate_score"] <= d["final_research_score"] + 1e-9


def test_empty_rows_render_safely():
    md = _gen().person_intel_markdown([])
    assert "No person-linked candidates" in md
    assert _gen().person_intel_json([]) == "[]"


def test_scan_person_intel_writes_artifacts(tmp_path):
    out = tmp_path / "run"
    rc = main(["scan", "--tickers", "NVDA", "CEG", "--output", str(out),
               "--format", "markdown", "--person-intel"])
    assert rc == 0
    assert (tmp_path / "run_person_intel.md").exists()
    pj = tmp_path / "run_person_intel.json"
    assert pj.exists()
    assert isinstance(json.loads(pj.read_text()), list)
