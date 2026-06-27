"""
llm_classify.py
===============
Phase 3 — an OPTIONAL LLM-assisted classifier for statement excerpts.

Design constraints, by the goal:
  * the LLM **extracts/classifies only** — it never makes the final decision, so
    every classified statement keeps ``needs_human_review``;
  * it is **off by default** and never required — a deterministic keyword model
    (the same transparent lexicon used elsewhere) is the always-available
    fallback, used automatically if no LLM is wired or the call fails;
  * the API key is read **only** from the ``ANTHROPIC_API_KEY`` environment
    variable, never hard-coded, never logged, never returned;
  * the LLM client is **injectable**, so this module is fully testable offline
    with a fake — no network in tests.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .proxy_map import THESIS_CLUSTERS, classify_clusters

logger = logging.getLogger("coi.person.llm")


class LLMClient(Protocol):
    """Anything that maps text -> {cluster: 0..1}. Injected for testability."""
    def classify(self, text: str, clusters: list[str]) -> dict[str, float]: ...


@dataclass
class ClassificationResult:
    clusters: dict[str, float]
    method: str                         # llm | keyword_fallback
    advisory: bool = True               # NEVER the final word
    needs_human_review: bool = True
    raw: dict = field(default_factory=dict)


@dataclass
class AnthropicClassifier:
    """Real LLM classifier. Reads ANTHROPIC_API_KEY from the env (never logged).

    Lazily imports the SDK so the dependency is optional. Used only when a caller
    explicitly opts in; everything else stays offline.
    """
    model: str = "claude-haiku-4-5-20251001"   # cheap, fast classifier
    max_tokens: int = 400

    def classify(self, text: str, clusters: list[str]) -> dict[str, float]:  # pragma: no cover - network
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        import anthropic
        client = anthropic.Anthropic()      # reads the key from the env itself
        prompt = (
            "Classify the following statement excerpt into these AI-infrastructure "
            "thesis clusters. Return ONLY compact JSON mapping each present cluster "
            "to an intensity 0..1 (omit absent clusters). Clusters: "
            f"{', '.join(clusters)}.\n\nExcerpt:\n{text}")
        msg = client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}])
        body = "".join(getattr(b, "text", "") for b in msg.content)
        return _parse_scores(body, clusters)


def _parse_scores(body: str, clusters: list[str]) -> dict[str, float]:
    """Tolerantly extract a {cluster: float} JSON object from a model reply."""
    m = re.search(r"\{.*\}", body or "", re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    allowed = set(clusters)
    out: dict[str, float] = {}
    for k, v in (data.items() if isinstance(data, dict) else []):
        if k in allowed:
            try:
                out[k] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
    return out


class StatementClassifier:
    """Wraps an optional LLM with a deterministic keyword fallback.

    The result is ALWAYS advisory (``needs_human_review`` stays True). If the LLM
    is absent or errors, the keyword model is used and the method is reported
    honestly as ``keyword_fallback``.
    """

    def __init__(self, llm: Optional[LLMClient] = None,
                 clusters: tuple[str, ...] = THESIS_CLUSTERS):
        self.llm = llm
        self.clusters = list(clusters)

    def classify(self, text: str) -> dict[str, float]:
        """Callable signature compatible with statements.make_statement_ref."""
        return self.classify_detailed(text).clusters

    def classify_detailed(self, text: str) -> ClassificationResult:
        if self.llm is not None:
            try:
                scores = self.llm.classify(text, self.clusters)
                if scores:
                    return ClassificationResult(
                        clusters={k: round(v, 4) for k, v in scores.items()},
                        method="llm")
                logger.info("LLM returned no clusters — using keyword fallback")
            except Exception as exc:
                logger.warning("LLM classify failed (%s) — keyword fallback", exc)
        return ClassificationResult(
            clusters=classify_clusters(text), method="keyword_fallback")
