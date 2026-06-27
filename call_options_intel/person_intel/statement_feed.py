"""
statement_feed.py
=================
The **conviction** signal class for the twice-daily radar.

Where :mod:`monitor` tracks *filings* (what Thiel/Aschenbrenner actually DID),
this module tracks public *statements* (what they SAY) — first-party essays
(situational-awareness.ai, forourposterity.com) and public news-search RSS.
A repeated, intensifying thesis is an *indirect conviction* signal that often
precedes the filing.

It reuses the discovery-only :mod:`statements` layer (url + short excerpt +
content hash + advisory cluster classification — **never** full third-party
text) and the :mod:`proxy_map` to derive **second-order** public candidates from
the dominant thesis cluster. Those candidates are HYPOTHESES (watchlist), kept
strictly separate from confirmed filings — a statement never becomes an
"investment".

The HTTP layer is injectable (offline fixtures by default, live RSS only on
opt-in); ``feedparser`` (already a dependency) parses the feed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from ..config_loader import load_yaml
from .proxy_map import ProxyMap, classify_clusters, load_proxy_map
from .statements import MAX_EXCERPT_CHARS, SourceTier, _hash, _normalise_url

logger = logging.getLogger("coi.person.statement_feed")

PRINCIPAL_NAMES = {
    "thiel": ("Peter Thiel", ("thiel", "peter thiel")),
    "aschenbrenner": ("Leopold Aschenbrenner",
                      ("aschenbrenner", "leopold aschenbrenner", "leopold")),
}


# ── injectable fetch layer ──────────────────────────────────────────────────
class FeedFetcher(Protocol):
    def get(self, url: str) -> Optional[str]: ...


@dataclass
class UrllibFeedFetcher:
    """Real RSS fetch via stdlib urllib. Used only in live mode."""
    user_agent: str = "SA-Scanner research (info@pcctradinginc.com)"
    timeout_s: int = 15

    def get(self, url: str) -> Optional[str]:  # pragma: no cover - network
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:
            logger.warning("feed fetch failed (%s): %s", url, exc)
            return None


@dataclass
class FixtureFeedFetcher:
    """Offline fetch — resolves the per-feed ``fixture`` filename under
    ``<fixtures>/statements/``. Returns None when no fixture is configured."""
    fixtures_dir: Path
    _by_fixture: dict[str, str] = field(default_factory=dict)

    def for_feed(self, fixture: str) -> Optional[str]:
        if not fixture:
            return None
        path = Path(self.fixtures_dir) / "statements" / fixture
        if not path.exists():
            logger.info("no statement fixture %s", fixture)
            return None
        return path.read_text(encoding="utf-8")

    def get(self, url: str) -> Optional[str]:   # not used offline (fixture keyed)
        return None


# ── the conviction signal ───────────────────────────────────────────────────
@dataclass
class StatementSignal:
    principal: str                 # thiel | aschenbrenner
    speaker: str
    source: str
    tier: str                      # official | primary | media | repost
    url: str
    date: str                      # ISO date
    age_days: Optional[int]
    excerpt: str                   # SHORT, truncated — never full text
    content_hash: str
    dominant_cluster: str
    cluster_scores: dict[str, float]
    derived_candidates: list[str]  # tickers — SECOND-ORDER HYPOTHESIS only
    falsification: str
    needs_human_review: bool = True   # statements are always advisory
    signal_kind: str = "conviction_statement"
    headline: str = ""
    why_it_matters: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_date(entry) -> Optional[str]:
    pp = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None)
    if pp:
        try:
            return date(pp.tm_year, pp.tm_mon, pp.tm_mday).isoformat()
        except (ValueError, TypeError):
            pass
    raw = getattr(entry, "published", "") or getattr(entry, "updated", "")
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return None


def _age_days(iso: Optional[str], as_of: date) -> Optional[int]:
    if not iso:
        return None
    try:
        return (as_of - date.fromisoformat(iso)).days
    except ValueError:
        return None


class StatementFeedMonitor:
    """Collect conviction signals from configured statement feeds."""

    def __init__(self, proxy_map: ProxyMap, fetcher: FeedFetcher,
                 fixture_fetcher: Optional[FixtureFeedFetcher] = None,
                 max_candidates: int = 3, min_intensity: float = 0.25):
        self.proxy_map = proxy_map
        self.fetcher = fetcher
        self.fixture_fetcher = fixture_fetcher
        self.max_candidates = max_candidates
        self.min_intensity = min_intensity

    # derive SECOND-ORDER candidates from the dominant cluster ----------------
    def _derived_candidates(self, dominant: str) -> list[str]:
        c = self.proxy_map.cluster(dominant)
        if not c or not c.proxies:
            return []
        ranked = sorted(c.proxies, key=lambda p: p.quality(), reverse=True)
        out: list[str] = []
        for p in ranked:
            if p.ticker not in out:
                out.append(p.ticker)
            if len(out) >= self.max_candidates:
                break
        return out

    def _raw_for(self, feed: dict) -> Optional[str]:
        """Fixture in offline mode, network in live mode."""
        if self.fixture_fetcher is not None:
            return self.fixture_fetcher.for_feed(feed.get("fixture", ""))
        return self.fetcher.get(feed.get("url", ""))

    def _build_signal(self, feed: dict, entry, as_of: date
                      ) -> Optional[StatementSignal]:
        principal = str(feed.get("principal", "")).lower()
        pname, name_keys = PRINCIPAL_NAMES.get(principal, (feed.get("speaker", ""), ()))
        title = (getattr(entry, "title", "") or "").strip()
        summary = (getattr(entry, "summary", "")
                   or getattr(entry, "description", "") or "").strip()
        blob = f"{title}. {summary}"

        # fuzzy media feeds must actually name the person
        if feed.get("require_name_match", False) and name_keys:
            low = blob.lower()
            if not any(k in low for k in name_keys):
                return None

        url = (getattr(entry, "link", "") or "").strip()
        iso = _parse_date(entry)
        excerpt = blob.strip()
        if len(excerpt) > MAX_EXCERPT_CHARS:
            excerpt = excerpt[:MAX_EXCERPT_CHARS].rstrip() + "…"

        scores = classify_clusters(blob)
        if not scores:
            return None
        dominant = max(scores, key=scores.get)
        if scores.get(dominant, 0.0) < self.min_intensity:
            return None

        derived = self._derived_candidates(dominant)
        fals = self.proxy_map.falsification_for(dominant) or ""
        tier = SourceTier.parse(feed.get("tier", "media"))

        headline = f"{pname}: “{title}” → cluster «{dominant}»"
        why = (f"Conviction signal (what they SAY): a {tier.value}-tier "
               f"statement weighting the «{dominant}» thesis. Derived public "
               f"proxies are SECOND-ORDER hypotheses (watchlist), not a "
               f"confirmed investment — corroborate with a filing.")

        return StatementSignal(
            principal=principal, speaker=pname, source=str(feed.get("source", "")),
            tier=tier.value, url=url, date=iso or "",
            age_days=_age_days(iso, as_of), excerpt=excerpt,
            content_hash="stmt:" + _hash(url, excerpt),
            dominant_cluster=dominant, cluster_scores=scores,
            derived_candidates=derived, falsification=fals,
            headline=headline, why_it_matters=why)

    def collect(self, feeds: list[dict], *, since_days: int = 30,
                as_of: Optional[date] = None) -> list[StatementSignal]:
        import feedparser
        as_of = as_of or date.today()
        out: list[StatementSignal] = []
        seen_hashes: set[str] = set()
        for feed in feeds or []:
            raw = self._raw_for(feed)
            if not raw:
                continue
            parsed = feedparser.parse(raw)
            for entry in parsed.entries:
                sig = self._build_signal(feed, entry, as_of)
                if sig is None:
                    continue
                if sig.age_days is not None and (
                        sig.age_days < 0 or sig.age_days > since_days):
                    continue
                if sig.content_hash in seen_hashes:
                    continue
                seen_hashes.add(sig.content_hash)
                out.append(sig)
        # most recent first; official tier before media on ties
        out.sort(key=lambda s: (s.age_days if s.age_days is not None else 999,
                                SourceTier.parse(s.tier).rank))
        return out


def load_statement_monitor(config_dir, data_sources_cfg: dict, mode: str,
                           fixtures_dir) -> tuple[StatementFeedMonitor, list[dict]]:
    """Build a statement monitor + the configured feed list for the given mode."""
    cfg = load_yaml("statement_sources", Path(config_dir) if config_dir else None)
    feeds = cfg.get("feeds", []) or []
    deriv = cfg.get("derivation", {}) or {}
    proxy_map = load_proxy_map(config_dir)
    if mode == "live":
        ua = ((data_sources_cfg or {}).get("edgar_13f", {})
              .get("user_agent", "SA-Scanner research (info@pcctradinginc.com)"))
        monitor = StatementFeedMonitor(
            proxy_map, fetcher=UrllibFeedFetcher(user_agent=ua),
            max_candidates=int(deriv.get("max_candidates_per_statement", 3)),
            min_intensity=float(deriv.get("min_cluster_intensity", 0.25)))
    else:
        ff = FixtureFeedFetcher(Path(fixtures_dir))
        monitor = StatementFeedMonitor(
            proxy_map, fetcher=ff, fixture_fetcher=ff,
            max_candidates=int(deriv.get("max_candidates_per_statement", 3)),
            min_intensity=float(deriv.get("min_cluster_intensity", 0.25)))
    return monitor, feeds
