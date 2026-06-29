"""
ev_gate.py
==========
The **fourth, decisive gate**: a CALL candidate may only emerge when its *option
structure* carries a plausibly **positive expected value (EV) after costs** — and
when none of the hard data/liquidity/timing guards trip. The three-axis gate
(:mod:`triple_score`) answers *"is this a NEW, verifiable, early person/capital
signal on a resolvable public proxy?"*. It does **not** answer *"does buying this
specific call actually pay?"*. Without that, the system ranks stories, not trades.

This module closes that gap with two things, both HARD (reject, never "warn"):

1. **Forward EV** (:func:`forward_ev`) — a Monte-Carlo of the *actual* contract.
   We simulate many GBM price paths and run each one through the SAME realistic
   engine the backtest uses (:func:`options_sim.simulate_option_trade`): you buy
   crossing toward the **ask**, sell crossing toward the **bid**, Black-Scholes
   reprice so **theta** bleeds and **vega** moves, and the daily **exit rules**
   (take-profit / stop / DTE / time) fire. Averaging the realised P&L over the
   path distribution gives the expected option P&L *after* spread, slippage,
   theta and the exit policy. With no thesis-implied drift a long call has
   **negative** EV (you pay the spread and rent theta) — which is the honest
   default: EV must be earned by a real, sized catalyst expectation, not asserted.

2. **Hard gates** (:func:`EvGate.evaluate`) — unresolved/incomplete snapshot,
   wide spread, thin OI/volume, DTE outside the tradeable window, **missing IV
   history** (cannot judge if IV is rich), or **earnings inside the holding
   window** each REJECT the candidate outright. Capital is protected by gates,
   not by footnotes.

Thresholds come from ``config/risk_thresholds.yml`` (the same liquidity/DTE/
earnings numbers the rest of the system already uses — single source of truth),
plus an ``ev_gate`` block for the EV-specific knobs. Everything is offline,
seeded and deterministic; live it is fed the real option snapshot + IV history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional

from .options_sim import ExitRules, simulate_option_trade

logger = logging.getLogger("coi.person.ev_gate")

# a fixed, arbitrary anchor date — only day-offsets matter inside the sim
_ANCHOR = date(2026, 1, 1)


# ── forward EV result ───────────────────────────────────────────────────────
@dataclass
class EvResult:
    ev_pct: float                 # mean realised option P&L after costs (e.g. -0.18)
    win_rate: float               # fraction of paths ending > 0
    p10: float
    p50: float
    p90: float
    mean_days: float
    n_paths: int
    drift_annual: float
    vol_annual: float
    exit_reasons: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ev_pct": round(self.ev_pct, 4), "win_rate": round(self.win_rate, 3),
            "p10": round(self.p10, 4), "p50": round(self.p50, 4),
            "p90": round(self.p90, 4), "mean_days": round(self.mean_days, 1),
            "n_paths": self.n_paths, "drift_annual": round(self.drift_annual, 4),
            "vol_annual": round(self.vol_annual, 4),
            "exit_reasons": self.exit_reasons,
        }


def _gbm_paths(spot: float, mu: float, sigma: float, days: int,
               n_paths: int, rng) -> "list":
    """Vectorised GBM: returns an (n_paths, days+1) price matrix, column 0 = spot."""
    import numpy as np
    dt = 1.0 / 365.0
    z = rng.standard_normal((n_paths, days))
    incr = (mu - 0.5 * sigma * sigma) * dt + sigma * (dt ** 0.5) * z
    logp = np.cumsum(incr, axis=1)
    body = spot * np.exp(logp)
    head = np.full((n_paths, 1), spot, dtype=float)
    return np.concatenate([head, body], axis=1)


def forward_ev(*, ticker: str, spot: float, strike: float, dte: int,
               entry_iv: float, entry_mid: float, drift_annual: float,
               vol_annual: Optional[float] = None, n_paths: int = 1500,
               seed: int = 7, rules: Optional[ExitRules] = None,
               spread_pct: Optional[float] = None) -> Optional[EvResult]:
    """Monte-Carlo the *actual* contract through the realistic options engine and
    return the expected P&L distribution after fills/theta/vega/exit-rules.

    ``drift_annual`` is the thesis-implied expected annual underlying drift — the
    ONLY place a directional edge enters. 0.0 ⇒ no assumed edge ⇒ honest theta
    drag. ``vol_annual`` defaults to the contract's own IV.
    """
    import numpy as np
    if not (spot and strike and entry_mid and dte) or spot <= 0 or strike <= 0 \
            or entry_mid <= 0 or dte <= 0:
        return None
    rules = rules or ExitRules()
    if spread_pct is not None and spread_pct >= 0:
        rules = replace(rules, assumed_spread_pct=float(spread_pct))
    sigma = float(vol_annual) if vol_annual else float(entry_iv or 0.4)
    sigma = max(0.01, sigma)
    horizon = min(rules.time_stop_days, max(1, int(dte)))
    rng = np.random.default_rng(seed)
    paths = _gbm_paths(spot, drift_annual, sigma, horizon, n_paths, rng)

    pnls: list[float] = []
    days: list[int] = []
    reasons: dict[str, int] = {}
    for i in range(n_paths):
        row = paths[i]
        series = {_ANCHOR + timedelta(days=n): float(row[n])
                  for n in range(horizon + 1)}

        def price_on(tk: str, d: date, _s=series) -> Optional[float]:
            return _s.get(d) if tk == ticker else None

        sim = simulate_option_trade(
            ticker=ticker, entry_date=_ANCHOR, strike=float(strike),
            dte=int(dte), entry_iv=float(entry_iv or sigma),
            entry_mid=float(entry_mid), price_on=price_on, iv_on=None,
            rules=rules)
        if sim is None:
            continue
        pnls.append(sim.option_pnl_pct)
        days.append(sim.days_held)
        reasons[sim.exit_reason] = reasons.get(sim.exit_reason, 0) + 1

    if not pnls:
        return None
    arr = np.array(pnls, dtype=float)
    return EvResult(
        ev_pct=float(arr.mean()),
        win_rate=float((arr > 0).mean()),
        p10=float(np.percentile(arr, 10)),
        p50=float(np.percentile(arr, 50)),
        p90=float(np.percentile(arr, 90)),
        mean_days=float(sum(days) / len(days)),
        n_paths=len(pnls), drift_annual=float(drift_annual),
        vol_annual=sigma, exit_reasons=reasons)


# ── the gate ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EvGateConfig:
    # EV
    ev_min: float = 0.0                 # required mean option P&L after costs
    n_paths: int = 1500
    seed: int = 7
    default_catalyst_drift_annual: float = 0.0
    # map a 0..10 final trade score to an assumed annual drift PRIOR (unvalidated;
    # the outcomes/backtest is what validates it). 0.0 ⇒ no assumed edge.
    signal_to_drift_annual_max: float = 0.25
    # hard liquidity / structure gates (mirrors risk_thresholds liquidity/dte)
    max_spread_pct: float = 0.18
    min_open_interest: int = 50
    min_volume: int = 5
    min_option_dollar_volume: float = 25_000.0  # volume × mid × 100 — real daily flow
    min_premium: float = 0.20                   # fill-quality / market-breadth floor
    dte_min: int = 30
    dte_max: int = 200
    require_iv_history: bool = True
    block_earnings_in_window: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "EvGateConfig":
        cfg = cfg or {}
        liq = cfg.get("liquidity", {}) or {}
        dte = cfg.get("dte", {}) or {}
        ev = cfg.get("ev_gate", {}) or {}
        d = cls()
        return cls(
            ev_min=float(ev.get("ev_min", d.ev_min)),
            n_paths=int(ev.get("n_paths", d.n_paths)),
            seed=int(ev.get("seed", d.seed)),
            default_catalyst_drift_annual=float(
                ev.get("default_catalyst_drift_annual",
                       d.default_catalyst_drift_annual)),
            signal_to_drift_annual_max=float(
                ev.get("signal_to_drift_annual_max",
                       d.signal_to_drift_annual_max)),
            max_spread_pct=float(liq.get("max_bid_ask_spread_pct", d.max_spread_pct)),
            min_open_interest=int(liq.get("min_open_interest", d.min_open_interest)),
            min_volume=int(liq.get("min_volume", d.min_volume)),
            min_option_dollar_volume=float(liq.get(
                "min_option_dollar_volume", d.min_option_dollar_volume)),
            min_premium=float(liq.get("min_premium", d.min_premium)),
            dte_min=int(dte.get("min", d.dte_min)),
            dte_max=int(dte.get("max", d.dte_max)),
            require_iv_history=bool(ev.get("require_iv_history", d.require_iv_history)),
            block_earnings_in_window=bool(
                ev.get("block_earnings_in_window", d.block_earnings_in_window)))

    def implied_drift_annual(self, final_trade_score: Optional[float]) -> float:
        """Thesis-implied annual drift PRIOR from the weakest-link trade score.
        A clearly-labelled prior, NOT a fitted parameter — validate via outcomes."""
        if not final_trade_score or final_trade_score <= 0:
            return self.default_catalyst_drift_annual
        return self.signal_to_drift_annual_max * (min(10.0, final_trade_score) / 10.0)


@dataclass
class EvDecision:
    passed: bool
    reasons: list[str]
    hard_fails: list[str]
    ev: Optional[EvResult] = None

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons,
                "hard_fails": self.hard_fails,
                "ev": self.ev.to_dict() if self.ev else None}


class EvGate:
    """Hard EV + data/liquidity/timing gate for a single option snapshot."""

    def __init__(self, cfg: Optional[EvGateConfig] = None,
                 rules: Optional[ExitRules] = None):
        self.cfg = cfg or EvGateConfig()
        self.rules = rules

    def evaluate(self, snapshot: Optional[dict], *,
                 iv_rank: Optional[float] = None,
                 earnings_in_dte: Optional[bool] = None,
                 catalyst_drift_annual: Optional[float] = None,
                 final_trade_score: Optional[float] = None) -> EvDecision:
        c = self.cfg
        if not snapshot:
            return EvDecision(False, ["HARD-GATE: no option snapshot — nothing to size"],
                              ["no_snapshot"], None)
        spot = snapshot.get("spot")
        strike = snapshot.get("strike")
        prem = snapshot.get("entry_premium")
        iv = snapshot.get("iv")
        dte = snapshot.get("dte")
        spread = snapshot.get("spread_pct")
        oi = snapshot.get("open_interest")
        vol = snapshot.get("volume")
        ticker = snapshot.get("ticker") or "UND"

        fails: list[str] = []
        missing = [k for k, v in (("spot", spot), ("strike", strike),
                                  ("entry_premium", prem), ("iv", iv),
                                  ("dte", dte)) if not v]
        if missing:
            fails.append("incomplete snapshot: " + ", ".join(missing))
        if spread is not None and spread > c.max_spread_pct:
            fails.append(f"spread {spread:.0%} > max {c.max_spread_pct:.0%} (illiquid)")
        if oi is not None and oi < c.min_open_interest:
            fails.append(f"open interest {oi} < {c.min_open_interest} (illiquid)")
        if vol is not None and vol < c.min_volume:
            fails.append(f"volume {vol} < {c.min_volume} (illiquid)")
        if c.min_option_dollar_volume > 0 and vol is not None and prem:
            dollar_vol = float(vol) * float(prem) * 100.0
            if dollar_vol < c.min_option_dollar_volume:
                fails.append(f"option $-volume ${dollar_vol:,.0f} < "
                             f"${c.min_option_dollar_volume:,.0f} (thin flow)")
        if c.min_premium > 0 and prem is not None and prem < c.min_premium:
            fails.append(f"premium {prem:.2f} < {c.min_premium:.2f} "
                         f"(penny option — poor fill / no market breadth)")
        if dte is not None and (dte < c.dte_min or dte > c.dte_max):
            fails.append(f"DTE {dte} outside tradeable window "
                         f"[{c.dte_min}, {c.dte_max}]")
        if c.require_iv_history and iv_rank is None:
            fails.append("no IV history — IV-rank unknown, cannot judge richness")
        if c.block_earnings_in_window and earnings_in_dte:
            fails.append("earnings inside holding window — IV-event, not a clean trade")

        if fails:
            return EvDecision(
                False, ["HARD-GATE rejected: " + f for f in fails], fails, None)

        drift = (catalyst_drift_annual if catalyst_drift_annual is not None
                 else c.implied_drift_annual(final_trade_score))
        ev = forward_ev(
            ticker=ticker, spot=float(spot), strike=float(strike), dte=int(dte),
            entry_iv=float(iv), entry_mid=float(prem), drift_annual=drift,
            n_paths=c.n_paths, seed=c.seed, rules=self.rules, spread_pct=spread)
        if ev is None:
            return EvDecision(False, ["HARD-GATE: EV not computable from snapshot"],
                              ["ev_uncomputable"], None)
        if ev.ev_pct <= c.ev_min:
            return EvDecision(
                False,
                [f"EV {ev.ev_pct:+.1%} ≤ min {c.ev_min:+.1%} after fills/theta/exits "
                 f"(win {ev.win_rate:.0%}, drift {drift:+.0%}/yr) — no positive "
                 f"expectancy; do not size"],
                ["negative_ev"], ev)
        return EvDecision(
            True,
            [f"EV {ev.ev_pct:+.1%} > min {c.ev_min:+.1%} (win {ev.win_rate:.0%}, "
             f"p10 {ev.p10:+.0%}/p90 {ev.p90:+.0%}, ~{ev.mean_days:.0f}d hold, "
             f"drift {drift:+.0%}/yr, {ev.n_paths} paths)"],
            [], ev)
