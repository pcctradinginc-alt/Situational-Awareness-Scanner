"""
entities.py
===========
Goal A — an **entity graph** for the tracked people and their vehicles, with
explicit confidence levels and a hard separation of FACTS from HYPOTHESES.

Why a graph and not a flat list: the *signal* we care about ("Thiel is
accumulating X") flows through controlled entities (Thiel → Thiel Macro / Mithril
/ Founders Fund → public position). The edge between two entities is itself a
claim that can be a sourced fact or a hypothesis, and it carries its own
confidence. Downstream scoring multiplies a signal by the confidence of the path
it travelled, so a position held by a *hypothesised* affiliate counts for less
than one held by a *confirmed* controlled vehicle.

No business logic beyond trivial lookups — this stays a transparent data layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("coi.person.entities")


class EntityType(str, Enum):
    PERSON = "person"
    FUND = "fund"                       # files 13F-HR (public equities)
    MANAGEMENT_CO = "management_company"
    GROWTH_FUND = "growth_fund"         # 13D/G + Form 4 only, no 13F
    PORTFOLIO_COMPANY = "portfolio_company"
    INDEX_PROXY = "index_proxy"         # e.g. QQQ / SOXX used as a benchmark


class Confidence(str, Enum):
    """Ordered confidence. ``CONFIRMED`` is reserved for primary-source facts."""
    CONFIRMED = "confirmed"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SPECULATIVE = "speculative"

    @property
    def weight(self) -> float:
        return _CONF_WEIGHT[self]

    @classmethod
    def parse(cls, value: object) -> "Confidence":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            logger.warning("Unknown confidence %r -> SPECULATIVE", value)
            return cls.SPECULATIVE


_CONF_WEIGHT: dict[Confidence, float] = {
    Confidence.CONFIRMED: 1.0,
    Confidence.HIGH: 0.8,
    Confidence.MEDIUM: 0.55,
    Confidence.LOW: 0.3,
    Confidence.SPECULATIVE: 0.1,
}


@dataclass
class Entity:
    id: str
    name: str
    type: EntityType
    cik: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)        # thesis-cluster tags
    files: list[str] = field(default_factory=list)        # form types it submits
    notes: str = ""


@dataclass
class Relation:
    """A directed claim ``source --kind--> target``.

    ``is_fact`` is the hard fact/hypothesis switch; ``confidence`` grades how
    strongly we believe it. A hypothesis can still be HIGH confidence (a
    well-reasoned but unconfirmed link); a fact should usually be CONFIRMED/HIGH.
    """
    source: str
    target: str
    kind: str
    confidence: Confidence = Confidence.MEDIUM
    is_fact: bool = False
    evidence: str = ""                  # short source pointer, NEVER full text


@dataclass
class EntityGraph:
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ── lookups ────────────────────────────────────────────────────────
    def get(self, entity_id: str) -> Optional[Entity]:
        return self.entities.get(entity_id)

    def by_cik(self, cik: str) -> Optional[Entity]:
        cik = _norm_cik(cik)
        for e in self.entities.values():
            if e.cik and _norm_cik(e.cik) == cik:
                return e
        return None

    def facts(self) -> list[Relation]:
        return [r for r in self.relations if r.is_fact]

    def hypotheses(self) -> list[Relation]:
        return [r for r in self.relations if not r.is_fact]

    def relations_from(self, entity_id: str) -> list[Relation]:
        return [r for r in self.relations if r.source == entity_id]

    def relations_to(self, entity_id: str) -> list[Relation]:
        return [r for r in self.relations if r.target == entity_id]

    def neighbors(self, entity_id: str) -> list[str]:
        out: list[str] = []
        for r in self.relations:
            if r.source == entity_id:
                out.append(r.target)
            elif r.target == entity_id:
                out.append(r.source)
        return list(dict.fromkeys(out))

    def path_confidence(self, source_id: str, target_id: str,
                        max_depth: int = 4) -> Optional[float]:
        """Best (highest) product-of-edge-confidence path source→target.

        Returns a 0..1 weight or None if unreachable. Used by person-scoring to
        discount a signal that only reaches a tracked person through a weak or
        hypothesised chain of control.
        """
        if source_id not in self.entities or target_id not in self.entities:
            return None
        best: dict[str, float] = {source_id: 1.0}
        # simple relaxation (Dijkstra-style on multiplicative weights)
        frontier = [(source_id, 1.0, 0)]
        reached: Optional[float] = None
        while frontier:
            node, w, depth = frontier.pop()
            if node == target_id:
                reached = max(reached or 0.0, w)
                continue
            if depth >= max_depth:
                continue
            for r in self.relations:
                nxt = None
                if r.source == node:
                    nxt = r.target
                elif r.target == node:
                    nxt = r.source
                if nxt is None:
                    continue
                nw = w * r.confidence.weight
                if nw > best.get(nxt, 0.0):
                    best[nxt] = nw
                    frontier.append((nxt, nw, depth + 1))
        return reached


def _norm_cik(cik: str) -> str:
    return str(cik).strip().lstrip("0") or "0"


def build_graph(cfg: dict | None) -> EntityGraph:
    """Build an :class:`EntityGraph` from an ``entity_graph.yml`` mapping.

    Never raises: malformed rows are skipped with a warning so a typo in config
    cannot crash a scan.
    """
    cfg = cfg or {}
    graph = EntityGraph()

    for row in cfg.get("entities", []) or []:
        if not isinstance(row, dict) or not row.get("id"):
            graph.warnings.append(f"skipped entity without id: {row!r}")
            continue
        try:
            etype = EntityType(str(row.get("type", "fund")).strip().lower())
        except ValueError:
            etype = EntityType.FUND
            graph.warnings.append(f"{row['id']}: unknown type -> fund")
        graph.entities[row["id"]] = Entity(
            id=row["id"], name=row.get("name", row["id"]), type=etype,
            cik=row.get("cik"), aliases=list(row.get("aliases", []) or []),
            tags=list(row.get("tags", []) or []),
            files=list(row.get("files", []) or []), notes=row.get("notes", ""),
        )

    for row in cfg.get("relations", []) or []:
        if not isinstance(row, dict):
            continue
        src, tgt = row.get("source"), row.get("target")
        if src not in graph.entities or tgt not in graph.entities:
            graph.warnings.append(
                f"relation references unknown entity: {src!r}->{tgt!r}")
            continue
        graph.relations.append(Relation(
            source=src, target=tgt, kind=row.get("kind", "related"),
            confidence=Confidence.parse(row.get("confidence", "medium")),
            is_fact=bool(row.get("is_fact", False)),
            evidence=row.get("evidence", ""),
        ))

    logger.info("Entity graph: %d entities, %d relations (%d facts, %d hypotheses)",
                len(graph.entities), len(graph.relations),
                len(graph.facts()), len(graph.hypotheses()))
    return graph


def load_graph(config_dir: str | Path | None = None) -> EntityGraph:
    """Load the bundled/edited ``config/entity_graph.yml`` (defaults-safe)."""
    from ..config_loader import load_yaml
    cfg = load_yaml("entity_graph", Path(config_dir) if config_dir else None)
    return build_graph(cfg)
