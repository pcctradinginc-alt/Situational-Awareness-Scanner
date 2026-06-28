"""Tests for the non-filing VORFELD change-detection (ADV/IAPD · jobs · domain)."""
import json
from datetime import date

from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.vorfeld import (
    AdvIapdAdapter, CertTransparencyAdapter, DomainWatchAdapter, FixtureFetcher,
    JobPostingsAdapter, SnapshotStore, collect_vorfeld,
)
from call_options_intel.person_intel.monitor import run_monitor

AS_OF = date(2026, 6, 27)


def _fx():
    return FixtureFetcher(DEFAULT_FIXTURES)


def _adv_prior():
    return {"firm_name": "Founders Fund LLC", "aum": 10_000_000_000,
            "control_persons": ["Peter Thiel", "Napoleon Ta"],
            "private_funds": ["Founders Fund VII", "Founders Fund Growth III"],
            "main_office": "1 Letterman Drive, San Francisco, CA"}


# ── baseline behaviour ───────────────────────────────────────────────────────
def test_first_sight_is_baseline_only():
    snap = SnapshotStore(path=None)
    sigs = AdvIapdAdapter(_fx(), _fx()).changes(
        [{"crd": "155462", "principal": "thiel"}], snap, AS_OF)
    assert sigs == []                              # first sight: no alert
    assert snap.get("adv:155462") is not None      # but baseline stored


# ── ADV / IAPD ───────────────────────────────────────────────────────────────
def test_adv_detects_aum_control_and_fund_changes():
    snap = SnapshotStore(path=None)
    snap.set("adv:155462", _adv_prior())
    sigs = AdvIapdAdapter(_fx(), _fx()).changes(
        [{"crd": "155462", "principal": "thiel"}], snap, AS_OF)
    kinds = {s.kind for s in sigs}
    assert "aum_change" in kinds                    # 10B -> 12.5B = +25%
    assert "new_control_person" in kinds
    assert "new_private_fund" in kinds
    fund = next(s for s in sigs if s.kind == "new_private_fund")
    assert "FF Compute SPV I" in fund.detail
    assert all(s.needs_human_review for s in sigs)
    assert all(s.content_hash.startswith("adv:155462:") for s in sigs)


def test_adv_small_aum_move_not_reported():
    snap = SnapshotStore(path=None)
    prior = _adv_prior()
    prior["aum"] = 13_000_000_000                   # 13.0B -> 14.0B = +7.7% < 10%
    # align the other fields with the fixture so ONLY aum could differ
    prior["control_persons"] = ["Peter Thiel", "Napoleon Ta", "Jane Doe (new GP)"]
    prior["private_funds"] = ["Founders Fund VII", "Founders Fund Growth III",
                              "FF Compute SPV I"]
    snap.set("adv:155462", prior)
    sigs = AdvIapdAdapter(_fx(), _fx()).changes(
        [{"crd": "155462", "principal": "thiel"}], snap, AS_OF)
    assert [s for s in sigs if s.kind == "aum_change"] == []


# ── job postings ─────────────────────────────────────────────────────────────
def test_jobs_new_role_classified_into_cluster():
    snap = SnapshotStore(path=None)
    snap.set("jobs:andurilindustries", {"ids": ["5510003"]})   # office mgr known
    sigs = JobPostingsAdapter(_fx(), _fx()).changes(
        [{"token": "andurilindustries", "name": "Anduril", "principal": "thiel"}],
        snap, AS_OF)
    titles = {s.detail for s in sigs}
    assert any("Power Systems" in t for t in titles)
    clusters = {s.cluster for s in sigs}
    assert "power_grid" in clusters or "compute" in clusters
    # the already-seen office-manager role is not re-emitted
    assert all("Office Manager" not in s.detail for s in sigs)


# ── domain watch ─────────────────────────────────────────────────────────────
def test_domain_hash_change_detected():
    snap = SnapshotStore(path=None)
    snap.set("dom:https_foundersfund_com", {"hash": "OLDHASH", "title": "old"})
    sigs = DomainWatchAdapter(_fx(), _fx()).changes(
        [{"url": "https://foundersfund.com/", "entity": "Founders Fund",
          "principal": "thiel"}], snap, AS_OF)
    assert len(sigs) == 1
    assert sigs[0].kind == "site_change"
    assert "VIII" in sigs[0].detail or "title" in sigs[0].detail


