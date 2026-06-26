"""
statements.py
=============
Goal E — a DISCOVERY-ONLY statement layer for news / podcasts / interviews.

Hard constraints, by design:
  * we store ONLY url + content hash + source + date + a SHORT excerpt — never
    full third-party text (the excerpt is hard-truncated);
  * official / primary sources outrank media reposts;
  * classification into thesis clusters is ADVISORY (a transparent keyword model
    standing in for an LLM extractor) — it never makes the final call, so every
    ref carries ``needs_human_review``;
  * dedup removes exact duplicates (same normalised URL + excerpt) and, per
    story, keeps the most authoritative source.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .proxy_map import classify_clusters

logger = logging.getLogger("coi.person.statements")

MAX_EXCERPT_CHARS = 280


class SourceTier(str, Enum):
    """Authority ranking. Lower ``rank`` = more authoritative."""
    OFFICIAL = "official"     # SEC, the person's own site/essay, company IR
    PRIMARY = "primary"       # first-party interview/podcast recording
    MEDIA = "media"           # journalism reporting on it
    REPOST = "repost"         # aggregator / social repost

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]

    @classmethod
    def parse(cls, value: object) -> "SourceTier":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.MEDIA


_TIER_RANK = {
    SourceTier.OFFICIAL: 0, SourceTier.PRIMARY: 1,
    SourceTier.MEDIA: 2, SourceTier.REPOST: 3,
}


@dataclass
class StatementRef:
    url: str
    source: str
    tier: SourceTier
    date: str                          # ISO date
    excerpt: str                       # SHORT, truncated — never full text
    content_hash: str = ""
    thesis_clusters: dict[str, float] = field(default_factory=dict)
    speaker: Optional[str] = None      # entity id or name
    needs_human_review: bool = True    # classification is advisory, not final


def _normalise_url(url: str) -> str:
    """Scheme/host-lowercased, query + fragment + trailing slash stripped, so the
    same article with tracking params dedupes to one key."""
    u = (url or "").strip()
    for sep in ("?", "#"):
        if sep in u:
            u = u.split(sep, 1)[0]
    u = u.rstrip("/")
    low = u.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.lower().startswith("www."):
        u = u[4:]
    return u.lower()


def _hash(url: str, excerpt: str) -> str:
    key = _normalise_url(url) + "|" + " ".join((excerpt or "").lower().split())
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def make_statement_ref(
    url: str, source: str, tier: object, date: str, excerpt: str,
    speaker: Optional[str] = None, max_excerpt_chars: int = MAX_EXCERPT_CHARS,
    classifier: Callable[[str], dict[str, float]] = classify_clusters,
) -> StatementRef:
    """Build a discovery ref. The excerpt is hard-truncated; full text is never
    stored. Classification is advisory only."""
    short = (excerpt or "").strip()
    if len(short) > max_excerpt_chars:
        short = short[:max_excerpt_chars].rstrip() + "…"
    t = SourceTier.parse(tier)
    return StatementRef(
        url=url, source=source, tier=t, date=date, excerpt=short,
        content_hash=_hash(url, short),
        thesis_clusters=classifier(short), speaker=speaker,
        needs_human_review=True,
    )


def dedupe_exact(refs: list[StatementRef]) -> list[StatementRef]:
    """Drop exact duplicates by content hash (keeps first seen)."""
    seen: set[str] = set()
    out: list[StatementRef] = []
    for r in refs:
        h = r.content_hash or _hash(r.url, r.excerpt)
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
    return out


def _default_story_key(r: StatementRef) -> str:
    # same speaker + date + dominant cluster ≈ the same underlying story
    dom = max(r.thesis_clusters, key=r.thesis_clusters.get) if r.thesis_clusters else ""
    return f"{(r.speaker or '').lower()}|{r.date}|{dom}"


def collapse_to_authoritative(
    refs: list[StatementRef],
    key_fn: Callable[[StatementRef], str] = _default_story_key,
) -> list[StatementRef]:
    """Per story, keep the single most authoritative source (official > media).

    Exact duplicates are removed first; then, within each story key, the lowest
    tier-rank (most official) ref wins. Ties keep the earliest date.
    """
    best: dict[str, StatementRef] = {}
    for r in dedupe_exact(refs):
        k = key_fn(r)
        cur = best.get(k)
        if cur is None or (r.tier.rank, r.date) < (cur.tier.rank, cur.date):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r.date, r.tier.rank))


class StatementStore:
    """In-memory discovery store with hash-based dedup on add."""

    def __init__(self) -> None:
        self._by_hash: dict[str, StatementRef] = {}

    def add(self, ref: StatementRef) -> bool:
        """Returns True if newly added, False if a duplicate hash already exists."""
        if ref.content_hash in self._by_hash:
            return False
        self._by_hash[ref.content_hash] = ref
        return True

    def all(self) -> list[StatementRef]:
        return list(self._by_hash.values())

    def authoritative(self) -> list[StatementRef]:
        return collapse_to_authoritative(self.all())
