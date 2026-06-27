"""Tests for real per-horizon historical pricing in outcome learning (Phase 4.1)."""
import json
from datetime import date

from call_options_intel.cli import main
from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.historical import (
    HistoricalPriceProvider, _parse_csv,
)
from call_options_intel.person_intel.outcomes import OutcomeStore

AS_OF = date(2026, 7, 1)


def _provider():
    return HistoricalPriceProvider(mode="offline", fixtures_dir=DEFAULT_FIXTURES)


def test_price_on_resolves_on_or_before():
    p = _provider()
    assert p.price_on("TEST", date(2026, 1, 2)) == 100
    assert p.price_on("TEST", date(2026, 2, 1)) == 112
    # a weekend/holiday date resolves to the prior available close
    assert p.price_on("TEST", date(2026, 2, 15)) == 112
    # before the series and beyond it -> None (never fabricate)
    assert p.price_on("TEST", date(2025, 1, 1)) is None
    assert p.price_on("TEST", date(2027, 1, 1)) is None
    assert p.price_on("UNKNOWN", date(2026, 2, 1)) is None


def test_parse_csv_tolerant_columns():
    s = _parse_csv("Date,Close\n2026-01-02,500\n2026-01-09,505\n")
    assert s[date(2026, 1, 2)] == 500 and s[date(2026, 1, 9)] == 505


def test_date_aware_evaluate_uses_horizon_price(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    store.record({"recorded_at": "2026-01-02T00:00:00+00:00", "ticker": "TEST",
                  "strike": 100, "entry_spot": 100, "entry_premium": 6.0,
                  "entry_delta": 0.45, "final_score": 7.5, "label": "candidate",
                  "source": "13D", "thesis_cluster": "compute", "regime": "normal"})
    price_at = _provider().make_price_at()
    ev = store.evaluate(price_at=price_at, as_of=AS_OF)
    by_h = {e["horizon"]: e for e in ev}
    # +30d -> 2026-02-01 close 112 -> +12%
    assert abs(by_h[30]["underlying_return"] - 0.12) < 1e-9
    # benchmark QQQ 500 -> 510 = +2% ; excess = 12% - 2% = 10%
    assert abs(by_h[30]["benchmark_returns"]["QQQ"] - 0.02) < 1e-9
    assert abs(by_h[30]["excess_vs_benchmark"]["QQQ"] - 0.10) < 1e-9
    # 180d target (2026-07-01) is beyond the fixture series -> no row
    assert 180 not in by_h


def test_cli_outcomes_report_historical(tmp_path, capsys):
    store_path = tmp_path / "oc.jsonl"
    OutcomeStore(store_path).record(
        {"recorded_at": "2026-01-02T00:00:00+00:00", "ticker": "TEST",
         "entry_spot": 100, "entry_premium": 6.0, "entry_delta": 0.45,
         "final_score": 7.5, "label": "candidate", "thesis_cluster": "compute",
         "source": "13D", "regime": "normal"})
    rc = main(["outcomes-report", "--historical", "--store", str(store_path),
               "--min-sample", "1", "--split", "2025-12-01"])
    assert rc == 0
    out = capsys.readouterr().out
    report = json.loads(out[: out.index("\n\n")])
    assert report["summary"]["by_horizon"]
    assert report["summary"]["by_horizon"]["30"]["excess_vs_QQQ"]["n"] >= 1
