"""
person_scoring.py
=================
Goal G — split the score so **thesis ≠ trade** is structural, not a slogan.

Two separate outputs:

  final_research_score
      "A tracked person/entity actually DID or SAID X (verified), and X maps to
       public proxy Y." Built from: person_signal, source_reliability,
       verification, thesis_alignment, proxy_quality — minus research penalties.

  final_trade_candidate_score
      The research signal GATED by market timing and options quality, and CAPPED
      when verification is weak. It can never exceed the research score, and a
      great thesis with no timing / illiquid options / failed falsification stays
      a non-trade.

Every component carries its reasoning and the raw inputs that produced it, so any
number can be audited. Every result carries a falsification condition and a
non-empty list of counter-arguments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .entities import Confidence
from .filings import FilingType, SignalRole, VerificationStatus, filing_meta
from .statements import StatementRef, SourceTier

logger = logging.getLogger("coi.person.scoring")


def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class ScoreComponent:
    name: str
    value: Optional[float]             # 0..10, or None if not assessable
    reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PersonSignal:
    """What a tracked person/entity did or said about a ticker."""
    ticker: str
    filing_type: Optional[FilingType] = None
    verification: Optional[VerificationStatus] = None
    path_confidence: Optional[float] = None     # entity-graph path weight 0..1
    portfolio_pct: Optional[float] = None
    statements: list[StatementRef] = field(default_factory=list)
    proxy_quality: Optional[float] = None        # 0..1
    thesis_clusters: dict[str, float] = field(default_factory=dict)
    falsification: str = ""
    falsification_triggered: bool = False
    # externally supplied market/options sub-scores (0..10) from the existing
    # engine — kept optional so this module needs no live data to be testable.
    market_timing: Optional[float] = None
    options_quality: Optional[float] = None


@dataclass
class PersonScore:
    ticker: str
    components: dict[str, ScoreComponent]
    weighted_research_positive: float
    research_penalty: float
    final_research_score: float
    final_trade_candidate_score: float
    research_label: str
    trade_label: str
    gate_factor: float
    verification_cap: float
    reasons_for: list[str]
    reasons_against: list[str]
    falsification: str
    needs_human_review: bool

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "final_research_score": round(self.final_research_score, 2),
            "final_trade_candidate_score": round(self.final_trade_candidate_score, 2),
            "research_label": self.research_label,
            "trade_label": self.trade_label,
            "gate_factor": round(self.gate_factor, 3),
            "verification_cap": self.verification_cap,
            "needs_human_review": self.needs_human_review,
            "components": {k: {"value": (None if c.value is None else round(c.value, 2)),
                               "reason": c.reason, "raw": c.raw}
                           for k, c in self.components.items()},
            "reasons_for": self.reasons_for,
            "reasons_against": self.reasons_against,
            "falsification": self.falsification,
        }


# verification status -> (research sub-score, hard trade cap)
_VERIF_SCORE: dict[VerificationStatus, float] = {
    VerificationStatus.VERIFIED: 10.0,
    VerificationStatus.PARTIALLY_VERIFIED: 6.0,
    VerificationStatus.NOT_VERIFIABLE_VIA_13F: 3.0,
    VerificationStatus.NEEDS_HUMAN_REVIEW: 2.0,
    VerificationStatus.REJECTED_NOISE: 0.0,
}
_VERIF_TRADE_CAP: dict[VerificationStatus, float] = {
    VerificationStatus.VERIFIED: 10.0,
    VerificationStatus.PARTIALLY_VERIFIED: 7.0,
    VerificationStatus.NOT_VERIFIABLE_VIA_13F: 4.0,
    VerificationStatus.NEEDS_HUMAN_REVIEW: 3.0,
    VerificationStatus.REJECTED_NOISE: 0.0,
}

DEFAULT_RESEARCH_WEIGHTS = {
    "person_signal": 0.30, "verification": 0.25, "source_reliability": 0.15,
    "thesis_alignment": 0.15, "proxy_quality": 0.15,
}


class PersonScorer:
    def __init__(self, research_weights: dict | None = None,
                 research_gates: dict | None = None):
        self.weights = research_weights or DEFAULT_RESEARCH_WEIGHTS
        g = research_gates or {}
        self.research_strong = g.get("research_strong", 6.5)
        self.research_monitor = g.get("research_monitor", 4.0)
        self.trade_candidate = g.get("trade_candidate", 6.0)
        self.trade_watch = g.get("trade_watch", 4.0)

    # ── components ──────────────────────────────────────────────────────
    def _person_signal(self, s: PersonSignal) -> ScoreComponent:
        if s.filing_type is None and not s.statements:
            return ScoreComponent("person_signal", None,
                                  "no filing or statement evidence", {})
        score = 0.0
        bits: list[str] = []
        raw: dict = {}
        if s.filing_type is not None:
            meta = filing_meta(s.filing_type)
            # EARLY filings (Form 4 / 13D / Form D) are worth more than lagged 13F
            role_bonus = {SignalRole.EARLY: 6.0, SignalRole.CONFIRMATION: 3.5,
                          SignalRole.CONTEXT: 1.5}[meta.role]
            score += role_bonus
            bits.append(f"{s.filing_type.value} ({meta.role.value})")
            raw["filing_role"] = meta.role.value
            raw["filing_lag_days"] = meta.typical_lag_days
        if s.statements:
            score += min(2.0, 0.7 * len(s.statements))
            bits.append(f"{len(s.statements)} statement ref(s)")
            raw["n_statements"] = len(s.statements)
        # discount by entity-graph path confidence (weak/hypothetical link = less)
        if s.path_confidence is not None:
            score *= (0.4 + 0.6 * s.path_confidence)
            bits.append(f"path-confidence ×{0.4 + 0.6 * s.path_confidence:.2f}")
            raw["path_confidence"] = s.path_confidence
        if s.portfolio_pct:
            score += min(2.0, 8.0 * s.portfolio_pct)
            raw["portfolio_pct"] = s.portfolio_pct
        return ScoreComponent("person_signal", round(_clamp(score), 2),
                              "; ".join(bits), raw)

    def _source_reliability(self, s: PersonSignal) -> ScoreComponent:
        if s.filing_type is None and not s.statements:
            return ScoreComponent("source_reliability", None, "no source", {})
        score = 3.0
        bits: list[str] = []
        raw: dict = {}
        if s.filing_type is not None and filing_meta(s.filing_type).is_edgar:
            score += 4.0
            bits.append("EDGAR primary filing")
            raw["edgar"] = True
        if s.statements:
            best = min(s.statements, key=lambda r: r.tier.rank)
            tier_bonus = {SourceTier.OFFICIAL: 3.0, SourceTier.PRIMARY: 2.0,
                          SourceTier.MEDIA: 0.5, SourceTier.REPOST: -0.5}[best.tier]
            score += tier_bonus
            bits.append(f"best source tier: {best.tier.value}")
            raw["best_tier"] = best.tier.value
        return ScoreComponent("source_reliability", round(_clamp(score), 2),
                              "; ".join(bits), raw)

    def _verification(self, s: PersonSignal) -> ScoreComponent:
        if s.verification is None:
            return ScoreComponent("verification", None, "not assessed", {})
        v = _VERIF_SCORE[s.verification]
        return ScoreComponent("verification", v, s.verification.value,
                              {"status": s.verification.value})

    def _thesis_alignment(self, s: PersonSignal) -> ScoreComponent:
        if not s.thesis_clusters:
            return ScoreComponent("thesis_alignment", None, "no cluster mapping", {})
        top = max(s.thesis_clusters.values())
        mean = sum(s.thesis_clusters.values()) / len(s.thesis_clusters)
        val = round(_clamp(10.0 * (0.7 * top + 0.3 * mean)), 2)
        dom = max(s.thesis_clusters, key=s.thesis_clusters.get)
        return ScoreComponent("thesis_alignment", val,
                              f"dominant cluster '{dom}' ({top:.2f})",
                              {"clusters": s.thesis_clusters})

    def _proxy_quality(self, s: PersonSignal) -> ScoreComponent:
        if s.proxy_quality is None:
            return ScoreComponent("proxy_quality", None, "no proxy mapping", {})
        return ScoreComponent("proxy_quality", round(_clamp(10.0 * s.proxy_quality), 2),
                              f"best proxy quality {s.proxy_quality:.2f}",
                              {"proxy_quality": s.proxy_quality})

    # ── combine ─────────────────────────────────────────────────────────
    def score(self, s: PersonSignal) -> PersonScore:
        comps = {
            "person_signal": self._person_signal(s),
            "verification": self._verification(s),
            "source_reliability": self._source_reliability(s),
            "thesis_alignment": self._thesis_alignment(s),
            "proxy_quality": self._proxy_quality(s),
        }
        # market/options are TRADE inputs, recorded as components but NOT part of
        # the research positive.
        comps["market_timing"] = ScoreComponent(
            "market_timing", s.market_timing,
            "external market sub-score" if s.market_timing is not None else "absent",
            {})
        comps["options_quality"] = ScoreComponent(
            "options_quality", s.options_quality,
            "external options sub-score" if s.options_quality is not None else "absent",
            {})

        # weighted research positive over PRESENT components (renormalised)
        present = {k: comps[k].value for k in self.weights if comps[k].value is not None}
        wsum = sum(self.weights[k] for k in present)
        weighted = (sum(present[k] * self.weights[k] for k in present) / wsum
                    if wsum > 0 else 0.0)

        # research penalties
        penalty = 0.0
        reasons_against: list[str] = []
        if s.falsification_triggered:
            penalty += 3.0
            reasons_against.append("FALSIFICATION TRIGGERED — thesis condition broke")
        if s.verification in (VerificationStatus.NEEDS_HUMAN_REVIEW,
                              VerificationStatus.REJECTED_NOISE):
            penalty += 1.0
            reasons_against.append(f"verification weak ({s.verification.value})")
        if s.path_confidence is not None and s.path_confidence < 0.4:
            penalty += 1.0
            reasons_against.append(
                f"weak entity-graph path confidence ({s.path_confidence:.2f})")
        penalty = min(5.0, penalty)

        research = round(_clamp(weighted - penalty), 2)

        # ── trade gating (thesis ≠ trade) ───────────────────────────────
        # missing timing/options info is penalised toward 0.4 (unknown ≠ good).
        timing_q = (s.market_timing / 10.0) if s.market_timing is not None else 0.4
        options_q = (s.options_quality / 10.0) if s.options_quality is not None else 0.4
        gate = round(0.5 * timing_q + 0.5 * options_q, 3)

        verif = s.verification or VerificationStatus.NEEDS_HUMAN_REVIEW
        cap = _VERIF_TRADE_CAP[verif]

        trade_base = research * gate                       # always ≤ research
        trade = round(_clamp(min(trade_base, cap)), 2)

        reasons_for: list[str] = []
        for k in ("person_signal", "verification", "source_reliability",
                  "thesis_alignment", "proxy_quality"):
            c = comps[k]
            if c.value is not None and c.value >= 5.0 and c.reason:
                reasons_for.append(f"{k}: {c.reason}")
        if s.market_timing is None:
            reasons_against.append("no market-timing sub-score — trade gated down")
        if s.options_quality is None:
            reasons_against.append("no options-quality sub-score — trade gated down")
        if trade < research:
            reasons_against.append(
                f"thesis≠trade: research {research:.1f} gated to trade {trade:.1f} "
                f"(gate ×{gate:.2f}, verification cap {cap:.0f})")
        if not reasons_against:
            reasons_against.append(
                "no automatic red flag — but verify live timing/IV/spread before sizing")

        return PersonScore(
            ticker=s.ticker, components=comps,
            weighted_research_positive=round(weighted, 2),
            research_penalty=round(penalty, 2),
            final_research_score=research,
            final_trade_candidate_score=trade,
            research_label=self._research_label(research),
            trade_label=self._trade_label(trade),
            gate_factor=gate, verification_cap=cap,
            reasons_for=reasons_for, reasons_against=reasons_against,
            falsification=s.falsification or "(no falsification condition supplied)",
            needs_human_review=(verif in (VerificationStatus.NEEDS_HUMAN_REVIEW,
                                          VerificationStatus.NOT_VERIFIABLE_VIA_13F)
                                or s.falsification_triggered),
        )

    def _research_label(self, x: float) -> str:
        if x >= self.research_strong:
            return "strong"
        if x >= self.research_monitor:
            return "monitor"
        return "weak"

    def _trade_label(self, x: float) -> str:
        if x >= self.trade_candidate:
            return "candidate"
        if x >= self.trade_watch:
            return "watch"
        return "reject"
