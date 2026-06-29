"""Tests for the per-bucket score-calibration harness (data-gated verdict)."""
from datetime import date

from call_options_intel.person_intel.score_calibration import (
    calibrate_by_bucket, score_bucket,
)


def _ramp_price_on(slope=0.004):
    """A deterministic rising path for NVDA + flat-ish benchmarks."""
    base = {"NVDA": 100.0, "QQQ": 400.0, "SOXX": 200.0}

    def price_on(t, d):
        b = base.get(t)
        if b is None:
            return None
        days = max(0, (d - date(2026, 1, 1)).days)
        mult = (1 + slope * days) if t == "NVDA" else (1 + 0.0005 * days)
        return b * mult

    return price_on


def _row(score, rec="2026-01-01"):
    return {"ticker": "NVDA", "strike": 100, "entry_premium": 6.0, "iv": 0.45,
            "dte": 200, "final_score": score, "recorded_at": rec, "label": "top"}


def test_score_bucket_edges():
    assert score_bucket(8) == "high>=7"
    assert score_bucket(5) == "mid5-7"
    assert score_bucket(4.9) == "low<5"


def test_calibration_structure_and_insufficient_until_min_sample():
    out = calibrate_by_bucket([_row(8.0)], _ramp_price_on(), min_sample=5)
    assert "buckets" in out and "monotonicity" in out
    cell = out["buckets"]["high>=7"][30]["option"]
    assert cell["insufficient"] is True            # 1 « 5 → no premature claim
    # every horizon's monotonicity is 'insufficient' on this tiny sample
    assert set(out["monotonicity"].values()) == {"insufficient"}


def test_calibration_reports_stats_when_enough_samples():
    rows = [_row(8.0) for _ in range(6)]            # >= min_sample in high bucket
    out = calibrate_by_bucket(rows, _ramp_price_on(), min_sample=5)
    opt = out["buckets"]["high>=7"][30]["option"]
    assert opt["n"] == 6 and not opt.get("insufficient")
    assert "avg" in opt and "hit_rate" in opt
    # the underlying rose on the ramp → its 30d avg is positive
    assert out["buckets"]["high>=7"][30]["underlying"]["avg"] > 0
