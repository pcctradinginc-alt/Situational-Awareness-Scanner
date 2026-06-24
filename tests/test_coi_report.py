"""Tests for report generation in all formats."""
import csv
import io
import json

import pytest

from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import Pipeline
from call_options_intel.report import ReportGenerator


@pytest.fixture(scope="module")
def result_and_gen():
    cfg = AppConfig()
    pipe = Pipeline(config=cfg, mode="offline")
    result = pipe.scan()
    return result, ReportGenerator(cfg.report)


def test_markdown_has_required_sections(result_and_gen):
    result, gen = result_and_gen
    md = gen.to_markdown(result)
    for section in ["Executive Summary", "Top CALL Candidates", "Watchlist",
                    "13F Smart-Money Changes", "Thesis Map", "Market Regime",
                    "Rejected Contracts", "Data Quality Warnings",
                    "Test / Paper-Trading Recommendation"]:
        assert section in md, f"missing section: {section}"
    assert "RESEARCH / PAPER MODE ONLY" in md


def test_csv_parses_and_has_headers(result_and_gen):
    result, gen = result_and_gen
    text = gen.to_csv(result)
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    for col in ["ticker", "strike", "final_score", "reasons_against",
                "paper_recommendation", "confidence_label"]:
        assert col in header
    assert len(rows) >= 2  # header + at least one candidate


def test_json_is_valid_and_structured(result_and_gen):
    result, gen = result_and_gen
    data = json.loads(gen.to_json(result))
    assert "candidates" in data and "top" in data
    assert data["mode"] == "offline"
    if data["candidates"]:
        c = data["candidates"][0]
        assert "contract" in c and "breakdown" in c
        assert "paper_recommendation" in c


def test_html_renders(result_and_gen):
    result, gen = result_and_gen
    html = gen.to_html(result)
    assert html.startswith("<!doctype html>")
    assert "RESEARCH" in html


def test_write_all_formats(tmp_path, result_and_gen):
    result, gen = result_and_gen
    written = gen.write(result, ["markdown", "csv", "json", "html"], tmp_path, "scan")
    for fmt, path in written.items():
        assert path.exists() and path.stat().st_size > 0


def test_unknown_format_raises(result_and_gen):
    result, gen = result_and_gen
    with pytest.raises(ValueError):
        gen.render(result, "pdf")
