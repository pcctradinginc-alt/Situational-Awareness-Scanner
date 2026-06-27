"""J — no-live-trading guard over the new sub-package, CLI smoke, integration."""
from pathlib import Path

import call_options_intel
from call_options_intel.cli import _grep_dangerous, main
from call_options_intel.person_intel.runner import run_person_intel


def test_no_order_code_in_whole_package():
    root = Path(call_options_intel.__file__).resolve().parent
    hits = _grep_dangerous(root)
    assert hits == [], f"order-execution patterns found: {hits}"


def test_guard_scans_person_intel_subpackage(tmp_path):
    # plant a file with an order pattern inside a fake package tree -> must be caught
    pkg = tmp_path / "pkg"
    (pkg / "person_intel").mkdir(parents=True)
    (pkg / "person_intel" / "evil.py").write_text("def x():\n    place_order()\n")
    hits = _grep_dangerous(pkg)
    assert any("evil.py" in h for h in hits)


def test_cli_person_intel_runs():
    assert main(["person-intel", "--tickers", "NVDA", "CEG"]) == 0


def test_cli_doctor_runs():
    assert main(["doctor"]) == 0


def test_network_weight_resolves_aliases():
    # the 13F fixtures label managers with short names ("Thiel Capital",
    # "Founders Fund"); these must resolve to the tracked network via aliases,
    # not silently fall back to the adjacent-smart-money weight.
    from call_options_intel.person_intel.entities import load_graph
    from call_options_intel.person_intel.runner import _network_weight
    g = load_graph()
    assert _network_weight(g, "Thiel Capital") == 1.0
    assert _network_weight(g, "Founders Fund") == 1.0
    assert _network_weight(g, "Situational Awareness LP") == 1.0
    assert _network_weight(g, "Coatue Management LLC") == 0.3   # adjacent, not network


def test_integration_thesis_not_equal_trade():
    rows = run_person_intel(only_tickers=["NVDA", "CEG", "TSM"])
    by = {r.score.ticker: r for r in rows}
    # every surfaced row keeps trade <= research and carries a falsification
    for r in rows:
        assert r.score.final_trade_candidate_score <= r.score.final_research_score + 1e-9
        assert r.score.falsification
    # TSM is a thesis proxy NOT held by a tracked fund -> unverifiable person link
    # -> strong-ish research must NOT translate into a strong trade candidate
    if "TSM" in by:
        tsm = by["TSM"]
        assert tsm.score.final_trade_candidate_score < tsm.score.final_research_score
