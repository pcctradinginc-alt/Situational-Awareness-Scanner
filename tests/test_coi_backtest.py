"""Tests for the paper-evaluation / signal store."""
from datetime import datetime, timedelta, timezone

from call_options_intel.backtest import SignalStore
from call_options_intel.models import (
    ScanResult, OptionCandidate, OptionContract, SignalBreakdown,
)


def _make_result():
    c = OptionContract(ticker="NVDA", expiry="2026-12-18", strike=120, dte=90,
                       bid=7.8, ask=8.2, iv=0.45, open_interest=5000, volume=1000,
                       delta=0.45, spot=110)
    cand = OptionCandidate(ticker="NVDA", name="NVIDIA", category="semis",
                           contract=c, final_score=7.4,
                           breakdown=SignalBreakdown(final_score=7.4),
                           confidence_label="high")
    r = ScanResult(generated_at=datetime.now(timezone.utc).isoformat())
    r.candidates = [cand]
    r.top = [cand]
    return r


def test_record_and_load(tmp_path):
    store = SignalStore(tmp_path / "signals.jsonl")
    n = store.record(_make_result(), horizon_days=30)
    assert n == 1
    rows = store.load()
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["entry_premium"] == 8.0


def test_evaluate_only_matured(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    # write one old (matured) and one fresh
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    import json
    with store.path.open("w") as fh:
        fh.write(json.dumps({"recorded_at": old, "ticker": "NVDA", "strike": 100,
                             "entry_spot": 100, "entry_premium": 8, "entry_delta": 0.5,
                             "final_score": 7, "horizon_days": 30}) + "\n")
        fh.write(json.dumps({"recorded_at": fresh, "ticker": "MU", "strike": 80,
                             "entry_spot": 80, "entry_premium": 5, "entry_delta": 0.4,
                             "final_score": 6, "horizon_days": 30}) + "\n")
    evaluated = store.evaluate(lambda t: {"NVDA": 120, "MU": 90}[t])
    assert len(evaluated) == 1                    # only matured NVDA
    e = evaluated[0]
    assert e["underlying_return"] == 0.2          # 100 -> 120
    # the delta/intrinsic option proxy was removed — only the real underlying remains
    assert "option_proxy_return" not in e


def test_summarize_metrics(tmp_path):
    store = SignalStore(tmp_path / "s.jsonl")
    evaluated = [
        {"ticker": "A", "final_score": 8, "underlying_return": 0.1},
        {"ticker": "B", "final_score": 6, "underlying_return": -0.05},
        {"ticker": "C", "final_score": 4, "underlying_return": -0.2},
    ]
    summary = store.summarize(evaluated)
    assert summary["n"] == 3
    assert 0 <= summary["underlying_return"]["hit_rate"] <= 1
    assert "option_proxy" not in summary          # proxy performance is gone
    assert "score_buckets" in summary
    assert "caveat" in summary  # no profitability claim without caveat


def test_summarize_empty():
    assert SignalStore("/tmp/none.jsonl").summarize([])["n"] == 0
