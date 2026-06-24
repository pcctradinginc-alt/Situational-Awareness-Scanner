"""
blackscholes.py
===============
Minimal, dependency-free Black-Scholes helpers used to ESTIMATE option greeks
when a free data source does not provide them. Estimates are always flagged as
estimated downstream. Uses math.erf for the normal CDF (no scipy/numpy needed).

These are standard textbook formulas; they are unit-tested against known values.
"""

from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    return (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def call_delta(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    """Black-Scholes CALL delta. Returns 0..1.

    Degrades safely: non-positive inputs or zero time/vol return an intrinsic
    approximation (1.0 if in-the-money, else 0.0).
    """
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    return norm_cdf(_d1(spot, strike, t, r, sigma))


def call_price(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    """Black-Scholes CALL price."""
    if t <= 0 or sigma <= 0:
        return max(0.0, spot - strike)
    d1 = _d1(spot, strike, t, r, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    return spot * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2)


def implied_vol_call(
    price: float, spot: float, strike: float, t: float, r: float,
    lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-5, iters: int = 100,
) -> float | None:
    """Invert BS for implied vol via bisection. Returns None if not bracketable."""
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    intrinsic = max(0.0, spot - strike * math.exp(-r * t))
    if price < intrinsic - tol:
        return None
    f_lo = call_price(spot, strike, t, r, lo) - price
    f_hi = call_price(spot, strike, t, r, hi) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        f_mid = call_price(spot, strike, t, r, mid) - price
        if abs(f_mid) < tol:
            return round(mid, 4)
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return round(0.5 * (lo + hi), 4)
