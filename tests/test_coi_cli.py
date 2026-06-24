"""CLI smoke tests."""
import json

from call_options_intel.cli import main


def test_scan_writes_reports(tmp_path, capsys):
    stem = tmp_path / "out"
    rc = main(["scan", "--output", str(stem), "--format", "markdown", "csv", "json"])
    assert rc == 0
    assert (tmp_path / "out.md").exists()
    assert (tmp_path / "out.csv").exists()
    assert (tmp_path / "out.json").exists()
    out = capsys.readouterr().out
    assert "RESEARCH / PAPER MODE ONLY" in out


def test_scan_restricted_and_record(tmp_path):
    store = tmp_path / "sig.jsonl"
    rc = main(["scan", "--tickers", "NVDA", "--output", str(tmp_path / "o"),
               "--format", "json", "--record", "--store", str(store)])
    assert rc == 0
    assert store.exists()
    data = json.loads((tmp_path / "o.json").read_text())
    assert all(c["ticker"] == "NVDA" for c in data["candidates"])


def test_doctor(capsys):
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "health check" in out
    assert "no live-trading" in out


def test_explain(capsys):
    rc = main(["explain", "NVDA"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NVDA" in out
    assert "Final score" in out


def test_explain_unknown(capsys):
    rc = main(["explain", "ZZZZ"])
    assert rc == 1


def test_update_13f(capsys):
    rc = main(["update-13f"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NVDA" in out


def test_backtest_demo_then_evaluate(tmp_path, capsys):
    store = tmp_path / "s.jsonl"
    rc = main(["backtest", "demo", "--store", str(store)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "option_proxy" in out or "no matured" in out
