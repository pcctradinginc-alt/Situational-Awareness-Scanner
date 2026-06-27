"""Tests for the conviction / statement-feed layer (offline-injected).

Contract:
  * a first-party essay becomes a conviction signal classified into a thesis
    cluster, with DERIVED second-order candidates (hypothesis / watchlist);
  * a statement is always advisory (needs_human_review) and never a confirmed
    investment;
  * media feeds that don't name the person are dropped (require_name_match);
  * conviction signals flow through the SAME dedup store as filings, and the
    digest/notify count both as new signals (email-only-on-new).
"""
from datetime import date

import pytest

from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.proxy_map import load_proxy_map
from call_options_intel.person_intel.statement_feed import (
    FixtureFeedFetcher, StatementFeedMonitor, load_statement_monitor,
)
from call_options_intel.person_intel.monitor import run_monitor
from call_options_intel.person_intel.monitor_report import (
    render_markdown, render_email_html, monitor_and_notify,
)

AS_OF = date(2026, 6, 27)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


def _offline_monitor(cfg):
    ff = FixtureFeedFetcher(DEFAULT_FIXTURES)
    return StatementFeedMonitor(load_proxy_map(cfg.config_dir),
                                fetcher=ff, fixture_fetcher=ff)


def _feeds(cfg):
    _, feeds = load_statement_monitor(cfg.config_dir, cfg.data_sources,
                                      "offline", DEFAULT_FIXTURES)
    return feeds


def test_essay_becomes_conviction_signal(cfg):
    sigs = _offline_monitor(cfg).collect(_feeds(cfg), since_days=400, as_of=AS_OF)
    assert sigs, "expected at least one conviction signal from the fixture essay"
    s = sigs[0]
    assert s.principal == "aschenbrenner"
    assert s.tier == "official"
    assert s.dominant_cluster                       # classified into a cluster
    assert s.derived_candidates                     # second-order candidates derived
    assert s.needs_human_review is True             # always advisory
    assert s.content_hash.startswith("stmt:")       # namespaced for dedup


def test_derived_candidates_are_known_tickers(cfg):
    sigs = _offline_monitor(cfg).collect(_feeds(cfg), since_days=400, as_of=AS_OF)
    power = next((s for s in sigs if s.dominant_cluster == "power_grid"), None)
    assert power is not None
    # the power_grid cluster proxies include VST/CEG/GEV in the curated config
    assert any(t in power.derived_candidates for t in ("VST", "CEG", "GEV"))


def test_recency_filter(cfg):
    # only the recent essay (2026-06-24) survives a 30-day window
    sigs = _offline_monitor(cfg).collect(_feeds(cfg), since_days=30, as_of=AS_OF)
    assert all(s.age_days is None or s.age_days <= 30 for s in sigs)
    assert any(s.age_days == 3 for s in sigs)       # the Trillion-Dollar Cluster


def test_name_match_drops_unrelated_media():
    from call_options_intel.person_intel.proxy_map import load_proxy_map as lpm

    class _F:
        def for_feed(self, fixture):  # offline fixture path
            return ("<rss><channel><item><title>Generic market wrap</title>"
                    "<description>stocks rose on compute optimism</description>"
                    "<link>http://x/y</link>"
                    "<pubDate>Wed, 24 Jun 2026 09:00:00 GMT</pubDate>"
                    "</item></channel></rss>")
        def get(self, url):
            return None

    mon = StatementFeedMonitor(lpm(None), fetcher=_F(), fixture_fetcher=_F())
    feed = {"principal": "thiel", "speaker": "Peter Thiel", "source": "news",
            "tier": "media", "require_name_match": True, "fixture": "x.xml"}
    # article never names Thiel -> dropped despite a matching thesis keyword
    assert mon.collect([feed], since_days=400, as_of=AS_OF) == []


def test_statements_flow_through_dedup(cfg, tmp_path):
    sp = tmp_path / "seen.json"
    r1 = run_monitor(config=cfg, mode="offline", since_days=400, as_of=AS_OF,
                     state_path=sp, persist=True)
    assert r1.statement_count >= 1
    assert r1.total_new == r1.new_count + r1.statement_count
    # second run: both filings AND statements are de-duplicated
    r2 = run_monitor(config=cfg, mode="offline", since_days=400, as_of=AS_OF,
                     state_path=sp, persist=True)
    assert r2.total_new == 0


