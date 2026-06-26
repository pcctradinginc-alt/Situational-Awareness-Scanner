"""
runner.py
=========
Offline orchestration that wires the person-intelligence layer end-to-end on the
bundled fixtures, demonstrating the full dataflow:

    Entity graph ─▶ 13F person-signal ─▶ verification ─▶ thesis proxy
                 ─▶ market/options timing (reused engine) ─▶ research vs trade

It reuses the existing :class:`Pipeline` purely to obtain the market and options
sub-scores — it does NOT modify the existing scan. Output is a list of
:class:`PersonScore` with the context that produced them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from ..config_loader import AppConfig
from ..pipeline import Pipeline, DEFAULT_FIXTURES
from .cusip_map import load_mapper
from .entities import EntityGraph, load_graph
from .filings import (
    Direction, FilingType, InstrumentType, assess_verification,
)
from .person_scoring import PersonScore, PersonScorer, PersonSignal
from .proxy_map import ProxyMap, load_proxy_map

logger = logging.getLogger("coi.person.runner")

# Principals whose proximity defines the "person" link strength.
PRINCIPALS = ("thiel", "aschenbrenner")


@dataclass
class PersonIntelRow:
    score: PersonScore
    held_by: list[str]
    dominant_cluster: str
    path_confidence: float


def _network_weight(graph: EntityGraph, manager_name: str) -> float:
    """Best entity-graph path weight from a tracked principal to this manager.

    A holding by a confirmed Thiel/Aschenbrenner vehicle carries more person-
    signal than one by merely adjacent smart money (which still counts, weakly).
    """
    ent = None
    for e in graph.entities.values():
        if e.name.lower() == (manager_name or "").lower():
            ent = e
            break
    if ent is None:
        return 0.3                       # adjacent smart money, not the network
    best = 0.0
    for p in PRINCIPALS:
        if p not in graph.entities:
            continue
        w = graph.path_confidence(p, ent.id)
        if w:
            best = max(best, w)
    # the principal's own vehicle (e.g. thiel_macro) -> high; self -> 1.0
    if ent.id in PRINCIPALS:
        best = 1.0
    return round(best or 0.3, 3)


def run_person_intel(
    config: Optional[AppConfig] = None,
    fixtures_dir: str | Path | None = None,
    today: Optional[date] = None,
    only_tickers: Optional[list[str]] = None,
) -> list[PersonIntelRow]:
    cfg = config or AppConfig()
    fixtures = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
    graph = load_graph(cfg.config_dir)
    pm: ProxyMap = load_proxy_map(cfg.config_dir)
    mapper = load_mapper(cfg.config_dir)
    scorer = PersonScorer()

    pipe = Pipeline(config=cfg, mode="offline", fixtures_dir=fixtures, today=today)
    universe = pipe.universe_builder.build()
    if only_tickers:
        from ..universe import UniverseBuilder
        universe = UniverseBuilder.filter_tickers(universe, only_tickers)
    changes = pipe._build_13f()

    rows: list[PersonIntelRow] = []
    for entry in universe:
        change = changes.get(entry.ticker)
        cluster_pairs = pm.clusters_for_ticker(entry.ticker)
        proxy_q = pm.proxy_quality(entry.ticker)
        # only surface names with an actual person-link (held by a tracked fund)
        # OR a mapped public proxy for the thesis
        if change is None and not cluster_pairs:
            continue

        # market + options timing from the existing engine (reused, not modified)
        snap = pipe.market.get_snapshot(entry.ticker)
        contracts = pipe.options.get_call_contracts(
            entry.ticker, snap.spot, snap.hist_vol_annual)
        market_s, *_ = pipe.engine.score_market(snap)
        opt_s, *_ = pipe.engine.score_options_env(contracts, snap.hist_vol_annual)

        # person signal from the 13F change (confirmation-grade) + path confidence
        path_conf = 0.3
        held_by: list[str] = []
        filing_type = None
        verification = None
        portfolio_pct = None
        if change is not None and change.conviction_score > 0:
            filing_type = FilingType.FORM_13F_HR
            held_by = list(change.managers)
            portfolio_pct = change.portfolio_pct
            path_conf = max((_network_weight(graph, m) for m in change.managers),
                            default=0.3)
            mapres = mapper.map_cusip("", entry.name)   # name-heuristic mapping
            verification, _ = assess_verification(
                InstrumentType.COMMON, Direction.LONG_EXPOSURE,
                mapping_confidence=(0.97 if mapres.mapped_ticker == entry.ticker
                                    else mapres.confidence),
                portfolio_pct=portfolio_pct)

        thesis_clusters = {name: p.quality() for name, p in cluster_pairs}
        dominant = (max(thesis_clusters, key=thesis_clusters.get)
                    if thesis_clusters else "")
        falsification = pm.falsification_for(dominant) or ""

        sig = PersonSignal(
            ticker=entry.ticker, filing_type=filing_type, verification=verification,
            path_confidence=path_conf if filing_type else None,
            portfolio_pct=portfolio_pct, proxy_quality=proxy_q,
            thesis_clusters=thesis_clusters, falsification=falsification,
            market_timing=market_s, options_quality=opt_s,
        )
        rows.append(PersonIntelRow(
            score=scorer.score(sig), held_by=held_by,
            dominant_cluster=dominant, path_confidence=path_conf))

    rows.sort(key=lambda r: r.score.final_research_score, reverse=True)
    return rows
