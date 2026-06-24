"""
scoring.py
==========
The signal engine. Combines five POSITIVE component scores (thesis, 13F, market,
options-environment, catalyst) into a weighted score, then subtracts an explicit
RISK PENALTY. Weights are configurable. Missing components are dropped and the
remaining weights re-normalised, so partial data degrades gracefully instead of
silently scoring 0.

Key separation of concerns (intentional):
  * THESIS score answers "is this a good company for the thesis?"
  * the score as a whole answers "is THIS a reasonable CALL setup *right now*?"
A strong thesis with a terrible options/vol setup must NOT rank highly.
"""

from __future__ import annotations

import logging
import statistics
from typing import Optional

from .models import (
    Holding13FChange, MarketSnapshot, OptionContract, SignalBreakdown,
    ThesisVector, TickerEvaluation, UniverseEntry,
)

logger = logging.getLogger("coi.scoring")


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def iv_richness_assessment(iv, hist_vol, risk_cfg):
    """Assess how rich a call's IV is vs realised vol.

    Returns (richness, iv_pct, band) where band ∈ {fair, mild, elevated,
    extreme} or (None, None, None) when inputs are missing. iv_pct maps richness
    via a configurable slope: iv_pct = 50 + slope*(richness-1). With slope=100 a
    call priced 30% over realised vol (richness 1.30) reaches the "expensive"
    band — far tighter than the previous 1.6x trigger.
    """
    if not iv or not hist_vol or hist_vol <= 0:
        return None, None, None
    richness = iv / hist_vol
    iv_cfg = (risk_cfg or {}).get("iv", {})
    slope = iv_cfg.get("richness_to_percentile_slope", 100)
    iv_pct = max(0.0, min(100.0, 50.0 + slope * (richness - 1.0)))
    exp_p = iv_cfg.get("expensive_percentile", 80)
    ext_p = iv_cfg.get("extreme_percentile", 92)
    fair_max = iv_cfg.get("fair_richness_max", 1.15)
    if iv_pct >= ext_p:
        band = "extreme"
    elif iv_pct >= exp_p:
        band = "elevated"
    elif richness <= fair_max:
        band = "fair"
    else:
        band = "mild"
    return richness, iv_pct, band


