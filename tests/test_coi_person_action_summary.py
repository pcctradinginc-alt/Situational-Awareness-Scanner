"""Tests for the noise-reduction + action-first digest:

  * a batch of new job postings is ONE ``hiring_wave`` signal, not N near-
    identical alerts (the "50 new signals" email problem);
  * ``MonitorResult.review_queue`` surfaces the principal-linked filings that
    are stuck on subject verification (the one manual step that unlocks them);
  * the digest/email leads with the honest split: handelbar · prüfen · Kontext.
"""
import json
from datetime import date

from call_options_intel.person_intel.monitor import MonitorResult, PersonAlert
from call_options_intel.person_intel.monitor_report import (
    _action_counts, render_email_html, render_markdown,
)
from call_options_intel.person_intel.vorfeld import (
    FixtureFetcher, JobPostingsAdapter, SnapshotStore, VorfeldSignal,
)

AS_OF = date(2026, 7, 3)


def _jobs_fixture(tmp_path, titles):
    d = tmp_path / "vorfeld" / "jobs"
    d.mkdir(parents=True)
    rows = [{"id": str(1000 + i), "title": t,
             "absolute_url": f"https://boards.greenhouse.io/testco/{i}"}
            for i, t in enumerate(titles)]
    (d / "testco.json").write_text(json.dumps({"jobs": rows}), encoding="utf-8")
    return FixtureFetcher(tmp_path)


def _changes(fx, snap):
    return JobPostingsAdapter(fx, fx).changes(
        [{"token": "testco", "name": "TestCo", "principal": "thiel"}],
        snap, AS_OF)


# ── hiring-wave aggregation ──────────────────────────────────────────────────
def test_many_new_roles_collapse_to_one_hiring_wave(tmp_path):
    titles = ["Robotics Software Engineer", "Senior Robotics Software Engineer",
              "GPU Compute Infrastructure Lead", "Technical Recruiter",
              "Buyer", "Production Scheduler"]
    fx = _jobs_fixture(tmp_path, titles)
    snap = SnapshotStore(path=None)
    snap.set("jobs:testco", {"ids": ["9999"]})            # baseline exists, all new
    sigs = _changes(fx, snap)
    assert len(sigs) == 1                           # ONE signal, not six
    wave = sigs[0]
    assert wave.kind == "hiring_wave"
    assert "6 new roles" in wave.headline
    assert "thesis clusters" in wave.detail
    assert wave.cluster                             # dominant cluster carried
    assert wave.needs_human_review


def test_few_new_roles_stay_individual(tmp_path):
    fx = _jobs_fixture(tmp_path, ["Robotics Software Engineer",
                                  "Office Manager", "Buyer"])
    snap = SnapshotStore(path=None)
    snap.set("jobs:testco", {"ids": ["9999"]})
    sigs = _changes(fx, snap)
    assert len(sigs) == 3                           # below threshold: unchanged
    assert {s.kind for s in sigs} == {"new_role"}


def test_hiring_wave_hash_is_stable_for_dedup(tmp_path):
    titles = ["A", "B", "C", "D", "E"]
    fx = _jobs_fixture(tmp_path, titles)
    s1, s2 = SnapshotStore(path=None), SnapshotStore(path=None)
    s1.set("jobs:testco", {"ids": ["9999"]})
    s2.set("jobs:testco", {"ids": ["9999"]})
    h1 = _changes(fx, s1)[0].content_hash
    h2 = _changes(fx, s2)[0].content_hash
    assert h1 == h2                                 # same batch → same dedup key


# ── review queue + action summary ────────────────────────────────────────────
def _alert(**kw):
    base = dict(
        entity="Situational Awareness LP", entity_id="salp", cik="1",
        filing_type="Form 4", form_raw="4", role="early",
        category="Insider transaction (Form 4)", signal_kind="insider_trade",
        filing_date="2026-07-02", age_days=1, accession="acc-1", url="",
        principal="aschenbrenner", path_weight=1.0,
        link_confidence="principal", subject_ticker=None,
        needs_human_review=True, triple={"gate_pass": False})
    base.update(kw)
    return PersonAlert(**base)


def _wave():
    return VorfeldSignal(
        source="job_postings", principal="thiel", entity="Anduril",
        kind="hiring_wave", headline="Anduril: hiring wave — 48 new roles",
        detail="48 new roles in one sweep", cluster="automation",
        content_hash="job:x:1", triple={"gate_pass": False})


def test_review_queue_only_unresolved_principal_filings():
    pending = _alert()
    resolved = _alert(accession="acc-2", subject_ticker="PLTR",
                      needs_human_review=False)
    weak = _alert(accession="acc-3", path_weight=0.3, principal="thiel")
    private = _alert(accession="acc-4", is_private=True)
    lagged = _alert(accession="acc-5", signal_kind="quarterly_13f",
                    filing_type="13F-HR")
    r = MonitorResult(new_count=5, mode="offline", run_at="t", principal_count=2,
                      alerts=[pending, resolved, weak, private, lagged])
    assert [a.accession for a in r.review_queue] == ["acc-1"]


def test_digest_leads_with_action_split():
    r = MonitorResult(new_count=1, alerts=[_alert()], mode="offline",
                      run_at="t", principal_count=1, vorfeld=[_wave()])
    assert _action_counts(r) == (0, 1, 1)
    md = render_markdown(r)
    assert "Was tun? — 0 handelbar · 1 prüfen · 1 Kontext" in md
    assert "PRÜFEN: Situational Awareness LP" in md
    assert "cusip_map.yml" in md                    # the concrete unlock step
    assert "Heute nichts kaufen" in md
    html = render_email_html(r, "now")
    assert "0 handelbar · 1 prüfen · 1 Kontext" in html
    assert "cusip_map.yml" in html


def test_sizeable_candidate_leads_the_summary():
    a = _alert(subject_ticker="PLTR", needs_human_review=False,
               triple={"gate_pass": True, "final_trade_score": 8.0,
                       "ticker": "PLTR"},
               ev={"passed": True, "reasons": ["EV +12% after fills"]})
    r = MonitorResult(new_count=1, alerts=[a], mode="offline", run_at="t",
                      principal_count=1)
    assert _action_counts(r) == (1, 0, 0)
    md = render_markdown(r)
    assert "1 handelbar" in md
    assert "HANDELBAR: PLTR" in md
