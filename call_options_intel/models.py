"""
models.py
=========
Plain dataclasses shared across the pipeline. No business logic here beyond
trivial derived properties, so the data contracts stay obvious and testable.

Design rule: every numeric field that can be unknown is Optional and defaults to
None. Downstream code must treat None as "missing" (degrade gracefully), never
crash. `data_flags` carries human-readable warnings about what was missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Universe ──────────────────────────────────────────────────────────────
@dataclass
class UniverseEntry:
    ticker: str
    name: str
    category: str
    relevance: float = 0.5          # 0..1 prior of AI-infra benefit
    thesis_tags: list[str] = field(default_factory=list)
    data_availability: str = "unknown"


# ── Thesis ────────────────────────────────────────────────────────────────
@dataclass
class ThesisVector:
    """Structured, uncertainty-aware representation of thesis themes.

    weights maps a canonical theme -> 0..1 intensity extracted from source
    text. `confidence` reflects how much source material backed the vector.
    """
    weights: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)

    def score_for_tags(self, tags: list[str]) -> float:
        """Average intensity over a company's thesis tags (0..1)."""
        if not tags or not self.weights:
            return 0.0
        vals = [self.weights.get(t, 0.0) for t in tags]
        return sum(vals) / len(vals)


# ── 13F ───────────────────────────────────────────────────────────────────
@dataclass
class Holding13F:
    ticker: str
    manager: str
    shares: Optional[float] = None
    value_usd: Optional[float] = None
    portfolio_pct: Optional[float] = None     # 0..1 of manager's 13F AUM


@dataclass
class Holding13FChange:
    """Quarter-over-quarter change for one ticker aggregated across managers."""
    ticker: str
    action: str = "hold"            # new | add | reduce | exit | hold
    pct_change: Optional[float] = None         # change in shares (e.g. 0.25 = +25%)
    portfolio_pct: Optional[float] = None
    quarters_held: int = 0
    managers: list[str] = field(default_factory=list)
    conviction_score: float = 0.0   # 0..10 after scoring


# ── Market ────────────────────────────────────────────────────────────────
@dataclass
class MarketSnapshot:
    ticker: str
    spot: Optional[float] = None
    momentum_3m: Optional[float] = None        # pct return ~63d
    momentum_6m: Optional[float] = None
    above_200dma: Optional[bool] = None
    rsi_14: Optional[float] = None
    drawdown_from_high: Optional[float] = None  # negative number, e.g. -0.18
    avg_dollar_volume: Optional[float] = None
    hist_vol_annual: Optional[float] = None     # realised vol (annualised)
    stabilizing: Optional[bool] = None          # price > short MA after drawdown
    data_flags: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.spot is not None and self.momentum_3m is not None


# ── Options ───────────────────────────────────────────────────────────────
@dataclass
class OptionContract:
    ticker: str
    expiry: str                      # ISO date
    strike: float
    dte: int
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    iv: Optional[float] = None       # implied volatility (decimal, e.g. 0.45)
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    delta: Optional[float] = None
    delta_estimated: bool = False
    spot: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return round((self.bid + self.ask) / 2.0, 4)
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if self.bid is not None and self.ask is not None and m and m > 0:
            return round((self.ask - self.bid) / m, 4)
        return None

    @property
    def moneyness(self) -> Optional[float]:
        if self.spot and self.spot > 0:
            return round(self.strike / self.spot, 4)
        return None


# ── Scoring ───────────────────────────────────────────────────────────────
@dataclass
class SignalBreakdown:
    thesis: float = 0.0
    thirteen_f: float = 0.0
    market: float = 0.0
    options: float = 0.0
    catalyst: float = 0.0
    risk_penalty: float = 0.0
    weighted_positive: float = 0.0
    final_score: float = 0.0
    confidence: float = 0.0          # 0..1 data completeness
    confidence_label: str = "low"
    components_present: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "thesis": round(self.thesis, 2),
            "thirteen_f": round(self.thirteen_f, 2),
            "market": round(self.market, 2),
            "options": round(self.options, 2),
            "catalyst": round(self.catalyst, 2),
            "risk_penalty": round(self.risk_penalty, 2),
            "weighted_positive": round(self.weighted_positive, 2),
            "final_score": round(self.final_score, 2),
            "confidence": round(self.confidence, 2),
            "confidence_label": self.confidence_label,
        }


@dataclass
class TickerEvaluation:
    """Per-ticker result before/independent of specific option selection."""
    ticker: str
    name: str = ""
    category: str = ""
    relevance: float = 0.0
    breakdown: SignalBreakdown = field(default_factory=SignalBreakdown)
    market: Optional[MarketSnapshot] = None
    thirteen_f: Optional[Holding13FChange] = None
    catalysts: list[str] = field(default_factory=list)
    reasons_for: list[str] = field(default_factory=list)
    reasons_against: list[str] = field(default_factory=list)
    data_flags: list[str] = field(default_factory=list)
    # non-options risk penalties (market/13F/catalyst) carried to per-contract
    # scoring so a great thesis with a contradictory 13F still gets penalised.
    penalties: dict[str, float] = field(default_factory=dict)
    label: str = "reject"            # top | watchlist | reject


@dataclass
class OptionCandidate:
    ticker: str
    name: str
    category: str
    contract: OptionContract
    final_score: float
    breakdown: SignalBreakdown
    strike_zone: str = ""
    thesis_explanation: str = ""
    catalysts: list[str] = field(default_factory=list)
    reasons_for: list[str] = field(default_factory=list)
    reasons_against: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    data_flags: list[str] = field(default_factory=list)
    is_event_trade: bool = False
    suggested_expiry_window: str = ""
    confidence_label: str = "low"
    paper_recommendation: str = ""


# ── Rejected contracts (for transparency) ─────────────────────────────────
@dataclass
class RejectedCandidate:
    ticker: str
    reason: str
    detail: str = ""


# ── Whole-scan result ──────────────────────────────────────────────────────
@dataclass
class ScanResult:
    generated_at: str = ""
    mode: str = "offline"
    universe_size: int = 0
    regime: dict = field(default_factory=dict)
    thesis_vector: Optional[ThesisVector] = None
    candidates: list[OptionCandidate] = field(default_factory=list)
    top: list[OptionCandidate] = field(default_factory=list)
    watchlist: list[TickerEvaluation] = field(default_factory=list)
    evaluations: list[TickerEvaluation] = field(default_factory=list)
    rejected_contracts: list[RejectedCandidate] = field(default_factory=list)
    thirteen_f_changes: dict[str, Holding13FChange] = field(default_factory=dict)
    data_quality_warnings: list[str] = field(default_factory=list)
    config_warnings: list[str] = field(default_factory=list)
