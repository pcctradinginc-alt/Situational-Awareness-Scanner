"""Tests for multi-horizon outcome learning + walk-forward guard (Goal I)."""
from datetime import date, datetime, timedelta, timezone

from call_options_intel.person_intel.outcomes import (
    HORIZONS, OutcomeStore, summarize, walk_forward_guard,
)

AS_OF = date(2026, 6, 26)


def _iso(days_ago: int) -> str:
    return (datetime(2026, 6, 26, tzinfo=timezone.utc) - timedelta(days=days_ago)) \
        .isoformat(timespec="seconds")


def _row(ticker, days_ago, spot, premium, score, **over):
    base = {
        "recorded_at": _iso(days_ago), "ticker": ticker, "strike": spot,
        "entry_spot": spot, "entry_premium": premium, "entry_delta": 0.45,
        "iv": 0.5, "final_score": score, "label": "candidate",
        "source": "13F", "thesis_cluster": "compute", "regime": "normal",
    }
    base.update(over)
    return base


def test_store_is_append_only_and_records_rejected(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    store.record(_row("NVDA", 200, 100, 8.0, 7.5))
    store.record(_row("BADCO", 200, 50, 4.0, 2.0, label="rejected"))
    rows = store.load()
    assert len(rows) == 2
    assert any(r["label"] == "rejected" for r in rows)   # rejected kept too


def test_evaluate_matures_only_passed_horizons(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    store.record(_row("NVDA", 45, 100, 8.0, 7.5))        # 45d old
    price_fn = lambda t, h: 100 * (1 + 0.01 * h / 30)    # grows with horizon
    ev = store.evaluate(price_fn, as_of=AS_OF)
    horizons = {e["horizon"] for e in ev}
    assert horizons == {h for h in HORIZONS if h <= 45}  # 7,14,30 only
    assert all(e["underlying_return"] > 0 for e in ev)


def test_evaluate_benchmark_excess(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    store.record(_row("NVDA", 200, 100, 8.0, 7.5))
    price_fn = lambda t, h: 110.0                         # +10%
    bench_fn = lambda sym, h: 0.04                        # benchmarks +4%
    ev = store.evaluate(price_fn, bench_fn, as_of=AS_OF)
    e30 = next(e for e in ev if e["horizon"] == 30)
    assert e30["benchmark_returns"]["QQQ"] == 0.04
    assert abs(e30["excess_vs_benchmark"]["QQQ"] - 0.06) < 1e-6   # 10% - 4%


def test_summarize_buckets_and_rejected_view(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    store.record(_row("NVDA", 200, 100, 8.0, 7.5, source="13D"))
    store.record(_row("AMD", 200, 100, 6.0, 5.5, thesis_cluster="networking"))
    store.record(_row("BADCO", 200, 100, 9.0, 2.0, label="rejected"))
    price_fn = lambda t, h: (130.0 if t in ("NVDA", "AMD") else 70.0)
    bench_fn = lambda sym, h: 0.05
    summary = summarize(store.evaluate(price_fn, bench_fn, as_of=AS_OF))
    assert summary["by_horizon"]
    assert "high>=7" in summary["score_buckets"]
    assert "13D" in summary["source_buckets"]
    assert "networking" in summary["thesis_buckets"]
    assert "normal" in summary["regime_buckets"]
    # rejected name fell -30%, accepted rose -> we correctly avoided it
    rv = summary["rejected_vs_accepted"]
    assert rv["accepted_underlying"]["avg_return"] > rv["rejected_underlying"]["avg_return"]


def test_walk_forward_guard_requires_min_sample(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    # only in-sample data (all before split) -> OOS empty -> no claim
    for i in range(5):
        store.record(_row(f"T{i}", 200, 100, 8.0, 7.5))
    price_fn = lambda t, h: 120.0
    ev = store.evaluate(price_fn, as_of=AS_OF)
    g = walk_forward_guard(ev, split_date="2026-03-01", horizon=30, min_sample=3)
    assert g["edge_claim"] == "insufficient_oos_sample"


def test_walk_forward_guard_positive_oos(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    # in-sample (recorded ~190d ago, before split) + OOS (recorded ~100d ago, after)
    for i in range(4):
        store.record(_row(f"IS{i}", 190, 100, 8.0, 7.5))
    for i in range(4):
        store.record(_row(f"OOS{i}", 100, 100, 8.0, 7.5))
    price_fn = lambda t, h: 140.0                        # everything wins
    ev = store.evaluate(price_fn, as_of=AS_OF)
    g = walk_forward_guard(ev, split_date="2026-03-01", horizon=30, min_sample=3)
    assert g["out_of_sample"]["n"] >= 3
    assert g["edge_claim"] == "positive_oos"


def test_walk_forward_guard_no_edge_when_losing(tmp_path):
    store = OutcomeStore(tmp_path / "out.jsonl")
    for i in range(5):
        store.record(_row(f"OOS{i}", 100, 100, 8.0, 7.5))
    price_fn = lambda t, h: 60.0                         # everything loses
    ev = store.evaluate(price_fn, as_of=AS_OF)
    g = walk_forward_guard(ev, split_date="2026-03-01", horizon=30, min_sample=3)
    assert g["edge_claim"] == "no_oos_edge"
