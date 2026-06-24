"""Tests for Black-Scholes helpers (mathematical correctness)."""
import math

from call_options_intel import blackscholes as bs


def test_norm_cdf_known_values():
    assert abs(bs.norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(bs.norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(bs.norm_cdf(-1.96) - 0.025) < 1e-3


def test_atm_call_delta_near_half():
    # ATM call delta is slightly above 0.5 for positive drift/vol
    d = bs.call_delta(100, 100, 0.5, 0.04, 0.4)
    assert 0.5 < d < 0.65


def test_deep_itm_and_otm_delta_bounds():
    assert bs.call_delta(200, 100, 0.5, 0.04, 0.4) > 0.9
    assert bs.call_delta(50, 100, 0.5, 0.04, 0.4) < 0.1


def test_call_price_monotonic_in_spot():
    p_low = bs.call_price(90, 100, 0.5, 0.04, 0.4)
    p_high = bs.call_price(110, 100, 0.5, 0.04, 0.4)
    assert p_high > p_low > 0


def test_call_price_above_intrinsic():
    spot, strike, t, r, sigma = 110, 100, 0.5, 0.04, 0.4
    price = bs.call_price(spot, strike, t, r, sigma)
    intrinsic = max(0.0, spot - strike * math.exp(-r * t))
    assert price >= intrinsic


def test_implied_vol_roundtrip():
    spot, strike, t, r, sigma = 100, 105, 0.5, 0.04, 0.5
    price = bs.call_price(spot, strike, t, r, sigma)
    iv = bs.implied_vol_call(price, spot, strike, t, r)
    assert iv is not None
    assert abs(iv - sigma) < 1e-2


def test_degenerate_inputs_safe():
    assert bs.call_delta(0, 100, 0.5, 0.04, 0.4) == 0.0
    assert bs.call_delta(100, 100, 0, 0.04, 0.4) in (0.0, 1.0)
    assert bs.implied_vol_call(-1, 100, 100, 0.5, 0.04) is None
