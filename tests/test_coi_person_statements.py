"""Tests for the discovery-only statement layer (Goal E)."""
from call_options_intel.person_intel.statements import (
    SourceTier, StatementStore, collapse_to_authoritative, dedupe_exact,
    make_statement_ref,
)


def test_excerpt_is_truncated_no_full_text():
    long_text = "compute " * 100      # ~800 chars
    ref = make_statement_ref(
        "https://example.com/a", "Example", "media", "2026-06-01", long_text,
        max_excerpt_chars=50)
    assert len(ref.excerpt) <= 51       # truncated + ellipsis
    assert ref.excerpt.endswith("…")
    assert ref.content_hash


def test_classification_is_advisory_only():
    ref = make_statement_ref(
        "https://situational-awareness.ai/essay", "SA", "official", "2026-06-01",
        "nuclear power grid gigawatts for compute", speaker="aschenbrenner")
    assert ref.thesis_clusters                 # got some classification
    assert ref.needs_human_review is True      # but never the final word


def test_exact_dedup_by_hash():
    a = make_statement_ref("https://x.com/p?utm_source=tw", "X", "media",
                           "2026-06-01", "same story text")
    b = make_statement_ref("https://www.x.com/p", "X", "media",
                           "2026-06-01", "same story text")
    # tracking params + www + scheme normalise to the SAME hash
    assert a.content_hash == b.content_hash
    assert len(dedupe_exact([a, b])) == 1


def test_collapse_prefers_official_over_media():
    official = make_statement_ref(
        "https://situational-awareness.ai/post", "SA (official)", "official",
        "2026-06-02", "compute and power grid build-out", speaker="aschenbrenner")
    media = make_statement_ref(
        "https://news.example.com/story", "SomeNews", "media",
        "2026-06-02", "compute and power grid build-out", speaker="aschenbrenner")
    repost = make_statement_ref(
        "https://aggregator.example.com/x", "Aggregator", "repost",
        "2026-06-02", "compute and power grid build-out", speaker="aschenbrenner")
    kept = collapse_to_authoritative([media, repost, official])
    assert len(kept) == 1
    assert kept[0].tier is SourceTier.OFFICIAL


def test_unknown_tier_defaults_to_media():
    ref = make_statement_ref("https://x", "X", "garbage-tier", "2026-06-01", "text")
    assert ref.tier is SourceTier.MEDIA


def test_store_dedups_on_add():
    store = StatementStore()
    r1 = make_statement_ref("https://x.com/p", "X", "media", "2026-06-01", "text one")
    r2 = make_statement_ref("https://x.com/p/", "X", "media", "2026-06-01", "text one")
    assert store.add(r1) is True
    assert store.add(r2) is False        # duplicate
    assert len(store.all()) == 1
