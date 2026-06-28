"""
proxy_map.py
============
Goal F — thesis clusters with an explicit **private → public proxy** mapping and,
crucially, a **falsification condition** for every cluster.

The tracked people mostly act in private markets (Form D placements, venture
stakes, essays). To turn that into a *public* trade idea we must map a cluster
(e.g. "power_grid") to public proxies and grade each proxy by how directly it
benefits:

    1st-order  — the pure-play that lives or dies on the thesis
    2nd-order  — a major beneficiary with other businesses
    3rd-order  — a diversified / indirect beneficiary

Every cluster also declares the observation that would FALSIFY it. If that
observation occurs, the proxy chain is demoted — an alert without a falsification
condition is not allowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .entities import Confidence

logger = logging.getLogger("coi.person.proxy")

# Canonical thesis clusters (Goal F). Stable keys other modules reference.
THESIS_CLUSTERS = (
    "compute", "power_grid", "nuclear", "cooling", "networking",
    "defense_ai", "export_controls", "automation",
)

# Lexicon used to CLASSIFY free text (statements/essays) into clusters. This is a
# transparent keyword model — advisory only, never the final decision.
CLUSTER_LEXICON: dict[str, list[str]] = {
    "compute": ["compute", "gpu", "accelerator", "chip", "semiconductor",
                "foundry", "wafer", "hbm", "training run", "flops"],
    "power_grid": ["power", "grid", "electricity", "megawatt", "gigawatt",
                   "substation", "transmission", "baseload", "energy demand"],
    "nuclear": ["nuclear", "smr", "reactor", "uranium", "enrichment", "fission"],
    "cooling": ["cooling", "liquid cooling", "immersion", "thermal", "heat rejection"],
    "networking": ["networking", "interconnect", "infiniband", "optical",
                   "switch", "nvlink", "ethernet"],
    "defense_ai": ["defense", "national security", "military", "autonomous weapon",
                   "intelligence", "battlefield", "dod"],
    "export_controls": ["export control", "sanction", "tariff", "chokepoint",
                        "entity list", "bis", "smuggling", "diffusion rule"],
    "automation": ["automation", "robotics", "humanoid", "autonomous",
                   "industrial automation", "labor"],
}

# order -> base quality multiplier
_ORDER_QUALITY = {1: 1.0, 2: 0.66, 3: 0.33}

# curated priors -> 0..1. Liquidity refined LIVE when options data is present;
# valuation stays a prior (higher = cheaper = bigger edge for an option buyer).
_LIQUIDITY = {"deep": 1.0, "high": 1.0, "medium": 0.6, "thin": 0.3, "low": 0.3}
_VALUATION = {"cheap": 1.0, "fair": 0.6, "rich": 0.3, "expensive": 0.3}

# edge weights (sum to 1) — link dominates, then liquidity, options, valuation.
_EDGE_W = {"link": 0.40, "liquidity": 0.25, "options": 0.20, "valuation": 0.15}


@dataclass
class Proxy:
    ticker: str
    order: int                  # 1 | 2 | 3
    evidence: Confidence        # how strong the private→public link is
    rationale: str = ""
    liquidity_hint: str = "medium"   # deep | medium | thin (curated prior)
    valuation_hint: str = "fair"     # cheap | fair | rich  (relative prior)

    def quality(self) -> float:
        """0..1 proxy quality = order-directness × evidence strength (the LINK)."""
        base = _ORDER_QUALITY.get(self.order, 0.2)
        return round(base * self.evidence.weight, 4)

    def liquidity(self) -> float:
        return _LIQUIDITY.get(str(self.liquidity_hint).lower(), 0.6)

    def valuation(self) -> float:
        return _VALUATION.get(str(self.valuation_hint).lower(), 0.6)


@dataclass
class RankedProxy:
    """A public proxy scored on four auditable dimensions + a composite edge.

    Answers: for a private theme, which LISTED name is the most liquid,
    undervalued, optionable proxy? `edge_score` (0..10) ranks them.
    """
    ticker: str
    cluster: str
    order: int
    link: float             # 0..1 private→public link quality
    liquidity: float        # 0..1 (live if available, else curated prior)
    options_quality: float  # 0..1 (live options richness/liquidity, else prior)
    valuation: float        # 0..1 (higher = cheaper = more attractive)
    edge_score: float       # 0..10 composite
    rationale: str = ""
    options_live: bool = False
    falsification: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "cluster": self.cluster, "order": self.order,
            "link": round(self.link, 3), "liquidity": round(self.liquidity, 3),
            "options_quality": round(self.options_quality, 3),
            "valuation": round(self.valuation, 3),
            "edge_score": round(self.edge_score, 2),
            "options_live": self.options_live,
            "rationale": self.rationale, "falsification": self.falsification,
        }


@dataclass
class ThesisCluster:
    name: str
    description: str = ""
    private_signal: str = ""    # what private-market action maps here
    falsification: str = ""     # the observation that demotes the chain
    proxies: list[Proxy] = field(default_factory=list)

    def best_proxy_for(self, ticker: str) -> Optional[Proxy]:
        cands = [p for p in self.proxies if p.ticker.upper() == ticker.upper()]
        return max(cands, key=lambda p: p.quality()) if cands else None


@dataclass
class ProxyMap:
    clusters: dict[str, ThesisCluster] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def cluster(self, name: str) -> Optional[ThesisCluster]:
        return self.clusters.get(name)

    def falsification_for(self, name: str) -> Optional[str]:
        c = self.clusters.get(name)
        return c.falsification if c else None

    def clusters_for_ticker(self, ticker: str) -> list[tuple[str, Proxy]]:
        out: list[tuple[str, Proxy]] = []
        for name, c in self.clusters.items():
            p = c.best_proxy_for(ticker)
            if p:
                out.append((name, p))
        return out

    def proxy_quality(self, ticker: str,
                      cluster_names: list[str] | None = None) -> Optional[float]:
        """Best proxy quality (0..1) for a ticker across the given clusters."""
        names = cluster_names or list(self.clusters)
        best: Optional[float] = None
        for name in names:
            c = self.clusters.get(name)
            if not c:
                continue
            p = c.best_proxy_for(ticker)
            if p:
                best = max(best or 0.0, p.quality())
        return best

    # ── ranked proxy map (the private→public edge) ──────────────────────
    def rank_cluster(self, name: str, options_fn=None) -> list[RankedProxy]:
        """Rank a cluster's public proxies by a composite EDGE score.

        edge = w_link·link + w_liq·liquidity + w_opt·options + w_val·valuation
        (each dimension 0..1). ``options_fn(ticker) -> (liquidity_0_1,
        options_quality_0_1)`` supplies LIVE liquidity/options-richness; without
        it, the curated liquidity prior stands in for both. Highest edge first —
        the most liquid, undervalued, optionable proxy for the private theme."""
        c = self.clusters.get(name)
        if not c:
            return []
        out: list[RankedProxy] = []
        for p in c.proxies:
            link = p.quality()
            liq = p.liquidity()
            opt = liq                     # offline fallback: liquidity ⇒ options
            live = False
            if options_fn is not None:
                r = options_fn(p.ticker)
                if r:
                    live_liq, live_opt = r
                    liq = max(0.0, min(1.0, live_liq))
                    opt = max(0.0, min(1.0, live_opt))
                    live = True
            val = p.valuation()
            edge = 10.0 * (_EDGE_W["link"] * link + _EDGE_W["liquidity"] * liq
                           + _EDGE_W["options"] * opt + _EDGE_W["valuation"] * val)
            out.append(RankedProxy(
                ticker=p.ticker, cluster=name, order=p.order, link=link,
                liquidity=liq, options_quality=opt, valuation=val,
                edge_score=round(edge, 2), rationale=p.rationale,
                options_live=live, falsification=c.falsification))
        out.sort(key=lambda r: r.edge_score, reverse=True)
        return out

    def best_proxies(self, name: str, top: int = 3, options_fn=None
                     ) -> list[RankedProxy]:
        return self.rank_cluster(name, options_fn=options_fn)[:top]


def build_proxy_map(cfg: dict | None) -> ProxyMap:
    cfg = cfg or {}
    pm = ProxyMap()
    for name, body in (cfg.get("clusters", {}) or {}).items():
        if not isinstance(body, dict):
            continue
        proxies: list[Proxy] = []
        for p in body.get("proxies", []) or []:
            if not isinstance(p, dict) or not p.get("ticker"):
                continue
            try:
                order = int(p.get("order", 3))
            except (TypeError, ValueError):
                order = 3
            proxies.append(Proxy(
                ticker=str(p["ticker"]).upper(), order=order,
                evidence=Confidence.parse(p.get("evidence", "medium")),
                rationale=p.get("rationale", ""),
                liquidity_hint=str(p.get("liquidity", "medium")),
                valuation_hint=str(p.get("valuation", "fair"))))
        cluster = ThesisCluster(
            name=name, description=body.get("description", ""),
            private_signal=body.get("private_signal", ""),
            falsification=body.get("falsification", ""), proxies=proxies)
        if not cluster.falsification:
            # Hard rule: every cluster MUST carry a falsification condition.
            pm.warnings.append(f"cluster '{name}' has no falsification condition")
        pm.clusters[name] = cluster
    logger.info("Proxy map: %d clusters, %d warnings",
                len(pm.clusters), len(pm.warnings))
    return pm


def load_proxy_map(config_dir: str | Path | None = None) -> ProxyMap:
    from ..config_loader import load_yaml
    cfg = load_yaml("thesis_proxies", Path(config_dir) if config_dir else None)
    return build_proxy_map(cfg)


def classify_clusters(text: str, lexicon: dict[str, list[str]] | None = None
                      ) -> dict[str, float]:
    """ADVISORY keyword classification of free text into thesis clusters.

    Returns {cluster: 0..1 intensity}. This stands in for an LLM extractor; it
    EXTRACTS/CLASSIFIES but never makes the final call (callers must keep
    ``needs_human_review``).
    """
    lex = lexicon or CLUSTER_LEXICON
    if not text:
        return {}
    low = text.lower()
    raw = {c: sum(low.count(p) for p in phrases) for c, phrases in lex.items()}
    mx = max(raw.values()) if raw else 0
    if mx <= 0:
        return {}
    return {c: round((v ** 0.5) / (mx ** 0.5), 4) for c, v in raw.items() if v > 0}