def test_digest_marks_statements_as_hypothesis(cfg):
    result = run_monitor(config=cfg, mode="offline", since_days=400, as_of=AS_OF,
                         state_path=None, persist=False)
    md = render_markdown(result)
    assert "Conviction / statement signals" in md
    assert "HYPOTHESIS" in md
    html = render_email_html(result, "now")
    assert "CONVICTION" in html
    assert "not a confirmed investment" in html


def test_notify_counts_both_signal_classes(cfg, tmp_path):
    out = monitor_and_notify(
        config=cfg, mode="offline", since_days=400, as_of=AS_OF,
        state_path=tmp_path / "seen.json", artifact_dir=tmp_path / "art",
        send_email=False)
    assert out["total_new"] == out["new_count"] + out["statement_count"]
    assert out["statement_count"] >= 1


def test_include_statements_toggle(cfg, tmp_path):
    r = run_monitor(config=cfg, mode="offline", since_days=400, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", include_statements=False,
                    persist=False)
    assert r.statement_count == 0


# ── v2 polish: html-strip · stopwords · media threshold · de-dup ────────────
from call_options_intel.person_intel.proxy_map import load_proxy_map as _lpm
from call_options_intel.person_intel.statement_feed import (
    StatementFeedMonitor, _strip_html,
)


def _rss(*items):
    return "<rss><channel>" + "".join(items) + "</channel></rss>"


def _item(title, desc, link, date="Wed, 24 Jun 2026 09:00:00 GMT"):
    return (f"<item><title>{title}</title><description>{desc}</description>"
            f"<link>{link}</link><pubDate>{date}</pubDate></item>")


class _Raw:
    def __init__(self, raw):
        self.raw = raw
    def for_feed(self, fixture):
        return self.raw
    def get(self, url):
        return self.raw


def _media_feed():
    return {"principal": "thiel", "speaker": "Peter Thiel", "source": "news",
            "tier": "media", "require_name_match": True, "fixture": "x.xml"}


def _mon(raw, **kw):
    f = _Raw(raw)
    return StatementFeedMonitor(_lpm(None), fetcher=f, fixture_fetcher=f, **kw)


def test_strip_html_removes_tags_and_entities():
    out = _strip_html('Compute &amp; power <a href="http://x">link</a>  here')
    assert "<" not in out and "&amp;" not in out
    assert "Compute & power" in out and "link" in out


def test_excerpt_has_no_html():
    raw = _rss(_item(
        "Peter Thiel on compute and data centers",
        'Thiel: gigawatt data centers and power grid <a href="http://n/1">[link]</a>',
        "http://news/1"))
    sigs = _mon(raw, media_min_clusters=1).collect(
        [_media_feed()], since_days=400, as_of=AS_OF)
    assert sigs
    assert "<a" not in sigs[0].excerpt and "href" not in sigs[0].excerpt


def test_gossip_stopword_dropped():
    raw = _rss(_item(
        "Powerful Miami men on roster of secret society run by Peter Thiel",
        "society gossip power players", "http://news/2"))
    # names Thiel and trips 'power', but it's gossip -> dropped
    assert _mon(raw, media_min_clusters=1).collect(
        [_media_feed()], since_days=400, as_of=AS_OF) == []


def test_media_breadth_filters_weak_relevance():
    raw = _rss(_item("Peter Thiel mentioned compute once",
                     "a single compute mention", "http://news/3"))
    # one thesis cluster only -> below the media breadth bar (>=2) -> dropped
    assert _mon(raw, media_min_clusters=2).collect(
        [_media_feed()], since_days=400, as_of=AS_OF) == []


def test_near_duplicate_stories_collapse():
    raw = _rss(
        _item("Peter Thiel boosts compute, power grid and data centers - Yahoo",
              "compute power grid data centers nuclear", "http://news/a"),
        _item("Peter Thiel boosts compute, power grid and data centers - Yahoo Canada",
              "compute power grid data centers nuclear", "http://news/b"),
    )
    sigs = _mon(raw, media_min_clusters=1).collect(
        [_media_feed()], since_days=400, as_of=AS_OF)
    # same speaker + date + dominant cluster -> one survivor
    assert len(sigs) == 1
