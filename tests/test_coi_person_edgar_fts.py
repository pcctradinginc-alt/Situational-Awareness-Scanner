"""Tests for the EDGAR Full-Text-Search vorfeld discovery + new-entity flagging."""
from datetime import date

from call_options_intel.config_loader import AppConfig, load_yaml
from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.edgar_fts import (
    EdgarFTSClient, FixtureFTSFetcher, load_fts_client,
)
from call_options_intel.person_intel.filings import FilingType
from call_options_intel.person_intel.monitor import run_monitor

AS_OF = date(2026, 6, 27)


def _client():
    return EdgarFTSClient(fetcher=FixtureFTSFetcher(DEFAULT_FIXTURES))


def _terms():
    cfg = AppConfig()
    return load_yaml("early_sources", cfg.config_dir)["edgar_fts"]["terms"]


def test_search_reduces_hits_to_typed_refs():
    hits = _client().search("Founders Fund")
    assert hits
    ref = hits[0].ref
    assert ref.filing_type is FilingType.FORM_D
    assert ref.cik == "0002099999"
    assert "FF VENTURES" in ref.entity
    assert ref.accession == "0002099999-26-000004"
    assert ref.url.startswith("https://www.sec.gov/Archives/edgar/data/")


def test_display_name_cik_suffix_stripped():
    hit = _client().search("Peter Thiel")[0]
    assert "(CIK" not in hit.ref.entity        # the " (CIK ...)" suffix is removed
    assert hit.ref.entity == "THIEL PETER"


def test_discover_dedups_and_filters_recency():
    hits = _client().discover(_terms(), since_days=30, as_of=AS_OF)
    accs = [h.ref.accession for h in hits]
    assert len(accs) == len(set(accs))          # deduped by accession
    # both fixture hits are within 30 days of AS_OF
    assert "0002099999-26-000004" in accs
    assert "0001211060-26-000051" in accs


def test_recency_window_excludes_old():
    # a 1-day window excludes the 25/26-Jun hits relative to AS_OF (27 Jun)
    hits = _client().discover(_terms(), since_days=0, as_of=AS_OF)
    assert hits == []


def test_load_client_offline_uses_fixtures():
    cfg = AppConfig()
    client = load_fts_client(cfg.data_sources, "offline", DEFAULT_FIXTURES)
    assert isinstance(client.fetcher, FixtureFTSFetcher)


# ── monitor integration ─────────────────────────────────────────────────────
def test_monitor_surfaces_new_entity(tmp_path):
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                      state_path=tmp_path / "seen.json", persist=False)
    new = [a for a in res.alerts if a.is_new_entity]
    assert new, "expected a NEW-ENTITY discovery from FTS"
    a = new[0]
    assert a.discovered_via == "edgar_fts"
    assert "FF VENTURES" in a.entity
    assert a.cik == "0002099999"
    assert a.matched_term == "Founders Fund"
    assert a.principal == "thiel"            # named in filing -> principal floor
    assert a.path_weight >= 0.5
    assert a.needs_human_review is True      # unconfirmed control link
    assert "NEW ENTITY" in a.category


def test_fts_known_entity_is_not_flagged_new(tmp_path):
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                      state_path=tmp_path / "seen.json", persist=False)
    # the FTS-discovered Thiel SC 13D is a KNOWN filer -> not a new entity
    thiel_fts = [a for a in res.alerts
                 if a.discovered_via == "edgar_fts" and not a.is_new_entity]
    assert thiel_fts
    assert thiel_fts[0].path_weight == 1.0


def test_fts_discovery_is_deduped_by_store(tmp_path):
    sp = tmp_path / "seen.json"
    r1 = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                     state_path=sp, persist=True)
    assert any(a.is_new_entity for a in r1.alerts)
    # second run: the FTS discoveries are already in the store -> not repeated
    r2 = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                     state_path=sp, persist=True)
    assert r2.new_count == 0
