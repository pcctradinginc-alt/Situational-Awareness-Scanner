"""
triple_score.py
===============
The **sharpened** decision model. The radar's real question is NOT
"which AI-infra call looks good?" but:

    "Which publicly tradeable stock/option best reflects a NEW, VERIFIABLE
     capital- or conviction-move by Aschenbrenner / Thiel — *before* it is
     broadly priced in?"

To answer that, every signal carries **three orthogonal scores** (0..10), and a
tradeable candidate may only emerge when **all three clear their gate** — a hard
AND, never a sum that lets one strong axis paper over a weak one:

  1. ``person_signal`` — how DIRECTLY does it hang on Leo / Thiel / SA LP /
     Founders Fund? (controlled-path directness × verification × primary source)
  2. ``freshness``     — how EARLY is it relative to the market? (leading filing
     type / fresh statement, decayed by age — a 44-day-old 13F is already priced)
  3. ``tradeability``  — is a handelbarer Call/Spread derivable NOW? (a resolved
     PUBLIC ticker with liquid options + acceptable timing; private / unmapped /
     no-options ⇒ not tradeable)

``final_trade_score = min(三)`` once the gate passes — a chain is only as strong
as its weakest link — else the signal stays research/watchlist. Every axis keeps
its reasoning, and the gate reports exactly which axis failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .filings import SignalRole
from .person_scoring import ScoreComponent, _clamp
from .statements import SourceTier


# ── thresholds (each axis must clear its own bar) ───────────────────────────
@dataclass(frozen=True)
class GateThresholds:
    person_signal: float = 5.0
    freshness: float = 5.0
    tradeability: float = 5.0
    watch: float = 4.0          # below the gate but worth watching

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "GateThresholds":
        g = (cfg or {}).get("person_gates", {}) if cfg else {}
        return cls(
            person_signal=float(g.get("person_signal", 5.0)),
            freshness=float(g.get("freshness", 5.0)),
            tradeability=float(g.get("tradeability", 5.0)),
            watch=float(g.get("watch", 4.0)))


# how early a leading filing type puts you vs the market, and how fast that edge
# decays with age (in days). A Form 4 edge is measured in days; a 13F is already
# ~45 days lagged, so its freshness starts low and decays fast.
_ROLE_LEAD = {SignalRole.EARLY: 9.0, SignalRole.CONFIRMATION: 4.5,
              SignalRole.CONTEXT: 2.0}
_ROLE_HORIZON = {SignalRole.EARLY: 21.0, SignalRole.CONFIRMATION: 30.0,
                 SignalRole.CONTEXT: 14.0}
_TIER_FRESH = {SourceTier.OFFICIAL: 8.0, SourceTier.PRIMARY: 7.0,
               SourceTier.MEDIA: 4.0, SourceTier.REPOST: 2.0}
_STATEMENT_HORIZON = 21.0


@dataclass
class TripleInput:
    """Everything the three axes need, supplied by the caller (monitor)."""
    ticker: Optional[str] = None
    # — person axis —
    path_weight: float = 0.0            # 0..1 directness to a tracked principal
    verified: bool = False              # filing/source verified (not needs_review)
    is_primary_source: bool = False     # EDGAR filing OR official first-party essay
    # — freshness axis —
    role: Optional[SignalRole] = None   # filing role (early/confirmation/context)
    age_days: Optional[int] = None
    source_tier: Optional[SourceTier] = None   # for statements
    # — tradeability axis —
    has_public_ticker: bool = False
    is_private: bool = False
    market_timing: Optional[float] = None      # 0..10 (None = unknown)
    options_quality: Optional[float] = None    # 0..10 (None = unknown liquidity)


@dataclass
class TripleScore:
    ticker: Optional[str]
    person_signal: ScoreComponent
    freshness: ScoreComponent
    tradeability: ScoreComponent
    gate_pass: bool
    failing_axes: list[str]
    final_trade_score: float
    label: str                          # TRADE-CANDIDATE | WATCH | NO-TRADE
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def c(x: ScoreComponent) -> dict:
            return {"value": None if x.value is None else round(x.value, 2),
                    "reason": x.reason}
        return {
            "ticker": self.ticker,
            "person_signal": c(self.person_signal),
            "freshness": c(self.freshness),
            "tradeability": c(self.tradeability),
            "gate_pass": self.gate_pass,
            "failing_axes": self.failing_axes,
            "final_trade_score": round(self.final_trade_score, 2),
            "label": self.label,
            "reasons": self.reasons,
        }


class TripleScorer:
    def __init__(self, thresholds: Optional[GateThresholds] = None):
        self.t = thresholds or GateThresholds()

    # ── axis 1: how directly tied to the tracked people ─────────────────
    def _person_signal(self, x: TripleInput) -> ScoreComponent:
        base = 10.0 * max(0.0, min(1.0, x.path_weight))
        bits = [f"path-directness {x.path_weight:.2f}"]
        score = base
        if not x.verified:
            score *= 0.6
            bits.append("unverified ×0.6")
        else:
            bits.append("verified")
        if x.is_primary_source:
            score = min(10.0, score + 1.0)
            bits.append("primary source +1")
        return ScoreComponent("person_signal", round(_clamp(score), 2),
                              "; ".join(bits),
                              {"path_weight": x.path_weight, "verified": x.verified})

    # ── axis 2: how early relative to the market ────────────────────────
    def _freshness(self, x: TripleInput) -> ScoreComponent:
        if x.role is not None:
            lead = _ROLE_LEAD.get(x.role, 2.0)
            horizon = _ROLE_HORIZON.get(x.role, 21.0)
            label = f"{x.role.value} filing (lead {lead:.0f})"
        elif x.source_tier is not None:
            lead = _TIER_FRESH.get(x.source_tier, 3.0)
            horizon = _STATEMENT_HORIZON
            label = f"{x.source_tier.value} statement"
        else:
            return ScoreComponent("freshness", None, "no timing basis", {})
        age = x.age_days if x.age_days is not None else 0
        recency = max(0.0, 1.0 - (max(0, age) / horizon))
        score = lead * recency
        return ScoreComponent(
            "freshness", round(_clamp(score), 2),
            f"{label}, age {age}d → recency ×{recency:.2f}",
            {"age_days": age, "horizon": horizon})

    # ── axis 3: is a tradeable instrument derivable NOW ─────────────────
    def _tradeability(self, x: TripleInput) -> ScoreComponent:
        if x.is_private:
            return ScoreComponent("tradeability", 0.0,
                                  "private / no public ticker — not tradeable", {})
        if not x.has_public_ticker or not x.ticker:
            return ScoreComponent("tradeability", 0.0,
                                  "no resolved public ticker — not tradeable yet", {})
        # unknown timing/liquidity is NOT good — treat as a conservative 4.0
        mt = x.market_timing if x.market_timing is not None else 4.0
        oq = x.options_quality if x.options_quality is not None else 4.0
        score = 0.5 * mt + 0.5 * oq
        bits = [f"market-timing {mt:.1f}", f"options {oq:.1f}"]
        if x.options_quality is None:
            score = min(score, 4.0)          # cannot confirm a liquid call
            bits.append("options liquidity UNVERIFIED → capped 4.0")
        return ScoreComponent("tradeability", round(_clamp(score), 2),
                              "; ".join(bits),
                              {"market_timing": mt, "options_quality": oq})

    # ── combine with a HARD AND-gate ────────────────────────────────────
    def score(self, x: TripleInput) -> TripleScore:
        ps = self._person_signal(x)
        fr = self._freshness(x)
        tr = self._tradeability(x)

        def v(c: ScoreComponent) -> float:
            return c.value if c.value is not None else 0.0

        failing: list[str] = []
        if v(ps) < self.t.person_signal:
            failing.append("person_signal")
        if v(fr) < self.t.freshness:
            failing.append("freshness")
        if v(tr) < self.t.tradeability:
            failing.append("tradeability")

        gate_pass = not failing
        weakest = min(v(ps), v(fr), v(tr))
        final = round(weakest, 2) if gate_pass else 0.0

        reasons: list[str] = []
        if gate_pass:
            reasons.append(
                f"ALL THREE axes clear the gate (person {v(ps):.1f}, "
                f"fresh {v(fr):.1f}, trade {v(tr):.1f}) → weakest-link score "
                f"{final:.1f}")
            label = "TRADE-CANDIDATE"
        else:
            reasons.append("gate FAILED — not a trade: weak " + ", ".join(failing))
            label = ("WATCH" if weakest >= self.t.watch and "tradeability"
                     not in failing else "NO-TRADE")
        for c in (ps, fr, tr):
            if c.reason:
                reasons.append(f"{c.name}: {c.reason}")

        return TripleScore(
            ticker=x.ticker, person_signal=ps, freshness=fr, tradeability=tr,
            gate_pass=gate_pass, failing_axes=failing, final_trade_score=final,
            label=label, reasons=reasons)
