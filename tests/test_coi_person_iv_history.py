"""Tests for the IV-history store and warmup-gated richness band (Phase 2)."""
from call_options_intel.cli import main
from call_options_intel.person_intel.iv_history import IVHistoryStore, richness_band


def test_store_record_and_history(tmp_path):
    s = IVHistoryStore(tmp_path / "iv.jsonl")
    assert s.record("NVDA", 0.45) is True
    assert s.record("NVDA", 0.50) is True
    assert s.record("NVDA", 0.0) is False          # non-positive rejected
    assert s.history("NVDA") == [0.45, 0.50]
    assert s.history("MU") == []


def test_record_many_counts(tmp_path):
    s = IVHistoryStore(tmp_path / "iv.jsonl")
    n = s.record_many({"NVDA": 0.4, "MU": 0.6, "BAD": 0.0})
    assert n == 2


def test_record_many_is_idempotent_per_day(tmp_path):
    # a twice-daily workflow must yield exactly ONE observation per ticker/day,
    # otherwise the IV rank double-counts calm days.
    from datetime import date
    s = IVHistoryStore(tmp_path / "iv.jsonl")
    d1, d2 = date(2026, 7, 3), date(2026, 7, 4)
    assert s.record_many({"NVDA": 0.4, "MU": 0.6}, as_of=d1) == 2
    assert s.record_many({"NVDA": 0.41, "MU": 0.61}, as_of=d1) == 0  # same day
    assert s.record_many({"NVDA": 0.42}, as_of=d2) == 1              # next day
    assert s.history("NVDA") == [0.4, 0.42]


def test_rank_warmup_then_computed(tmp_path):
    s = IVHistoryStore(tmp_path / "iv.jsonl")
    for v in [0.30 + 0.01 * i for i in range(25)]:
        s.record("NVDA", v)
    cold = s.rank("MU", 0.5, warmup_min_obs=20)
    assert cold.warmed_up is False and cold.basis == "warmup_default"
    warm = s.rank("NVDA", 0.54, warmup_min_obs=20)
    assert warm.warmed_up is True and warm.rank > 90


def test_richness_band_pre_warmup_uses_proxy():
    band, val, basis = richness_band(current_iv=0.6, hist_vol=0.4,
                                     history=[0.5, 0.55], risk_cfg={},
                                     warmup_min_obs=20)
    assert basis == "realised_vol_proxy"
    assert band is not None


def test_richness_band_after_warmup_uses_history():
    hist = [0.30 + 0.01 * i for i in range(25)]      # 0.30..0.54
    hi_band, hi_pct, basis = richness_band(0.54, 0.4, hist, {}, warmup_min_obs=20)
    assert basis == "iv_history"
    assert hi_band in ("elevated", "extreme")
    lo_band, _, _ = richness_band(0.31, 0.4, hist, {}, warmup_min_obs=20)
    assert lo_band == "fair"


def test_cli_record_iv_runs(tmp_path):
    rc = main(["record-iv", "--store", str(tmp_path / "iv.jsonl"),
               "--tickers", "NVDA", "MU"])
    assert rc == 0
    # at least one ATM IV should have been written from the options fixture
    assert (tmp_path / "iv.jsonl").exists()
