"""
universe.py
===========
Builds the AI-infrastructure equity universe from config. Pure, deterministic,
no network. Invalid rows are skipped with a warning rather than crashing.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .models import UniverseEntry

logger = logging.getLogger("coi.universe")


class UniverseBuilder:
    def __init__(self, universe_cfg: dict):
        self.cfg = universe_cfg or {}
        self.categories: list[str] = list(self.cfg.get("categories", []))

    def build(self) -> list[UniverseEntry]:
        entries: list[UniverseEntry] = []
        seen: set[str] = set()
        for row in self.cfg.get("tickers", []) or []:
            if not isinstance(row, dict):
                logger.warning("Skipping non-mapping universe row: %r", row)
                continue
            ticker = str(row.get("ticker", "")).strip().upper()
            if not ticker:
                logger.warning("Skipping universe row without ticker: %r", row)
                continue
            if ticker in seen:
                logger.warning("Duplicate ticker %s — keeping first", ticker)
                continue
            seen.add(ticker)
            try:
                relevance = float(row.get("relevance", 0.5))
            except (TypeError, ValueError):
                relevance = 0.5
            relevance = max(0.0, min(1.0, relevance))
            entries.append(UniverseEntry(
                ticker=ticker,
                name=str(row.get("name", ticker)),
                category=str(row.get("category", "uncategorized")),
                relevance=relevance,
                thesis_tags=[str(t) for t in (row.get("thesis_tags") or [])],
                data_availability=str(row.get("data_availability", "unknown")),
            ))
        logger.info("Universe built: %d tickers across %d categories",
                    len(entries), len(self.categories))
        return entries

    @staticmethod
    def filter_tickers(entries: Iterable[UniverseEntry],
                       tickers: Iterable[str]) -> list[UniverseEntry]:
        wanted = {t.strip().upper() for t in tickers}
        return [e for e in entries if e.ticker in wanted]
