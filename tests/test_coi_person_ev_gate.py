"""Tests for the forward-EV hard gate (person_intel/ev_gate.py).

The gate encodes the goal: a CALL candidate only emerges when the actual option
structure has a plausibly POSITIVE expected value after costs AND no hard
data/liquidity/timing guard trips.
"""
import pytest

pytest.importorskip("numpy")

from call_options_intel.blackscholes import call_price
from call_options_intel.person_intel.ev_gate import (
    EvGate, EvGateConfig, forward_ev,
)


def _fair(spot, strike, dte, iv, r=0.04):
    """The market mid a real snapshot carries: BS-consistent with its own IV, so
    the EV model neither over- nor under-prices the entry (no manufactured edge)."""
    return round(call_price(spot, strike, dte / 365.0, r, iv), 2)


def _clean_snapshot(**over):
    spot, strike, dte, iv = 100.0, 100.0, 90, 0.45
    snap = {
        "ticker": "PLTR", "spot": spot, "strike": strike,
        "entry_premium": _fair(spot, strike, dte, iv), "iv": iv, "dte": dte,
        "spread_pct": 0.04, "open_interest": 5000, "volume": 800,
    }
    snap.update(over)
    return snap


# ── forward EV behaviour ─────────────────────────────────────────────────────
_MID = _fair(100.0, 100.0, 90, 0.45)


def test_forward_ev_is_deterministic_under_seed():
    kw = dict(ticker="PLTR", spot=100.0, strike=100.0, dte=90, entry_iv=0.45,
              entry_mid=_MID, drift_annual=0.0, n_paths=400, seed=11)
    a = forward_ev(**kw)
    b = forward_ev(**kw)
    assert a is not None and b is not None
    assert a.ev_pct == b.ev_pct and a.win_rate == b.win_rate


def test_zero_drift_long_call_has_negative_ev():
    # fairly-priced entry, no thesis edge → you pay the spread and rent theta,
    # and the TP/SL barriers on a (sub-)martingale add no free lunch → EV < 0
    ev = forward_ev(ticker="PLTR", spot=100.0, strike=100.0, dte=90,
                    entry_iv=0.45, entry_mid=_MID, drift_annual=0.0,
                    n_paths=1500, seed=7)
    assert ev is not None
    assert ev.ev_pct < 0.0


def test_ev_increases_with_drift():
    base = dict(ticker="PLTR", spot=100.0, strike=100.0, dte=90, entry_iv=0.45,
                entry_mid=_MID, n_paths=1500, seed=7)
    low = forward_ev(drift_annual=0.0, **base)
    high = forward_ev(drift_annual=1.0, **base)
    assert high.ev_pct > low.ev_pct
    assert high.win_rate >= low.win_rate


def test_forward_ev_none_on_incomplete():
    assert forward_ev(ticker="X", spot=0.0, strike=100.0, dte=90, entry_iv=0.4,
                      entry_mid=5.0, drift_annual=0.0) is None
    assert forward_ev(ticker="X", spot=100.0, strike=100.0, dte=90, entry_iv=0.4,
                      entry_mid=0.0, drift_annual=0.0) is None


# ── hard gates reject (never "warn") ─────────────────────────────────────────
def _gate():
    # small n_paths to keep the gate tests fast; drift supplied per-test
    return EvGate(EvGateConfig(n_paths=400))


def test_wide_spread_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(spread_pct=0.30), iv_rank=40.0,
                         earnings_in_dte=False, catalyst_drift_annual=1.0)
    assert d.passed is False
    assert "negative_ev" not in d.hard_fails
    assert any("spread" in r for r in d.reasons)


def test_thin_liquidity_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(open_interest=5, volume=0), iv_rank=40.0,
                         earnings_in_dte=False, catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("open interest" in r or "volume" in r for r in d.reasons)


def test_thin_dollar_volume_is_hard_rejected():
    # 6 contracts × ~$9 mid × 100 ≈ $5.6k « $25k floor → thin flow
    d = _gate().evaluate(_clean_snapshot(volume=6, open_interest=5000),
                         iv_rank=40.0, earnings_in_dte=False,
                         catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("$-volume" in r for r in d.reasons)


def test_penny_option_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(entry_premium=0.10, volume=100000),
                         iv_rank=40.0, earnings_in_dte=False,
                         catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("penny option" in r for r in d.reasons)


def test_missing_iv_history_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(), iv_rank=None, earnings_in_dte=False,
                         catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("IV history" in r for r in d.reasons)
    assert d.ev is None             # EV not even computed once a hard gate trips


def test_earnings_in_window_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(), iv_rank=40.0, earnings_in_dte=True,
                         catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("earnings" in r for r in d.reasons)


def test_dte_outside_window_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(dte=10), iv_rank=40.0,
                         earnings_in_dte=False, catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("DTE" in r for r in d.reasons)


def test_incomplete_snapshot_is_hard_rejected():
    d = _gate().evaluate(_clean_snapshot(entry_premium=None), iv_rank=40.0,
                         earnings_in_dte=False, catalyst_drift_annual=1.0)
    assert d.passed is False
    assert any("incomplete" in r for r in d.reasons)


# ── EV gate: positive expectancy passes, negative rejects ────────────────────
def test_clean_snapshot_negative_ev_rejected_without_edge():
    # all hard gates clear, but no thesis drift → negative EV → reject
    d = _gate().evaluate(_clean_snapshot(), iv_rank=40.0, earnings_in_dte=False,
                         catalyst_drift_annual=0.0)
    assert d.passed is False
    assert d.hard_fails == ["negative_ev"]
    assert d.ev is not None and d.ev.ev_pct <= 0.0


def test_clean_snapshot_with_strong_edge_passes():
    d = _gate().evaluate(_clean_snapshot(), iv_rank=40.0, earnings_in_dte=False,
                         catalyst_drift_annual=1.2)
    assert d.passed is True
    assert d.hard_fails == []
    assert d.ev is not None and d.ev.ev_pct > 0.0


def test_implied_drift_scales_with_score():
    cfg = EvGateConfig(signal_to_drift_annual_max=0.30,
                       default_catalyst_drift_annual=0.05)
    assert cfg.implied_drift_annual(None) == 0.05
    assert cfg.implied_drift_annual(10.0) == pytest.approx(0.30)
    assert cfg.implied_drift_annual(5.0) == pytest.approx(0.15)


def test_from_config_reads_shared_liquidity_and_dte():
    cfg = EvGateConfig.from_config({
        "liquidity": {"max_bid_ask_spread_pct": 0.12, "min_open_interest": 75,
                      "min_volume": 10},
        "dte": {"min": 45, "max": 150},
        "ev_gate": {"ev_min": 0.05, "n_paths": 200, "require_iv_history": False},
    })
    assert cfg.max_spread_pct == 0.12
    assert cfg.min_open_interest == 75
    assert cfg.dte_min == 45 and cfg.dte_max == 150
    assert cfg.ev_min == 0.05 and cfg.n_paths == 200
    assert cfg.require_iv_history is False
