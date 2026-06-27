"""
fills.py
========
Goal H — make the options side realistic and conservative.

  * **Conservative fill** instead of mid: a buyer of a call does not get filled at
    the midpoint — model crossing toward the ask with a configurable haircut.
  * **Theta / vega** greeks and a full Black-Scholes **reprice simulation** so a
    long call into a flat tape (theta drag) and an earnings IV-crush (vega hit)
    are both penalised explicitly, not hand-waved.
  * **IV rank only after a warmup** window — before enough IV history exists, fall
    back to a neutral default instead of pretending to know the percentile.
  * **Paper-only sizing** — a risk-fraction of a notional paper book. Never an
    order; there is no broker code here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..blackscholes import call_delta, call_price

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


# ── conservative fill (no free mid) ─────────────────────────────────────────
@dataclass
class FillQuote:
    mid: Optional[float]
    conservative_fill: Optional[float]
    spread_pct: Optional[float]
    slippage_pct: Optional[float]      # (conservative - mid) / mid
    basis: str                          # bid_ask | last_haircut | none


def conservative_call_fill(
    bid: Optional[float], ask: Optional[float], last: Optional[float] = None,
    haircut: float = 0.5, last_slippage: float = 0.05,
) -> FillQuote:
    """Estimate the price a BUYER realistically pays for a call.

    With a two-sided market the fill is ``mid + haircut*(ask - mid)`` — i.e. it
    sits between the mid and the ask (haircut 0 = mid, 1 = ask). With only a last
    price, apply a flat upward slippage. The conservative fill is always ≥ mid, so
    downstream P&L is never flattered by assuming a mid fill.
    """
    haircut = max(0.0, min(1.0, haircut))
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        fill = mid + haircut * (ask - mid)
        spread_pct = ((ask - bid) / mid) if mid > 0 else None
        slip = ((fill - mid) / mid) if mid > 0 else None
        return FillQuote(round(mid, 4), round(fill, 4),
                         round(spread_pct, 4) if spread_pct is not None else None,
                         round(slip, 4) if slip is not None else None, "bid_ask")
    if last is not None and last > 0:
        fill = last * (1.0 + last_slippage)
        return FillQuote(round(last, 4), round(fill, 4), None,
                         round(last_slippage, 4), "last_haircut")
    return FillQuote(None, None, None, None, "none")


# ── greeks + reprice simulation ─────────────────────────────────────────────
@dataclass
class Greeks:
    delta: float
    theta_per_day: float               # $ change per calendar day (negative)
    vega_per_vol_pt: float             # $ change per +1 IV percentage point


def call_greeks(spot: float, strike: float, dte: int, r: float, sigma: float) -> Greeks:
    """Black-Scholes delta, theta (per day) and vega (per 1 vol point)."""
    t = max(dte, 0) / 365.0
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return Greeks(1.0 if spot > strike else 0.0, 0.0, 0.0)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    delta = call_delta(spot, strike, t, r, sigma)
    theta_year = (-(spot * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(t))
                  - r * strike * math.exp(-r * t) * _cdf(d2))
    vega_full = spot * _norm_pdf(d1) * math.sqrt(t)       # per 1.00 vol
    return Greeks(round(delta, 4), round(theta_year / 365.0, 4),
                  round(vega_full / 100.0, 4))            # per 1 vol point


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class SimResult:
    entry_premium: float
    exit_premium: float
    pnl_pct: float                     # capped at -1.0 (premium is max loss)
    spot_move_pct: float
    iv_change_pts: float
    days_forward: int
    theta_component: float
    vega_component: float


def simulate_call(
    spot: float, strike: float, dte: int, r: float, sigma: float,
    entry_premium: float, days_forward: int,
    spot_move_pct: float = 0.0, iv_change_pts: float = 0.0,
) -> Optional[SimResult]:
    """Reprice a long call after ``days_forward`` under a spot move and IV change.

    Uses a full BS reprice (so theta decay AND vega/IV-crush are captured), then
    reports realised P&L vs the (conservative) entry premium. ``iv_change_pts`` is
    in IV percentage points (e.g. -0.20 = a 20-pt IV crush around earnings).
    """
    if entry_premium is None or entry_premium <= 0:
        return None
    t2 = max(0.0, (dte - days_forward) / 365.0)
    spot2 = spot * (1.0 + spot_move_pct)
    sigma2 = max(0.01, sigma + iv_change_pts)
    if t2 <= 0:
        exit_premium = max(0.0, spot2 - strike)
    else:
        exit_premium = call_price(spot2, strike, t2, r, sigma2)
    pnl = max(-1.0, (exit_premium - entry_premium) / entry_premium)

    # attribute (first-order, for transparency) the theta and vega contributions
    g = call_greeks(spot, strike, dte, r, sigma)
    theta_comp = g.theta_per_day * days_forward
    vega_comp = g.vega_per_vol_pt * (iv_change_pts * 100.0)
    return SimResult(
        entry_premium=round(entry_premium, 4), exit_premium=round(exit_premium, 4),
        pnl_pct=round(pnl, 4), spot_move_pct=spot_move_pct,
        iv_change_pts=iv_change_pts, days_forward=days_forward,
        theta_component=round(theta_comp, 4), vega_component=round(vega_comp, 4),
    )


# ── IV rank with warmup ─────────────────────────────────────────────────────
@dataclass
class IVRank:
    rank: float                        # 0..100
    warmed_up: bool
    basis: str                         # history | warmup_default
    n_obs: int


def iv_rank_with_warmup(
    current_iv: Optional[float], history: list[float] | None,
    warmup_min_obs: int = 20, default_rank: float = 50.0,
) -> IVRank:
    """IV rank vs an own-collected IV history, but ONLY once warmed up.

    Before ``warmup_min_obs`` observations exist, return the neutral default
    instead of a falsely precise percentile.
    """
    hist = [h for h in (history or []) if h is not None]
    if current_iv is None or len(hist) < warmup_min_obs:
        return IVRank(round(default_rank, 1), False, "warmup_default", len(hist))
    lo, hi = min(hist), max(hist)
    rank = 50.0 if hi <= lo else 100.0 * (current_iv - lo) / (hi - lo)
    return IVRank(round(max(0.0, min(100.0, rank)), 1), True, "history", len(hist))


# ── paper-only sizing ───────────────────────────────────────────────────────
@dataclass
class PaperSize:
    contracts: int
    dollar_risk: float
    note: str = ("PAPER ONLY — sizing for a notional research book. "
                 "Not an order; this system never trades.")


def paper_position_size(
    book_value: float, risk_fraction: float, entry_premium: float,
    contract_multiplier: int = 100, max_contracts: int = 50,
) -> PaperSize:
    """Number of contracts a risk-fraction of a PAPER book would buy.

    Long-call max loss is the premium, so dollars-at-risk = contracts × premium ×
    multiplier. Returns 0 when inputs are unusable. Never places an order.
    """
    if (book_value <= 0 or risk_fraction <= 0 or entry_premium is None
            or entry_premium <= 0):
        return PaperSize(0, 0.0)
    budget = book_value * max(0.0, min(1.0, risk_fraction))
    per_contract = entry_premium * contract_multiplier
    n = int(budget // per_contract)
    n = max(0, min(max_contracts, n))
    return PaperSize(n, round(n * per_contract, 2))
