"""
score_calibration.py
====================
Does a 0–10 score actually predict outcomes? This measures it — per **score
bucket** (high>=7 · mid 5–7 · low<5) and per **horizon** (7/14/30/60/90/180d) —
comparing the **realistic option P&L** (the path sim, NOT a proxy) against the
honest alternatives: the **underlying**, **QQQ**, **SOXX** and **no-trade** (0).

Crucially it asserts nothing it cannot support: every (bucket, horizon) cell stays
``insufficient`` until it has at least ``min_sample`` matured signals. Like the
walk-forward guard, the answer to "is score 7 really better than score 5?" only
appears once enough real outcomes have accumulated — the code is ready before the
data is, and never flatters.

It reuses :func:`options_sim.simulate_option_trade` (conservative fills, BS theta+
vega reprice, daily exit rules) with the exit horizon clamped to each evaluation
horizon, so the option is judged the way it would actually be traded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Optional

from .options_sim import ExitRules, PriceOn, IVOn, simulate_option_trade

HORIZONS = (7, 14, 30, 60, 90, 180)


def score_bucket(s: float) -> str:
    return "high>=7" if s >= 7 else ("mid5-7" if s >= 5 else "low<5")


def _stats(xs: list[float], min_sample: int) -> dict:
    if len(xs) < min_sample:
        return {"n": len(xs), "insufficient": True}
    wins = [x for x in xs if x > 0]
    return {"n": len(xs), "hit_rate": round(len(wins) / len(xs), 3),
            "avg": round(sum(xs) / len(xs), 4),
            "best": round(max(xs), 4), "worst": round(min(xs), 4)}


def calibrate_by_bucket(rows: list[dict], price_on: PriceOn, *,
                        iv_on: Optional[IVOn] = None,
                        base_rules: Optional[ExitRules] = None,
                        horizons: tuple[int, ...] = HORIZONS,
                        min_sample: int = 5) -> dict:
    """Per (score-bucket × horizon): realistic option P&L vs underlying / QQQ /
    SOXX / no-trade, plus a monotonicity read on whether higher buckets do better.

    ``rows`` are recorded outcome rows carrying an option snapshot
    (ticker/strike/entry_premium/iv/dte), ``final_score`` and ``recorded_at``.
    """
    base_rules = base_rules or ExitRules()
    # acc[bucket][h] = {"option": [...], "underlying": [...], "qqq": [...], "soxx": [...]}
    acc: dict[str, dict[int, dict[str, list[float]]]] = {}

    for r in rows:
        if r.get("label") == "rejected":
            continue
        tkr, strike = r.get("ticker"), r.get("strike")
        prem, iv, dte = r.get("entry_premium"), r.get("iv"), r.get("dte")
        score = r.get("final_score")
        if not (tkr and strike and prem and iv and dte) or score is None:
            continue
        try:
            ed = date.fromisoformat(str(r.get("recorded_at"))[:10])
        except (ValueError, TypeError):
            continue
        b = score_bucket(float(score))
        for h in horizons:
            rules_h = replace(base_rules, time_stop_days=int(h))
            sim = simulate_option_trade(
                ticker=tkr, entry_date=ed, strike=float(strike), dte=int(dte),
                entry_iv=float(iv), entry_mid=float(prem), price_on=price_on,
                iv_on=iv_on, rules=rules_h)
            if sim is None:
                continue
            cell = acc.setdefault(b, {}).setdefault(
                h, {"option": [], "underlying": [], "qqq": [], "soxx": []})
            cell["option"].append(sim.option_pnl_pct)
            if sim.stock_return is not None:
                cell["underlying"].append(sim.stock_return)
            if sim.qqq_return is not None:
                cell["qqq"].append(sim.qqq_return)
            if sim.soxx_return is not None:
                cell["soxx"].append(sim.soxx_return)

    out: dict = {"min_sample": min_sample, "horizons": list(horizons), "buckets": {}}
    for b in ("high>=7", "mid5-7", "low<5"):
        if b not in acc:
            continue
        out["buckets"][b] = {}
        for h in horizons:
            cell = acc[b].get(h)
            if not cell:
                continue
            out["buckets"][b][h] = {
                "option": _stats(cell["option"], min_sample),
                "underlying": _stats(cell["underlying"], min_sample),
                "qqq": _stats(cell["qqq"], min_sample),
                "soxx": _stats(cell["soxx"], min_sample),
                "no_trade": {"avg": 0.0},
            }

    out["monotonicity"] = _monotonicity(out["buckets"], horizons, min_sample)
    out["caveat"] = (
        "Realistic option P&L (path sim) vs underlying/QQQ/SOXX/no-trade per "
        "score-bucket × horizon. A cell stays 'insufficient' until n >= min_sample. "
        "No claim that a higher bucket is better until 'monotonicity' says so on a "
        "sufficient sample.")
    return out


def _monotonicity(buckets: dict, horizons, min_sample: int) -> dict:
    """Per horizon: does avg option P&L rise high>=7 ≥ mid5-7 ≥ low<5 on a
    sufficient sample? Else 'insufficient' — never asserted prematurely."""
    order = ("high>=7", "mid5-7", "low<5")
    res: dict[int, str] = {}
    for h in horizons:
        avgs = []
        ok = True
        for b in order:
            cell = buckets.get(b, {}).get(h, {}).get("option", {})
            if cell.get("insufficient") or "avg" not in cell:
                ok = False
                break
            avgs.append(cell["avg"])
        if not ok or len(avgs) < 2:
            res[h] = "insufficient"
        else:
            res[h] = ("monotonic" if all(avgs[i] >= avgs[i + 1]
                                         for i in range(len(avgs) - 1))
                      else "not_monotonic")
    return res
