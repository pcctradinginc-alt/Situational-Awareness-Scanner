"""
portfolio_risk.py
=================
Aggregate guardrails over the **sizeable** (EV-cleared) CALL candidates. A single
positive-EV trade is not a robust book: stacked correlated AI-beta, a lopsided net
delta or a vega-heavy basket can blow up together. This caps, across all proposed
positions at once:

  * **count** — max concurrent positions, and max per thesis-cluster;
  * **concentration** — max single-name and max per-cluster share of premium notional;
  * **net delta** — directional exposure (Σ delta × 100 × contracts);
  * **aggregate vega** — total long-volatility exposure (Σ vega × contracts).

It NEVER sizes or trades — it flags breaches so a human does not unknowingly stack
correlated risk. Thesis cluster is used as a correlation proxy (same cluster ⇒
correlated). Research/paper only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PortfolioLimits:
    max_positions: int = 5
    max_per_cluster: int = 2
    max_single_name_weight: float = 0.40   # share of total premium notional
    max_cluster_weight: float = 0.60
    max_net_delta: float = 300.0           # Σ delta × 100 × contracts (share-equiv)
    max_aggregate_vega: float = 500.0      # Σ vega × contracts ($ per vol point)

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "PortfolioLimits":
        p = (cfg or {}).get("portfolio", {}) if cfg else {}
        d = cls()
        return cls(
            max_positions=int(p.get("max_positions", d.max_positions)),
            max_per_cluster=int(p.get("max_per_cluster", d.max_per_cluster)),
            max_single_name_weight=float(
                p.get("max_single_name_weight", d.max_single_name_weight)),
            max_cluster_weight=float(p.get("max_cluster_weight", d.max_cluster_weight)),
            max_net_delta=float(p.get("max_net_delta", d.max_net_delta)),
            max_aggregate_vega=float(p.get("max_aggregate_vega", d.max_aggregate_vega)))


@dataclass
class Position:
    ticker: str
    cluster: str = ""
    delta: float = 0.0          # option delta per share (0..1 for a call)
    vega: float = 0.0           # $ per vol point, per contract
    premium: float = 0.0        # mid premium per share
    contracts: int = 1

    def notional(self) -> float:
        return float(self.premium) * 100.0 * max(1, int(self.contracts))


def assess_portfolio(positions: list[Position],
                     limits: Optional[PortfolioLimits] = None) -> dict:
    """Return {ok, n, breaches[], aggregates{}} for a basket of proposed positions."""
    limits = limits or PortfolioLimits()
    if not positions:
        return {"ok": True, "n": 0, "breaches": [], "aggregates": {}}

    notional = {id(p): p.notional() for p in positions}
    total = sum(notional.values()) or 1.0

    cl_count: dict[str, int] = defaultdict(int)
    cl_notional: dict[str, float] = defaultdict(float)
    name_notional: dict[str, float] = defaultdict(float)
    net_delta = 0.0
    agg_vega = 0.0
    for p in positions:
        n = notional[id(p)]
        if p.cluster:
            cl_count[p.cluster] += 1
            cl_notional[p.cluster] += n
        name_notional[p.ticker] += n
        net_delta += float(p.delta) * 100.0 * max(1, int(p.contracts))
        agg_vega += float(p.vega) * max(1, int(p.contracts))

    breaches: list[str] = []
    if len(positions) > limits.max_positions:
        breaches.append(
            f"{len(positions)} positions > max {limits.max_positions}")
    for cl, c in sorted(cl_count.items()):
        if c > limits.max_per_cluster:
            breaches.append(
                f"cluster «{cl}»: {c} positions > max {limits.max_per_cluster}")
    for cl, nl in sorted(cl_notional.items()):
        w = nl / total
        if w > limits.max_cluster_weight:
            breaches.append(
                f"cluster «{cl}» weight {w:.0%} > max {limits.max_cluster_weight:.0%}")
    for tk, nl in sorted(name_notional.items()):
        w = nl / total
        if w > limits.max_single_name_weight:
            breaches.append(
                f"{tk} weight {w:.0%} > max single-name {limits.max_single_name_weight:.0%}")
    if abs(net_delta) > limits.max_net_delta:
        breaches.append(
            f"net delta {net_delta:.0f} > max {limits.max_net_delta:.0f}")
    if agg_vega > limits.max_aggregate_vega:
        breaches.append(
            f"aggregate vega {agg_vega:.0f} > max {limits.max_aggregate_vega:.0f}")

    return {
        "ok": not breaches,
        "n": len(positions),
        "breaches": breaches,
        "aggregates": {
            "net_delta": round(net_delta, 1),
            "aggregate_vega": round(agg_vega, 1),
            "total_notional": round(total, 1),
            "by_cluster": {k: round(v, 1) for k, v in sorted(cl_notional.items())},
        },
    }
