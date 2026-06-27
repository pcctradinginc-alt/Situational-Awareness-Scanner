"""
candidates.py
=============
Turns eligible tickers into ranked, explainable CALL-option candidates.

Hard filters (reject with a reason): DTE outside window, missing essential data,
open interest / volume below floors, bid-ask spread too wide. Survivors get a
PER-CONTRACT options score (liquidity + IV richness + expiry suitability + strike
/delta attractiveness) recombined with the ticker's thesis/13F/market/catalyst
scores, so each contract is judged on its own merits, not just its underlying.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import (
    OptionCandidate, OptionContract, RejectedCandidate, TickerEvaluation,
    UniverseEntry,
)
from .scoring import (
    SignalEngine, _atm_iv, _clamp, _richness_str, iv_richness_assessment,
)

logger = logging.getLogger("coi.candidates")


class CandidateGenerator:
    def __init__(self, engine: SignalEngine, risk_cfg: dict):
        self.engine = engine
        self.risk = risk_cfg or {}
        self.dte = self.risk.get("dte", {})
        self.liq = self.risk.get("liquidity", {})
        self.zones = self.risk.get("strike_zones", {})
        self.penalty_cfg = engine.penalty_cfg

    def generate(
        self, ev: TickerEvaluation, entry: UniverseEntry,
        contracts: list[OptionContract], hist_vol: Optional[float],
        earnings_in_days: Optional[int] = None, iv_rank: Optional[float] = None,
    ) -> tuple[list[OptionCandidate], list[RejectedCandidate]]:
        candidates: list[OptionCandidate] = []
        rejected: list[RejectedCandidate] = []

        # Hard data-quality floor: if we DO have contracts but the underlying's
        # signal is assembled from too few real components, reject rather than
        # surface a low-evidence "opportunity".
        min_dq = self.risk.get("data_quality", {}).get("min_acceptable", 0.30)
        if contracts and ev.breakdown.confidence < min_dq:
            return [], [RejectedCandidate(
                ev.ticker, "insufficient_data_quality",
                f"data completeness {ev.breakdown.confidence:.2f} < {min_dq}")]

        dte_min = self.dte.get("min", 30)
        dte_max = self.dte.get("max", 200)
        min_oi = self.liq.get("min_open_interest", 50)
        min_vol = self.liq.get("min_volume", 5)
        max_spread = self.liq.get("max_bid_ask_spread_pct", 0.18)

        flag_window = self.risk.get("earnings", {}).get("flag_window_days", 10)
        is_event = (earnings_in_days is not None and 0 <= earnings_in_days <= flag_window)

        atm_iv = _atm_iv(contracts)

        for c in contracts:
            # ── hard filters ────────────────────────────────────────────
            if c.dte < dte_min:
                rejected.append(RejectedCandidate(
                    c.ticker, "near_expiry_lottery",
                    f"DTE {c.dte} < min {dte_min} @ strike {c.strike}"))
                continue
            if c.dte > dte_max:
                rejected.append(RejectedCandidate(
                    c.ticker, "dte_above_max", f"DTE {c.dte} > max {dte_max}"))
                continue
            if c.mid is None:
                rejected.append(RejectedCandidate(
                    c.ticker, "missing_price", f"no usable bid/ask/last @ {c.strike}"))
                continue
            if c.open_interest is None:
                rejected.append(RejectedCandidate(
                    c.ticker, "missing_open_interest", f"strike {c.strike}"))
                continue
            if c.open_interest < min_oi:
                rejected.append(RejectedCandidate(
                    c.ticker, "low_open_interest",
                    f"OI {c.open_interest} < {min_oi} @ {c.strike}"))
                continue
            if c.volume is not None and c.volume < min_vol:
                rejected.append(RejectedCandidate(
                    c.ticker, "low_volume", f"vol {c.volume} < {min_vol} @ {c.strike}"))
                continue
            spread = c.spread_pct
            if spread is None:
                rejected.append(RejectedCandidate(
                    c.ticker, "missing_quote", f"no two-sided quote @ {c.strike}"))
                continue
            if spread > max_spread:
                rejected.append(RejectedCandidate(
                    c.ticker, "wide_spread",
                    f"spread {spread:.1%} > {max_spread:.0%} @ {c.strike}"))
                continue

            zone = self._strike_zone(c.moneyness)
            if zone is None:
                rejected.append(RejectedCandidate(
                    c.ticker, "strike_out_of_zone",
                    f"moneyness {c.moneyness} outside configured zones"))
                continue

            cand = self._build_candidate(
                ev, entry, c, zone, hist_vol, atm_iv, is_event, earnings_in_days,
                iv_rank)
            candidates.append(cand)

        candidates.sort(key=lambda x: x.final_score, reverse=True)
        return candidates, rejected

    # ── per-contract scoring ───────────────────────────────────────────
    def _build_candidate(self, ev, entry, c, zone, hist_vol, atm_iv,
                         is_event, earnings_in_days, iv_rank=None) -> OptionCandidate:
        opt_score, opt_for, opt_against, opt_pen = self._contract_options_score(
            c, zone, hist_vol, atm_iv, iv_rank)

        bd_components = {
            "thesis": ev.breakdown.thesis,
            "thirteen_f": ev.breakdown.thirteen_f or None,
            "market": ev.breakdown.market or None,
            "options": opt_score,
            "catalyst": ev.breakdown.catalyst or None,
        }
        # preserve "absent" semantics: components scored 0 because data missing
        # were excluded from ev.breakdown.components_present
        present = set(ev.breakdown.components_present)
        for k in ("thirteen_f", "market", "catalyst"):
            if k not in present:
                bd_components[k] = None

        # merge ticker-level (market/13F/catalyst) penalties with this contract's
        # options penalties so e.g. a contradictory 13F or missing catalyst still
        # weighs on every contract of the underlying.
        penalties = dict(ev.penalties)
        penalties.update(opt_pen)
        if is_event:
            # Holding a call INTO earnings is a real IV-crush risk, not just a
            # label — apply a genuine, configurable penalty (its own key).
            penalties["event_iv_risk"] = self.penalty_cfg.get("event_iv_risk", 0.7)

        bd = self.engine.combine(bd_components, penalties)

        # ── reasons against (ALWAYS non-empty, incl. event risk) ──────────
        reasons_against = list(ev.reasons_against) + list(opt_against)
        if is_event:
            reasons_against.append(
                f"earnings in ~{earnings_in_days}d → IV-crush risk into the event")
        if not reasons_against:
            reasons_against.append(
                "no automatic red flags detected — but a long call still loses "
                "100% if the move is late or smaller than implied; verify live "
                "IV/spread/catalyst before sizing")

        risk_notes = list(opt_against)
        if c.delta_estimated:
            risk_notes.append("delta is ESTIMATED (Black-Scholes), not exchange-provided")
        if is_event:
            risk_notes.append(
                f"EARNINGS in ~{earnings_in_days}d → IV-driven EVENT trade, not pure trend")
        if bd.confidence_label == "low":
            risk_notes.append("LOW data confidence — treat score with caution")

        thesis_expl = self._thesis_explanation(entry, ev)
        paper = self._paper_recommendation(bd.final_score, ev.label, is_event,
                                            bd.confidence_label)

        return OptionCandidate(
            ticker=c.ticker, name=entry.name, category=entry.category, contract=c,
            final_score=bd.final_score, breakdown=bd, strike_zone=zone,
            thesis_explanation=thesis_expl, catalysts=ev.catalysts,
            reasons_for=ev.reasons_for + opt_for,
            reasons_against=reasons_against,
            risk_notes=risk_notes, data_flags=ev.data_flags,
            is_event_trade=is_event,
            suggested_expiry_window=self._expiry_window(),
            confidence_label=bd.confidence_label, paper_recommendation=paper,
        )

    def _contract_options_score(self, c, zone, hist_vol, atm_iv, iv_rank=None):
        reasons_for: list[str] = []
        reasons_against: list[str] = []
        penalties: dict[str, float] = {}
        s = 5.0
        min_oi = self.liq.get("min_open_interest", 50)

        # liquidity
        if c.open_interest >= min_oi * 10:
            s += 2.0; reasons_for.append(f"OI {c.open_interest} (deep)")
        elif c.open_interest >= min_oi * 2:
            s += 1.0
        if c.spread_pct is not None and c.spread_pct <= self.liq.get(
                "max_bid_ask_spread_pct", 0.18) / 2:
            s += 1.0; reasons_for.append(f"tight spread {c.spread_pct:.1%}")

        # expiry suitability
        pmin = self.dte.get("preferred_min", 60)
        pmax = self.dte.get("preferred_max", 180)
        if pmin <= c.dte <= pmax:
            s += 1.5; reasons_for.append(f"{c.dte} DTE in preferred window")
        else:
            reasons_against.append(f"{c.dte} DTE outside preferred {pmin}-{pmax}")

        # delta / leverage sweet spot
        if c.delta is not None:
            if 0.30 <= c.delta <= 0.55:
                s += 1.5; reasons_for.append(f"delta {c.delta:.2f} (balanced leverage)")
            elif c.delta < 0.15:
                s -= 1.0
                penalties["near_expiry_lottery"] = self.penalty_cfg.get(
                    "near_expiry_lottery", 0.8)
                reasons_against.append(f"delta {c.delta:.2f} (low-prob lottery)")
            elif c.delta > 0.80:
                s -= 0.5; reasons_against.append(f"delta {c.delta:.2f} (deep ITM, less leverage)")

        # strike zone preference
        if zone == "near_the_money":
            s += 0.5
        elif zone == "longer_dated_otm" and c.dte < self.dte.get("preferred_min", 60):
            reasons_against.append("aggressive OTM strike on short tenor")

        # IV richness: real IV percentile once warmed up, else realised-vol proxy.
        richness, iv_pct, band = iv_richness_assessment(
            c.iv, hist_vol, self.risk, iv_rank)
        rich = _richness_str(richness, iv_pct)
        ivs = f"{c.iv:.0%} " if c.iv else ""
        if band == "extreme":
            s -= 2.0
            penalties["extreme_iv"] = self.penalty_cfg.get("extreme_iv", 0.8)
            reasons_against.append(f"IV {ivs}very rich ({rich})")
        elif band == "elevated":
            s -= 1.0
            reasons_against.append(f"IV {ivs}elevated ({rich})")
        elif band == "mild":
            reasons_against.append(f"IV {ivs}somewhat rich ({rich})")
        elif band == "fair":
            reasons_for.append(f"IV {ivs}fair ({rich})")
        return round(_clamp(s), 2), reasons_for, reasons_against, penalties

    # ── helpers ─────────────────────────────────────────────────────────
    def _strike_zone(self, moneyness: Optional[float]) -> Optional[str]:
        if moneyness is None:
            return None
        for zone in ("near_the_money", "slightly_otm", "longer_dated_otm"):
            lo, hi = self.zones.get(zone, [None, None])
            if lo is not None and hi is not None and lo <= moneyness <= hi:
                return zone
        return None

    def _expiry_window(self) -> str:
        return f"{self.dte.get('preferred_min', 60)}-{self.dte.get('preferred_max', 180)} DTE"

    def _thesis_explanation(self, entry: UniverseEntry, ev: TickerEvaluation) -> str:
        tags = ", ".join(entry.thesis_tags[:4]) or "AI-infrastructure"
        return (f"{entry.name} ({entry.category}) maps to AI-infra thesis via "
                f"{tags}. Relevance prior {entry.relevance:.2f}; "
                f"thesis sub-score {ev.breakdown.thesis:.1f}/10.")

    def _paper_recommendation(self, score, label, is_event, conf_label) -> str:
        base = "PAPER ONLY — never auto-traded. "
        if conf_label == "low":
            return base + "Insufficient data confidence; track on paper, do not size."
        if label == "top" and not is_event:
            return base + "Candidate for a small paper position; verify live quotes & spread first."
        if is_event:
            return base + "Event/IV trade — paper-test around earnings; expect IV crush risk."
        return base + "Watch only; wait for better entry / catalyst / IV."
