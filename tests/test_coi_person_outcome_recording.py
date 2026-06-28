"""Tests for live outcome recording — every signal (incl. rejects) is stored."""
from datetime import date

from call_options_intel.person_intel.monitor import run_monitor
from call_options_intel.person_intel.monitor_report import monitor_and_notify
from call_options_intel.person_intel.outcomes import OutcomeStore
from call_options_intel.person_intel.proxy_map import load_proxy_map
from call_options_intel.person_intel.outcome_recorder import record_run, _decision

AS_OF = date(2026, 6, 27)


def _fake_snapshot(ticker):
    # a deterministic option snapshot for any ticker
    return {"spot": 100.0, "strike": 110.0, "entry_premium": 5.0,
            "entry_delta": 0.42, "iv": 0.45, "dte": 60, "spread_pct": 0.04,
            "open_interest": 1200, "volume": 800, "expiry": "2026-08-26"}


def _result(tmp_path):
    return run_monitor(mode="offline", as_of=AS_OF, since_days=120,
                       state_path=tmp_path / "seen.json", persist=False)


# ── recorder ─────────────────────────────────────────────────────────────────
def test_records_every_signal_including_rejects(tmp_path):
    res = _result(tmp_path)
    store = tmp_path / "out.jsonl"
    n = record_run(res, load_proxy_map(None), store_path=store,
                   snapshot_fn=_fake_snapshot, as_of=AS_OF)
    rows = OutcomeStore(store).load()
    # one row per NEW signal (filings + statements + vorfeld)
    assert n == len(rows)
    assert n == res.new_count + res.statement_count + res.vorfeld_count
    labels = {r["label"] for r in rows}
    assert "rejected" in labels            # rejects ARE recorded (the whole point)
    assert "top" in labels                 # gate-passers too


def test_row_has_full_decision_context(tmp_path):
    res = _result(tmp_path)
    store = tmp_path / "out.jsonl"
    record_run(res, load_proxy_map(None), store_path=store,
               snapshot_fn=_fake_snapshot, as_of=AS_OF)
    rows = OutcomeStore(store).load()
    r = next(x for x in rows if x.get("entry_spot"))
    for k in ("recorded_at", "source", "latency_days", "principal", "path_weight",
              "ticker", "thesis_cluster", "person_signal", "freshness",
              "tradeability", "final_score", "label", "entry_spot", "strike",
              "entry_premium", "entry_delta", "iv", "dte", "spread_pct"):
        assert k in r, f"missing {k}"


def test_top_and_reject_labels_match_gate():
    assert _decision({"label": "TRADE-CANDIDATE"}) == "top"
    assert _decision({"label": "WATCH"}) == "watch"
    assert _decision({"label": "NO-TRADE"}) == "rejected"


def test_signal_without_ticker_still_recorded(tmp_path):
    # a snapshot_fn returning None must not drop the row (audit trail)
    res = _result(tmp_path)
    store = tmp_path / "out.jsonl"
    n = record_run(res, load_proxy_map(None), store_path=store,
                   snapshot_fn=lambda t: None, as_of=AS_OF)
    rows = OutcomeStore(store).load()
    assert n == len(rows) and n > 0
    assert all("entry_spot" not in r for r in rows)   # no snapshot, still recorded


def test_append_only_accumulates(tmp_path):
    res = _result(tmp_path)
    store = tmp_path / "out.jsonl"
    pm = load_proxy_map(None)
    n1 = record_run(res, pm, store_path=store, snapshot_fn=_fake_snapshot, as_of=AS_OF)
    n2 = record_run(res, pm, store_path=store, snapshot_fn=_fake_snapshot, as_of=AS_OF)
    assert len(OutcomeStore(store).load()) == n1 + n2   # append-only


# ── matures via the existing evaluator ──────────────────────────────────────
def test_recorded_rows_mature_into_returns(tmp_path):
    res = _result(tmp_path)
    store = tmp_path / "out.jsonl"
    record_run(res, load_proxy_map(None), store_path=store,
               snapshot_fn=_fake_snapshot, as_of=AS_OF)
    os_store = OutcomeStore(store)
    # 40 days later, price up 20% -> the matured 7/14/30-day rows get a return
    later = date(2026, 8, 6)
    evaluated = os_store.evaluate(price_fn=lambda t, h: 120.0, as_of=later)
    assert evaluated
    matured = [e for e in evaluated if e.get("entry_spot")]
    assert matured
    assert any(abs(e["underlying_return"] - 0.20) < 1e-6 for e in matured)
    # rejected rows are evaluated too (so we can measure what we avoided)
    assert any(e["label"] == "rejected" for e in evaluated)


# ── monitor_and_notify integration ──────────────────────────────────────────
def test_monitor_and_notify_records(tmp_path):
    out = monitor_and_notify(
        mode="offline", as_of=AS_OF, since_days=120,
        state_path=tmp_path / "seen.json", artifact_dir=tmp_path / "art",
        send_email=False, record_outcomes=True,
        outcome_store_path=tmp_path / "out.jsonl")
    assert out["outcomes_recorded"] >= 1
    assert (tmp_path / "out.jsonl").exists()
