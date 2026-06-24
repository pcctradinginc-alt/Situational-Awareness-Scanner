"""
thesis.py
=========
Strategic-thesis ingestion. Reads local markdown/text (and, in live mode,
explicitly-allowed public URLs) and extracts a STRUCTURED, uncertainty-aware
thesis vector over canonical AI-infrastructure themes.

This is deliberately a transparent keyword/lexicon model, NOT an LLM. It does
not claim philosophical certainty: it counts theme evidence and normalises it
into 0..1 intensities with an attached confidence based on corpus size.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import ThesisVector

logger = logging.getLogger("coi.thesis")

# Canonical themes -> lexicon of indicative phrases (lowercased substrings).
THEME_LEXICON: dict[str, list[str]] = {
    "compute_scarcity": [
        "compute scarcity", "gpu shortage", "compute constrained", "scarce compute",
        "compute bottleneck", "gpu supply", "accelerator shortage", "compute demand",
    ],
    "semiconductor_bottlenecks": [
        "semiconductor", "chip shortage", "foundry", "fab capacity", "wafer",
        "advanced node", "lithography", "chip bottleneck",
    ],
    "ai_data_centers": [
        "data center", "datacenter", "hyperscale", "ai cluster", "server build",
        "rack", "colocation",
    ],
    "cloud_capex": [
        "capex", "capital expenditure", "cloud spend", "cloud investment",
        "hyperscaler spend", "infrastructure spend",
    ],
    "power_grid_constraints": [
        "power", "grid", "electricity", "megawatt", "gigawatt", "energy demand",
        "power constraint", "electrification", "baseload", "nuclear",
    ],
    "memory_bandwidth": [
        "memory bandwidth", "hbm", "high bandwidth memory", "dram", "memory wall",
    ],
    "networking": [
        "networking", "interconnect", "infiniband", "ethernet", "optical",
        "switch", "nvlink",
    ],
    "cooling": [
        "cooling", "liquid cooling", "thermal", "immersion cooling", "heat",
    ],
    "frontier_model_race": [
        "frontier model", "agi", "superintelligence", "scaling laws", "training run",
        "frontier lab", "model race", "gpt", "next generation model",
    ],
    "national_security_ai_race": [
        "national security", "ai race", "geopolitic", "export control",
        "arms race", "great power", "ccp", "china",
    ],
    "automation_robotics": [
        "robotics", "automation", "humanoid", "autonomous", "industrial automation",
    ],
    "ai_infrastructure_suppliers": [
        "supplier", "supply chain", "bottleneck", "picks and shovels",
        "infrastructure provider", "enabler",
    ],
    "geopolitical_chokepoints": [
        "chokepoint", "taiwan", "strait", "export ban", "sanction", "tariff",
    ],
    "monetization_lag": [
        "monetization", "monetisation", "revenue lag", "underpriced",
        "spend ahead of revenue", "roi", "payback",
    ],
}


class ThesisAnalyzer:
    def __init__(self, lexicon: dict[str, list[str]] | None = None):
        self.lexicon = lexicon or THEME_LEXICON

    # ── extraction ────────────────────────────────────────────────────
    def analyze_text(self, text: str, source: str = "inline") -> ThesisVector:
        return self.analyze_corpus([(source, text)])

    def analyze_corpus(self, docs: list[tuple[str, str]]) -> ThesisVector:
        """docs = list of (source_name, text). Returns a normalised vector."""
        raw_counts: dict[str, int] = {t: 0 for t in self.lexicon}
        total_tokens = 0
        sources: list[str] = []
        for source, text in docs:
            if not text:
                continue
            sources.append(source)
            low = text.lower()
            total_tokens += len(re.findall(r"\w+", low))
            for theme, phrases in self.lexicon.items():
                for p in phrases:
                    # count occurrences of each phrase
                    raw_counts[theme] += low.count(p)

        max_count = max(raw_counts.values()) if raw_counts else 0
        weights: dict[str, float] = {}
        if max_count > 0:
            for theme, c in raw_counts.items():
                if c > 0:
                    # sqrt compression so a single dominant theme doesn't zero out
                    weights[theme] = round((c ** 0.5) / (max_count ** 0.5), 4)

        # Confidence grows with corpus size and theme coverage, capped at 1.
        coverage = (len(weights) / len(self.lexicon)) if self.lexicon else 0.0
        size_factor = min(1.0, total_tokens / 1500.0)
        confidence = round(min(1.0, 0.5 * coverage + 0.5 * size_factor), 3)

        logger.info("Thesis vector: %d themes from %d docs (%d tokens), conf=%.2f",
                    len(weights), len(sources), total_tokens, confidence)
        return ThesisVector(weights=weights, confidence=confidence, sources=sources)

    # ── file ingestion ────────────────────────────────────────────────
    def analyze_paths(self, paths: list[str | Path]) -> ThesisVector:
        docs: list[tuple[str, str]] = []
        for p in paths:
            path = Path(p)
            if path.is_dir():
                for f in sorted(path.glob("**/*")):
                    if f.suffix.lower() in (".md", ".txt", ".markdown"):
                        docs.append((f.name, _safe_read(f)))
            elif path.exists():
                docs.append((path.name, _safe_read(path)))
            else:
                logger.warning("Thesis source not found: %s", path)
        if not docs:
            logger.warning("No thesis documents found — empty vector")
        return self.analyze_corpus(docs)


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read %s: %s", path, exc)
        return ""