class SignalEngine:
    def __init__(self, scoring_cfg: dict, risk_cfg: dict):
        self.scoring_cfg = scoring_cfg or {}
        self.risk_cfg = risk_cfg or {}
        self.weights = self.scoring_cfg.get("weights", {})
        self.penalty_cfg = self.scoring_cfg.get("risk_penalty", {})
        self.gates = self.scoring_cfg.get("gates", {})
        self.conf_labels = self.scoring_cfg.get("confidence", {}).get(
            "labels", {"high": 0.8, "medium": 0.55, "low": 0.0})

    # ── component sub-scores ───────────────────────────────────────────
    def score_thesis(self, entry: UniverseEntry, vector: ThesisVector) -> float:
        prior = entry.relevance  # 0..1
        tag_intensity = vector.score_for_tags(entry.thesis_tags) if vector else 0.0
        if vector and vector.confidence > 0.05:
            blended = 0.6 * prior + 0.4 * tag_intensity
        else:
            blended = prior  # no usable thesis corpus -> rely on prior
        return round(_clamp(blended * 10.0), 2)

    def score_market(self, snap: Optional[MarketSnapshot]):
        reasons_for: list[str] = []
        reasons_against: list[str] = []
        penalties: dict[str, float] = {}
        if snap is None or not snap.complete:
            return None, reasons_for, reasons_against, penalties

        s = 5.0
        if snap.momentum_3m is not None:
            if snap.momentum_3m > 0.10:
                s += 2.0; reasons_for.append(f"3m momentum +{snap.momentum_3m:.0%}")
            elif snap.momentum_3m > 0:
                s += 1.0
            elif snap.momentum_3m < -0.05:
                s -= 1.0; reasons_against.append(f"3m momentum {snap.momentum_3m:.0%}")
        if snap.momentum_6m is not None and snap.momentum_6m > 0.15:
            s += 1.0; reasons_for.append(f"6m momentum +{snap.momentum_6m:.0%}")
        if snap.above_200dma is True:
            s += 1.5; reasons_for.append("above 200d MA (uptrend)")
        elif snap.above_200dma is False:
            s -= 1.0; reasons_against.append("below 200d MA (downtrend)")
        if snap.rsi_14 is not None:
            if 45 <= snap.rsi_14 <= 65:
                s += 0.5
            elif snap.rsi_14 > 75:
                s -= 0.5; reasons_against.append(f"RSI {snap.rsi_14:.0f} overbought")

        dd_limit = self.risk_cfg.get("market", {}).get(
            "max_drawdown_without_stabilization", 0.35)
        if snap.drawdown_from_high is not None and snap.drawdown_from_high <= -dd_limit:
            if snap.stabilizing:
                reasons_for.append("deep drawdown but stabilizing")
            else:
                penalties["excessive_drawdown"] = self.penalty_cfg.get(
                    "excessive_drawdown", 0.6)
                reasons_against.append(
                    f"drawdown {snap.drawdown_from_high:.0%} without stabilization")
        return round(_clamp(s), 2), reasons_for, reasons_against, penalties

    def score_13f(self, change: Optional[Holding13FChange]):
        reasons_for: list[str] = []
        reasons_against: list[str] = []
        penalties: dict[str, float] = {}
        if change is None:
            return None, reasons_for, reasons_against, penalties
        conv = change.conviction_score
        if conv < 0:
            penalties["contradictory_13f"] = self.penalty_cfg.get(
                "contradictory_13f", 0.7)
            reasons_against.append(
                f"13F: smart money {change.action} (score {conv:+.1f})")
            return 0.0, reasons_for, reasons_against, penalties
        if change.action in ("new", "add") and conv > 0:
            reasons_for.append(
                f"13F: {change.action} by {', '.join(change.managers) or 'tracked funds'}")
        return round(_clamp(conv), 2), reasons_for, reasons_against, penalties

    def score_catalyst(self, catalysts: list[str]):
        # No catalyst data => the component is ABSENT (None), not a weak positive.
        # This keeps confidence honest (a name with no catalyst is less complete)
        # and still applies the no_catalyst penalty. Returning a fake 2.0 would
        # inflate data completeness and neuter the data-quality floor.
        reasons_for: list[str] = []
        penalties: dict[str, float] = {}
        if not catalysts:
            penalties["no_catalyst"] = self.penalty_cfg.get("no_catalyst", 0.5)
            return None, reasons_for, penalties
        s = 2.0 + 2.0 * len(catalysts)
        reasons_for.extend(catalysts)
        return round(_clamp(s), 2), reasons_for, penalties

    def score_options_env(self, contracts: list[OptionContract],
                          hist_vol: Optional[float]):
        """Aggregate options-environment quality (liquidity + IV richness)."""
        reasons_for: list[str] = []
        reasons_against: list[str] = []
        penalties: dict[str, float] = {}
        liq = self.risk_cfg.get("liquidity", {})
        min_oi = liq.get("min_open_interest", 50)
        max_spread = liq.get("max_bid_ask_spread_pct", 0.18)

        usable = [c for c in contracts if c.open_interest is not None]
        if not usable:
            return None, reasons_for, reasons_against, penalties

        ois = [c.open_interest for c in usable]
        spreads = [c.spread_pct for c in usable if c.spread_pct is not None]
        median_oi = statistics.median(ois) if ois else 0
        median_spread = statistics.median(spreads) if spreads else None

        s = 5.0
        if median_oi >= min_oi * 10:
            s += 2.5; reasons_for.append(f"deep options liquidity (median OI {median_oi:.0f})")
        elif median_oi >= min_oi:
            s += 1.0
        else:
            s -= 2.0
            penalties["illiquid_options"] = self.penalty_cfg.get("illiquid_options", 1.0)
            reasons_against.append(f"thin options OI (median {median_oi:.0f})")

        if median_spread is not None:
            if median_spread <= max_spread / 2:
                s += 1.5; reasons_for.append(f"tight spreads (median {median_spread:.1%})")
            elif median_spread > max_spread:
                s -= 1.5
                penalties["wide_spread"] = self.penalty_cfg.get("wide_spread", 1.0)
                reasons_against.append(f"wide spreads (median {median_spread:.1%})")

        # IV richness proxy: ATM IV vs realised vol (no IV history needed).
        atm = _atm_iv(contracts)
        richness, iv_pct, band = iv_richness_assessment(atm, hist_vol, self.risk_cfg)
        if band == "extreme":
            s -= 2.0
            penalties["extreme_iv"] = self.penalty_cfg.get("extreme_iv", 0.8)
            reasons_against.append(f"IV very rich vs realised ({richness:.2f}x)")
        elif band == "elevated":
            s -= 1.0
            reasons_against.append(f"IV elevated vs realised ({richness:.2f}x)")
        elif band == "mild":
            reasons_against.append(f"IV somewhat rich vs realised ({richness:.2f}x)")
        elif band == "fair":
            reasons_for.append(f"IV fair vs realised ({richness:.2f}x)")
        return round(_clamp(s), 2), reasons_for, reasons_against, penalties

    # ── combine ─────────────────────────────────────────────────────────
    def combine(self, components: dict[str, Optional[float]],
                penalties: dict[str, float]) -> SignalBreakdown:
        present = {k: v for k, v in components.items() if v is not None}
        applied_w = {k: self.weights.get(k, 0.0) for k in present}
        wsum = sum(applied_w.values())
        if wsum <= 0:
            weighted = 0.0
        else:
            weighted = sum(present[k] * applied_w[k] for k in present) / wsum

        max_pen = self.penalty_cfg.get("max_total_penalty", 4.0)
        # data-quality penalty if too many components missing
        n_total = len(components)
        n_present = len(present)
        data_quality = n_present / n_total if n_total else 0.0
        if data_quality < self.risk_cfg.get("data_quality", {}).get("low_confidence_below", 0.55):
            penalties = dict(penalties)
            penalties.setdefault("weak_data_quality",
                                 self.penalty_cfg.get("weak_data_quality", 1.0))
        penalty_total = min(max_pen, sum(penalties.values()))

        final = round(_clamp(weighted - penalty_total), 2)
        confidence = round(data_quality, 2)
        label = self._confidence_label(confidence)
        bd = SignalBreakdown(
            thesis=components.get("thesis") or 0.0,
            thirteen_f=components.get("thirteen_f") or 0.0,
            market=components.get("market") or 0.0,
            options=components.get("options") or 0.0,
            catalyst=components.get("catalyst") or 0.0,
            risk_penalty=round(penalty_total, 2),
            weighted_positive=round(weighted, 2),
            final_score=final,
            confidence=confidence,
            confidence_label=label,
            components_present=list(present.keys()),
        )
        return bd

    def _confidence_label(self, conf: float) -> str:
        if conf >= self.conf_labels.get("high", 0.8):
            return "high"
        if conf >= self.conf_labels.get("medium", 0.55):
            return "medium"
        return "low"

    # ── top-level per-ticker evaluation ────────────────────────────────
    def evaluate_ticker(
        self, entry: UniverseEntry, vector: ThesisVector,
        snap: Optional[MarketSnapshot], change: Optional[Holding13FChange],
        contracts: list[OptionContract], catalysts: list[str],
    ) -> TickerEvaluation:
        thesis_s = self.score_thesis(entry, vector)
        market_s, mfor, magainst, mpen = self.score_market(snap)
        f13_s, ffor, fagainst, fpen = self.score_13f(change)
        cat_s, cfor, cpen = self.score_catalyst(catalysts)
        hist_vol = snap.hist_vol_annual if snap else None
        opt_s, ofor, oagainst, open_ = self.score_options_env(contracts, hist_vol)

        components = {
            "thesis": thesis_s, "thirteen_f": f13_s, "market": market_s,
            "options": opt_s, "catalyst": cat_s,
        }
        # non-options penalties propagate to per-contract scoring; options-env
        # penalties (open_) are aggregate and replaced per-contract downstream.
        nonopt_penalties: dict[str, float] = {}
        for p in (mpen, fpen, cpen):
            nonopt_penalties.update(p)
        penalties = dict(nonopt_penalties)
        penalties.update(open_)

        bd = self.combine(components, penalties)

        ev = TickerEvaluation(
            ticker=entry.ticker, name=entry.name, category=entry.category,
            relevance=entry.relevance, breakdown=bd, market=snap,
            thirteen_f=change, catalysts=catalysts, penalties=nonopt_penalties,
        )
        ev.reasons_for = [f"Thesis fit {thesis_s:.1f}/10 ({entry.category})"] + \
            mfor + ffor + cfor + ofor
        ev.reasons_against = magainst + fagainst + oagainst
        if "no_catalyst" in penalties:
            ev.reasons_against.append("no identified near-term catalyst")
        if snap:
            ev.data_flags.extend(snap.data_flags)
        if not contracts:
            ev.data_flags.append("no_options_data")

        # label by gates
        top_min = self.gates.get("top_candidate_min_score", 6.5)
        watch_min = self.gates.get("watchlist_min_score", 4.5)
        if bd.final_score >= top_min:
            ev.label = "top"
        elif bd.final_score >= watch_min:
            ev.label = "watchlist"
        else:
            ev.label = "reject"
        return ev


def _atm_iv(contracts: list[OptionContract]) -> Optional[float]:
    """Median IV of contracts closest to the money (moneyness 0.95..1.05)."""
    near = [c.iv for c in contracts
            if c.iv and c.moneyness and 0.95 <= c.moneyness <= 1.05]
    if near:
        return statistics.median(near)
    any_iv = [c.iv for c in contracts if c.iv]
    return statistics.median(any_iv) if any_iv else None
