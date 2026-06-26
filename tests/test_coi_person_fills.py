"""Tests for conservative fills, greeks/sim, IV warmup and paper sizing (Goal H)."""
from call_options_intel.person_intel.fills import (
    call_greeks, conservative_call_fill, iv_rank_with_warmup,
    paper_position_size, simulate_call,
)


# ── conservative fill ───────────────────────────────────────────────────────
def test_conservative_fill_between_mid_and_ask():
    q = conservative_call_fill(bid=1.00, ask=1.20, haircut=0.5)
    assert q.mid == 1.10
    assert 1.10 <= q.conservative_fill <= 1.20    # never the free mid
    assert q.conservative_fill > q.mid
    assert q.slippage_pct > 0
    assert q.basis == "bid_ask"


def test_fill_haircut_bounds():
    at_mid = conservative_call_fill(1.0, 1.2, haircut=0.0)
    at_ask = conservative_call_fill(1.0, 1.2, haircut=1.0)
    assert at_mid.conservative_fill == at_mid.mid
    assert abs(at_ask.conservative_fill - 1.2) < 1e-9


def test_fill_last_fallback_and_none():
    q = conservative_call_fill(bid=None, ask=None, last=2.0, last_slippage=0.05)
    assert q.basis == "last_haircut"
    assert q.conservative_fill > 2.0
    empty = conservative_call_fill(None, None, None)
    assert empty.basis == "none" and empty.conservative_fill is None


# ── greeks + simulation ─────────────────────────────────────────────────────
def test_call_greeks_signs():
    g = call_greeks(spot=100, strike=100, dte=60, r=0.04, sigma=0.5)
    assert 0 < g.delta < 1
    assert g.theta_per_day < 0          # long call bleeds theta
    assert g.vega_per_vol_pt > 0        # gains with IV


def test_flat_tape_loses_to_theta():
    entry = 8.0
    sim = simulate_call(spot=100, strike=100, dte=60, r=0.04, sigma=0.5,
                        entry_premium=entry, days_forward=30,
                        spot_move_pct=0.0, iv_change_pts=0.0)
    assert sim is not None
    assert sim.exit_premium < entry     # time decay with no move
    assert sim.pnl_pct < 0
    assert sim.theta_component < 0


def test_iv_crush_hurts_even_on_up_move():
    # small up move but a 20-pt IV crush (earnings) can still lose
    crush = simulate_call(100, 100, 60, 0.04, 0.6, entry_premium=9.0,
                          days_forward=5, spot_move_pct=0.02, iv_change_pts=-0.20)
    no_crush = simulate_call(100, 100, 60, 0.04, 0.6, entry_premium=9.0,
                             days_forward=5, spot_move_pct=0.02, iv_change_pts=0.0)
    assert crush.pnl_pct < no_crush.pnl_pct
    assert crush.vega_component < 0


def test_big_up_move_gains_and_loss_capped():
    win = simulate_call(100, 105, 90, 0.04, 0.5, entry_premium=6.0,
                        days_forward=20, spot_move_pct=0.25)
    assert win.pnl_pct > 0
    wipeout = simulate_call(100, 120, 30, 0.04, 0.5, entry_premium=6.0,
                            days_forward=30, spot_move_pct=-0.30)
    assert wipeout.pnl_pct == -1.0      # premium is the max loss


# ── IV warmup ───────────────────────────────────────────────────────────────
def test_iv_rank_warmup_returns_default():
    r = iv_rank_with_warmup(0.5, [0.4, 0.6], warmup_min_obs=20)
    assert r.warmed_up is False
    assert r.basis == "warmup_default"
    assert r.rank == 50.0


def test_iv_rank_after_warmup_is_computed():
    hist = [0.30 + 0.01 * i for i in range(25)]   # 0.30..0.54
    low = iv_rank_with_warmup(0.30, hist, warmup_min_obs=20)
    high = iv_rank_with_warmup(0.54, hist, warmup_min_obs=20)
    assert low.warmed_up and high.warmed_up
    assert low.rank < high.rank
    assert high.rank > 90


# ── paper sizing ────────────────────────────────────────────────────────────
def test_paper_sizing_and_cap():
    s = paper_position_size(book_value=100_000, risk_fraction=0.02,
                            entry_premium=5.0)        # budget 2000 / (5*100)=4
    assert s.contracts == 4
    assert s.dollar_risk == 2000.0
    assert "PAPER ONLY" in s.note


def test_paper_sizing_zero_on_bad_input():
    assert paper_position_size(0, 0.02, 5.0).contracts == 0
    assert paper_position_size(100_000, 0.02, 0).contracts == 0
    capped = paper_position_size(10_000_000, 1.0, 1.0, max_contracts=50)
    assert capped.contracts == 50