def test_domain_no_change_when_hash_matches():
    snap = SnapshotStore(path=None)
    # first establish the real baseline, then re-run with the same fixture
    a = DomainWatchAdapter(_fx(), _fx())
    a.changes([{"url": "https://foundersfund.com/", "entity": "FF"}], snap, AS_OF)
    sigs = a.changes([{"url": "https://foundersfund.com/", "entity": "FF"}],
                     snap, AS_OF)
    assert sigs == []


# ── certificate transparency (new domains) ──────────────────────────────────
_CERT_PATS = [{"q": "foundersfund", "principal": "thiel"}]


def test_cert_first_sight_is_baseline_only():
    snap = SnapshotStore(path=None)
    sigs = CertTransparencyAdapter(_fx(), _fx()).changes(_CERT_PATS, snap, AS_OF)
    assert sigs == []                              # baseline, no alert
    assert snap.get("cert:foundersfund") is not None


def test_cert_detects_new_domain():
    snap = SnapshotStore(path=None)
    snap.set("cert:foundersfund",
             {"domains": ["foundersfund.com", "www.foundersfund.com"]})
    sigs = CertTransparencyAdapter(_fx(), _fx()).changes(_CERT_PATS, snap, AS_OF)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "new_domain"
    assert s.entity == "ff-compute-spv.foundersfund.com"
    assert s.cluster == "compute"                  # classified from the name
    assert s.needs_human_review is True
    assert s.source == "cert_transparency"


def test_cert_old_domain_outside_window_skipped():
    # a 1-day recency window excludes the 24-Jun cert relative to 27-Jun
    snap = SnapshotStore(path=None)
    snap.set("cert:foundersfund",
             {"domains": ["foundersfund.com", "www.foundersfund.com"]})
    sigs = CertTransparencyAdapter(_fx(), _fx(), recent_days=1).changes(
        _CERT_PATS, snap, AS_OF)
    assert sigs == []


# ── snapshot persistence ─────────────────────────────────────────────────────
def test_snapshot_roundtrip(tmp_path):
    p = tmp_path / "snap.json"
    snap = SnapshotStore.load(p)
    snap.set("k", {"v": 1})
    snap.save()
    assert json.loads(p.read_text())["k"]["v"] == 1
    assert SnapshotStore.load(p).get("k") == {"v": 1}


# ── monitor integration ──────────────────────────────────────────────────────
def test_vorfeld_flows_through_monitor(tmp_path):
    snap = tmp_path / "snap.json"
    # seed prior state so the bundled fixtures register as CHANGES
    snap.write_text(json.dumps({
        "adv:155462": _adv_prior(),
        "jobs:andurilindustries": {"ids": ["5510003"]},
        "dom:https_foundersfund_com": {"hash": "OLD", "title": "old"},
        "dom:https_situational_awareness_ai": {"hash": "OLD", "title": "old"},
        "cert:foundersfund": {"domains": ["foundersfund.com",
                                          "www.foundersfund.com"]},
    }))
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                      state_path=tmp_path / "seen.json",
                      vorfeld_snapshot_path=snap, persist=False)
    assert res.vorfeld_count >= 6
    assert res.total_new == res.new_count + res.statement_count + res.vorfeld_count
    sources = {v.source for v in res.vorfeld}
    assert {"adv_iapd", "job_postings", "domain_watch",
            "cert_transparency"} <= sources
    # every vorfeld signal carries the three-axis score
    for v in res.vorfeld:
        assert "gate_pass" in v.triple


def test_vorfeld_baseline_then_dedup(tmp_path):
    snap = tmp_path / "snap.json"
    seen = tmp_path / "seen.json"
    # run 1: no prior snapshot -> baseline only, zero vorfeld
    r1 = run_monitor(mode="offline", as_of=AS_OF, since_days=40, state_path=seen,
                     vorfeld_snapshot_path=snap, persist=True)
    assert r1.vorfeld_count == 0
    # run 2: fixtures unchanged vs the now-saved baseline -> still zero
    r2 = run_monitor(mode="offline", as_of=AS_OF, since_days=40, state_path=seen,
                     vorfeld_snapshot_path=snap, persist=True)
    assert r2.vorfeld_count == 0
