"""
network.py
==========
A small read-over-the-entity-graph helper that summarises a tracked principal's
**documented network** — the public and private portfolio companies reachable
from them through *fact* edges, plus the recurring thesis themes across that
network.

This is context, not a trade: it answers "where is the Thiel network already
committed, and to which themes?" so a fresh filing can be read against the
backdrop of the network. Private companies (Anduril, SpaceX, …) are surfaced
explicitly as **private** — they seed second-order public proxies via the thesis
clusters but are never themselves a tradeable ticker.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .entities import EntityGraph, EntityType

_TICKER_RE = re.compile(r"Ticker\s+([A-Z]{1,5})")


@dataclass
class NetworkCompany:
    entity_id: str
    name: str
    is_public: bool
    ticker: Optional[str]
    tags: list[str]
    path_weight: float


@dataclass
class NetworkSummary:
    principal: str
    public: list[NetworkCompany] = field(default_factory=list)
    private: list[NetworkCompany] = field(default_factory=list)
    themes: list[tuple[str, int]] = field(default_factory=list)  # (tag, count) desc

    def context_line(self) -> str:
        if not (self.public or self.private):
            return ""
        pub = ", ".join(f"{c.ticker or c.name}" for c in self.public) or "—"
        priv = ", ".join(c.name.split(" (")[0] for c in self.private) or "—"
        themes = ", ".join(f"{t}×{n}" for t, n in self.themes[:4])
        parts = [f"public: {pub}"]
        if self.private:
            parts.append(f"private: {priv}")
        if themes:
            parts.append(f"themes: {themes}")
        return " · ".join(parts)


def fact_path_weight(graph: EntityGraph, source_id: str, target_id: str,
                     max_depth: int = 4) -> float:
    """Best multiplicative confidence path source→target over **fact edges only**.

    Unlike :meth:`EntityGraph.path_confidence`, this ignores hypothesis edges, so
    a principal's *documented* network never leaks in through an unconfirmed
    ideological-alignment link.
    """
    if source_id not in graph.entities or target_id not in graph.entities:
        return 0.0
    best: dict[str, float] = {source_id: 1.0}
    frontier = [(source_id, 1.0, 0)]
    reached = 0.0
    while frontier:
        node, w, depth = frontier.pop()
        if node == target_id:
            reached = max(reached, w)
            continue
        if depth >= max_depth:
            continue
        for r in graph.relations:
            if not r.is_fact:
                continue
            nxt = r.target if r.source == node else (
                r.source if r.target == node else None)
            if nxt is None:
                continue
            nw = w * r.confidence.weight
            if nw > best.get(nxt, 0.0):
                best[nxt] = nw
                frontier.append((nxt, nw, depth + 1))
    return reached


def _is_public(entity) -> Optional[str]:
    """Return the ticker if the entity's notes mark it PUBLIC, else None."""
    notes = entity.notes or ""
    if "PUBLIC" in notes.upper() or _TICKER_RE.search(notes):
        m = _TICKER_RE.search(notes)
        return m.group(1) if m else None
    return None


def network_summary(graph: EntityGraph, principal: str,
                    min_weight: float = 0.2) -> NetworkSummary:
    """Documented portfolio companies reachable from a principal via fact edges."""
    summary = NetworkSummary(principal=principal)
    if principal not in graph.entities:
        return summary
    theme_counter: Counter[str] = Counter()
    for ent in graph.entities.values():
        if ent.type is not EntityType.PORTFOLIO_COMPANY:
            continue
        w = fact_path_weight(graph, principal, ent.id)
        if w < min_weight:
            continue
        ticker = _is_public(ent)
        company = NetworkCompany(
            entity_id=ent.id, name=ent.name, is_public=ticker is not None,
            ticker=ticker, tags=list(ent.tags), path_weight=round(w, 3))
        (summary.public if company.is_public else summary.private).append(company)
        for t in ent.tags:
            theme_counter[t] += 1
    summary.public.sort(key=lambda c: c.path_weight, reverse=True)
    summary.private.sort(key=lambda c: c.path_weight, reverse=True)
    summary.themes = theme_counter.most_common()
    return summary
