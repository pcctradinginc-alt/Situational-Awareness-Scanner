"""
options_sim.py
==============
A **realistic** path-based options backtest — not a proxy return.

The old `_option_proxy_return` is a first-order delta/intrinsic approximation. This
walks a long call **day by day** along the REAL underlying price path (and an IV
path when available), repricing with Black-Scholes so **theta decay** and
**vega / IV-crush** are captured, and applies real-world frictions and exits:

  * **Conservative fills** — buy crosses toward the ASK on entry, sell crosses
    toward the BID on exit (never mid); an assumed/known spread drives slippage.
  * **Exit rules**, checked daily in priority order:
      1. **IV-crush avoidance** — exit if IV collapses ≥ a threshold vs entry
      2. **Take-profit** — exit at +TP of premium
      3. **Stop-loss** — exit at −SL of premium
      4. **Time / DTE stop** — exit at max-hold days or when DTE gets too short
  * **Benchmarks over the same window** — the stock (buy-and-hold), QQQ, SOXX,
    and **"no trade"** (0.0), so the option is judged against the alternatives.

Everything is injectable (a ``price_on(ticker, date)`` callable and an optional
``iv_on(ticker, date)``), so it is deterministic and unit-tested offline; live it
is driven by the free Stooq history + the collected IV history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Callable, Optional

from ..blackscholes import call_price
from .fills import call_greeks, conservative_call_fill

PriceOn = Callable[[str, date], Optional[float]]
IVOn = Callable[[str, date], Optional[float]]


@dataclass(frozen=True)
class ExitRules:
    take_profit: float = 1.00       # +100% of premium → take profit
    stop_loss: float = 0.50         # −50% of premium → stop
    time_stop_days: int = 30        # max calendar days held
    min_dte: int = 7                # exit if remaining DTE falls below this
    iv_crush_exit_pts: float = 0.15 # exit if IV drops ≥ this many points vs entry
    entry_haircut: float = 0.50     # fraction of half-spread paid crossing to ask
    exit_haircut: float = 0.50      # fraction of half-spread given crossing to bid
    assumed_spread_pct: float = 0.06  # used when only a mid is known
    r: float = 0.04

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "ExitRules":
        g = (cfg or {}).get("options_backtest", {}) if cfg else {}
        d = cls()
        return cls(
            take_profit=float(g.get("take_profit", d.take_profit)),
            stop_loss=float(g.get("stop_loss", d.stop_loss)),
            time_stop_days=int(g.get("time_stop_days", d.time_stop_days)),
            min_dte=int(g.get("min_dte", d.min_dte)),
            iv_crush_exit_pts=float(g.get("iv_crush_exit_pts", d.iv_crush_exit_pts)),
            entry_haircut=float(g.get("entry_haircut", d.entry_haircut)),
            exit_haircut=float(g.get("exit_haircut", d.exit_haircut)),
            assumed_spread_pct=float(g.get("assumed_spread_pct", d.assumed_spread_pct)),
            r=float(g.get("risk_free_rate", d.r)))


@dataclass
class TradeSim:
    ticker: str
    entry_date: str
    exit_date: str
    days_held: int
    entry_fill: float
    exit_fill: float
    option_pnl_pct: float            # realised, conservative fills, capped at −1
    exit_reason: str                 # iv_crush | take_profit | stop_loss | time_stop | dte_stop | expiry | window_end | no_data
    iv_entry: Optional[float]
    iv_exit: Optional[float]
    theta_cost: float                # first-order attribution
    vega_pnl: float
    # alternatives over the SAME window
    stock_return: Optional[float]
    qqq_return: Optional[float]
    soxx_return: Optional[float]
    no_trade: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _bid_ask_from_mid(mid: float, spread_pct: float) -> tuple[float, float]:
    half = max(0.0, spread_pct) / 2.0 * mid
    return max(0.01, mid - half), mid + half


def _entry_fill(mid: float, rules: ExitRules) -> float:
    bid, ask = _bid_ask_from_mid(mid, rules.assumed_spread_pct)
    fq = conservative_call_fill(bid, ask, haircut=rules.entry_haircut)
    return fq.conservative_fill or mid


def _exit_fill(mid: float, rules: ExitRules) -> float:
    """A SELLER crosses toward the BID: mid − exit_haircut·(mid − bid)."""
    bid, _ = _bid_ask_from_mid(mid, rules.assumed_spread_pct)
    return round(max(0.0, mid - rules.exit_haircut * (mid - bid)), 4)


def _ret(provider_on, sym: str, d0: date, d1: date) -> Optional[float]:
    p0 = provider_on(sym, d0)
    p1 = provider_on(sym, d1)
    if p0 and p1 and p0 > 0:
        return round((p1 - p0) / p0, 4)
    return None


def simulate_option_trade(
    *, ticker: str, entry_date: date, strike: float, dte: int,
    entry_iv: float, entry_mid: float, price_on: PriceOn,
    iv_on: Optional[IVOn] = None, rules: Optional[ExitRules] = None,
) -> Optional[TradeSim]:
    """Walk a long call day-by-day with BS repricing + exit rules + frictions."""
    rules = rules or ExitRules()
    spot0 = price_on(ticker, entry_date)
    if not spot0 or not entry_mid or entry_mid <= 0 or not strike or strike <= 0:
        return None
    entry_fill = _entry_fill(entry_mid, rules)
    horizon = min(rules.time_stop_days, max(1, dte))

    g0 = call_greeks(spot0, strike, dte, rules.r, entry_iv or 0.4)
    last_spot, last_iv = spot0, entry_iv

    def _close(d: date, reason: str, exit_mid: float, iv_t: Optional[float]):
        exit_fill = _exit_fill(exit_mid, rules)
        pnl = max(-1.0, (exit_fill - entry_fill) / entry_fill)
        days = (d - entry_date).days
        theta_cost = round(g0.theta_per_day * days, 4)
        vega_pnl = (round(g0.vega_per_vol_pt * ((iv_t - entry_iv) * 100.0), 4)
                    if iv_t is not None and entry_iv is not None else 0.0)
        return TradeSim(
            ticker=ticker, entry_date=entry_date.isoformat(), exit_date=d.isoformat(),
            days_held=days, entry_fill=round(entry_fill, 4),
            exit_fill=round(exit_fill, 4), option_pnl_pct=round(pnl, 4),
            exit_reason=reason, iv_entry=entry_iv, iv_exit=iv_t,
            theta_cost=theta_cost, vega_pnl=vega_pnl,
            stock_return=_ret(price_on, ticker, entry_date, d),
            qqq_return=_ret(price_on, "QQQ", entry_date, d),
            soxx_return=_ret(price_on, "SOXX", entry_date, d))

    for n in range(1, horizon + 1):
        d = entry_date + timedelta(days=n)
        spot_t = price_on(ticker, d)
        if spot_t is None:
            spot_t = last_spot                # carry last close over a gap day
        iv_t = (iv_on(ticker, d) if iv_on else None)
        if iv_t is None:
            iv_t = last_iv
        last_spot, last_iv = spot_t, iv_t
        dte_t = dte - n
        t_t = max(0.0, dte_t / 365.0)
        mid_t = (max(0.0, spot_t - strike) if t_t <= 0
                 else call_price(spot_t, strike, t_t, rules.r, max(0.01, iv_t or 0.4)))
        pnl_mon = (mid_t - entry_fill) / entry_fill

        # exit rules in priority order
        if (entry_iv is not None and iv_t is not None
                and (entry_iv - iv_t) >= rules.iv_crush_exit_pts):
            return _close(d, "iv_crush", mid_t, iv_t)
        if pnl_mon >= rules.take_profit:
            return _close(d, "take_profit", mid_t, iv_t)
        if pnl_mon <= -rules.stop_loss:
            return _close(d, "stop_loss", mid_t, iv_t)
        if t_t <= 0:
            return _close(d, "expiry", mid_t, iv_t)
        if dte_t <= rules.min_dte:
            return _close(d, "dte_stop", mid_t, iv_t)
        if n >= rules.time_stop_days:
            return _close(d, "time_stop", mid_t, iv_t)

    # window ended without a rule firing → exit at the last evaluated day
    d = entry_date + timedelta(days=horizon)
    iv_t = (iv_on(ticker, d) if iv_on else None) or last_iv
    dte_t = max(0.0, (dte - horizon) / 365.0)
    mid_t = (max(0.0, last_spot - strike) if dte_t <= 0
             else call_price(last_spot, strike, dte_t, rules.r, max(0.01, iv_t or 0.4)))
    return _close(d, "window_end", mid_t, iv_t)


# ── aggregate a set of recorded signals into a realistic backtest summary ────
def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    wins = [x for x in xs if x > 0]
    return {"n": len(xs), "hit_rate": round(len(wins) / len(xs), 3),
            "avg": round(sum(xs) / len(xs), 4),
            "best": round(max(xs), 4), "worst": round(min(xs), 4)}


def backtest_signals(rows: list[dict], price_on: PriceOn,
                     iv_on: Optional[IVOn] = None,
                     rules: Optional[ExitRules] = None) -> dict:
    """Run the realistic sim over recorded outcome rows that carry an option
    snapshot, and summarise option P&L vs stock / QQQ / SOXX / no-trade."""
    sims: list[TradeSim] = []
    for r in rows:
        if r.get("label") == "rejected":
            continue
        tkr = r.get("ticker")
        strike = r.get("strike")
        prem = r.get("entry_premium")
        iv = r.get("iv")
        dte = r.get("dte")
        try:
            ed = date.fromisoformat(str(r.get("recorded_at"))[:10])
        except (ValueError, TypeError):
            continue
        if not (tkr and strike and prem and iv and dte):
            continue
        s = simulate_option_trade(
            ticker=tkr, entry_date=ed, strike=float(strike), dte=int(dte),
            entry_iv=float(iv), entry_mid=float(prem), price_on=price_on,
            iv_on=iv_on, rules=rules)
        if s:
            sims.append(s)

    if not sims:
        return {"n": 0, "note": "no signals with a complete option snapshot + price history"}
    opt = [s.option_pnl_pct for s in sims]
    stock = [s.stock_return for s in sims if s.stock_return is not None]
    qqq = [s.qqq_return for s in sims if s.qqq_return is not None]
    soxx = [s.soxx_return for s in sims if s.soxx_return is not None]
    reasons: dict[str, int] = {}
    for s in sims:
        reasons[s.exit_reason] = reasons.get(s.exit_reason, 0) + 1
    return {
        "n": len(sims),
        "option_pnl": _stats(opt),
        "vs_stock": _stats(stock),
        "vs_qqq": _stats(qqq),
        "vs_soxx": _stats(soxx),
        "no_trade": {"avg": 0.0, "note": "the do-nothing alternative"},
        "exit_reasons": reasons,
        "caveat": ("Realistic path sim: conservative fills (ask in / bid out), "
                   "BS reprice for theta+vega, daily exit rules. Still a model — "
                   "real fills depend on live liquidity; no profitability is "
                   "claimed without the walk-forward out-of-sample guard."),
    }
