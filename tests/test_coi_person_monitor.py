"""Tests for the live, dedup'd person-signal monitor (offline-injected).

These cover the contract that matters for the radar:
  * tracked-manager filings become typed person-alerts;
  * the dedup store makes a filing fire exactly ONCE (email-only-on-new);
  * a filing is a fact, but its subject ticker is only asserted when sourced
    (PLTR via CUSIP) and otherwise stays needs_human_review (no guessing);
  * principal-linkage weights confirmed Thiel/Aschenbrenner vehicles above noise;
  * the digest separates facts from hypotheses.
"""
from datetime import date

import pytest

from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.edgar_fast import (
    EdgarFastClient, FixtureFetcher,
)
from call_options_intel.person_intel.entities import load_graph
from call_options_intel.person_intel.cusip_map import load_mapper
from call_options_intel.person_intel.proxy_map import load_proxy_map
from call_options_intel.person_intel.filings import FilingType, SignalRole
from call_options_intel.person_intel.monitor import (
    PersonMonitor, SeenStore, run_monitor,
)
from call_options_intel.person_intel.monitor_report import (
    monitor_and_notify, render_markdown, render_email_html, send_person_email,
)

AS_OF = date(2026, 6, 27)


def _monitor(cfg: AppConfig) -> PersonMonitor:
    client = EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))
    return PersonMonitor(
        client=client, graph=load_graph(cfg.config_dir),
        mapper=load_mapper(cfg.config_dir),
        proxy_map=load_proxy_map(cfg.config_dir),
        managers=cfg.investors.get("managers", []) or [])


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


@pytest.fixture
def store(tmp_path) -> SeenStore:
    return SeenStore.load(tmp_path / "seen.json")


# ── collection + typing ─────────────────────────────────────────────────────
def test_collects_relevant_filings_only(cfg, store):
    alerts = _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)
    fts = {a.filing_type for a in alerts}
    # the 8-K in the Thiel fixture must NOT surface (context noise)
    assert FilingType.UNKNOWN.value not in fts
    assert "8-K" not in fts
    # the tracked person-forms must surface
    assert {"Form 4", "SC 13D/A", "SC 13G", "13F-HR"} <= fts


def test_roles_are_honest(cfg, store):
    alerts = {a.filing_type: a for a in
              _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)}
    assert alerts["Form 4"].role == SignalRole.EARLY.value
    assert alerts["SC 13D/A"].role == SignalRole.EARLY.value
    assert alerts["13F-HR"].role == SignalRole.CONFIRMATION.value


# ── subject resolution: sourced fact vs un-guessed hypothesis ───────────────
def test_13d_subject_resolves_to_pltr_with_confidence(cfg, store):
    alerts = {a.filing_type: a for a in
              _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)}
    a = alerts["SC 13D/A"]
    assert a.subject_ticker == "PLTR"
    assert a.subject_confidence >= 0.9
    assert a.needs_human_review is False
    assert a.is_fact is True               # the filing itself is a fact
    assert a.falsification                  # PLTR maps to a thesis cluster


def test_unsourced_subject_stays_needs_review(cfg, store):
    """Form 4 fixture has no resolvable CUSIP doc -> never guesses a ticker."""
    alerts = {a.filing_type: a for a in
              _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)}
    a = alerts["Form 4"]
    assert a.subject_ticker is None
    assert a.needs_human_review is True


def test_13f_is_event_level_not_single_name(cfg, store):
    alerts = {a.filing_type: a for a in
              _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)}
    a = alerts["13F-HR"]
    assert a.subject_ticker is None
    assert a.signal_kind == "quarterly_13f"


# ── principal linkage ───────────────────────────────────────────────────────
def test_thiel_filings_are_principal_linked(cfg, store):
    alerts = _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)
    assert alerts, "expected Thiel fixture filings"
    for a in alerts:
        assert a.principal == "thiel"
        assert a.path_weight == 1.0
        assert a.link_confidence == "principal"


def test_min_weight_filters_network(cfg, store):
    # a high floor keeps only confirmed-vehicle signals; the Thiel ones survive
    alerts = _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF,
                                      min_path_weight=0.75)
    assert all(a.path_weight >= 0.75 for a in alerts)
    assert alerts


# ── dedup: fire exactly once (email-only-on-new) ────────────────────────────
def test_dedup_fires_each_filing_once(cfg, tmp_path):
    sp = tmp_path / "seen.json"
    mon = _monitor(cfg)
    s1 = SeenStore.load(sp)
    first = mon.new_alerts(s1, since_days=120, as_of=AS_OF)
    assert len(first) >= 4
    for a in first:                        # persist what we reported
        s1.seen[a.accession] = {"first_seen": AS_OF.isoformat()}
    s1.save()
    # a fresh load sees them as already-known -> zero new
    s2 = SeenStore.load(sp)
    second = mon.new_alerts(s2, since_days=120, as_of=AS_OF)
    assert second == []


def test_run_monitor_persists_and_dedups(cfg, tmp_path):
    sp = tmp_path / "seen.json"
    r1 = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                     state_path=sp, persist=True)
    assert r1.new_count >= 4
    assert r1.principal_count >= 4
    r2 = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                     state_path=sp, persist=True)
    assert r2.new_count == 0


def test_dry_run_does_not_persist(cfg, tmp_path):
    sp = tmp_path / "seen.json"
    run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                state_path=sp, persist=False)
    assert not sp.exists()


# ── rendering + notify ──────────────────────────────────────────────────────
def test_markdown_separates_fact_from_hypothesis(cfg, store):
    result_alerts = _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)
    from call_options_intel.person_intel.monitor import MonitorResult
    result = MonitorResult(new_count=len(result_alerts), alerts=result_alerts,
                           mode="offline", run_at="t", principal_count=4)
    md = render_markdown(result)
    assert "Filer (fact)" in md
    assert "hypothesis until verified" in md
    assert "PLTR" in md
    html = render_email_html(result, "now")
    assert "PLTR" in html and "Research/paper only" in html


def test_13f_is_declassed_never_a_trade_candidate():
    # a quarterly 13F is universe/thesis-grade — even if (hypothetically) all
    # three axes cleared, it must NEVER appear as a trade-candidate.
    from call_options_intel.person_intel.monitor import MonitorResult, PersonAlert
    passing = {"gate_pass": True, "final_trade_score": 9.0, "label": "TRADE-CANDIDATE"}
    a13f = PersonAlert(
        entity="Founders Fund", entity_id="ff", cik="1", filing_type="13F-HR",
        form_raw="13F-HR", role="confirmation", category="Quarterly 13F",
        signal_kind="quarterly_13f", filing_date="2026-06-01", age_days=5,
        accession="x", url="", principal="thiel", path_weight=1.0,
        link_confidence="principal", subject_ticker="NVDA", triple=passing)
    a4 = PersonAlert(
        entity="Thiel", entity_id="t", cik="2", filing_type="Form 4",
        form_raw="4", role="early", category="Insider", signal_kind="insider_trade",
        filing_date="2026-06-20", age_days=1, accession="y", url="",
        principal="thiel", path_weight=1.0, link_confidence="principal",
        subject_ticker="PLTR", triple=passing)
    r = MonitorResult(new_count=2, alerts=[a13f, a4], mode="offline", run_at="t",
                      principal_count=2)
    kinds = {x.signal_kind for x in r.trade_candidates}
    assert "quarterly_13f" not in kinds          # declassed
    assert "insider_trade" in kinds              # the early Form 4 still qualifies


def test_email_skipped_when_unconfigured(cfg, store, monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    from call_options_intel.person_intel.monitor import MonitorResult
    alerts = _monitor(cfg).new_alerts(store, since_days=120, as_of=AS_OF)
    result = MonitorResult(new_count=len(alerts), alerts=alerts,
                           mode="offline", run_at="t", principal_count=4)
    assert send_person_email(result, "now") is False   # no creds -> no send


def test_notify_writes_artifacts_no_email(cfg, tmp_path):
    out = monitor_and_notify(
        config=cfg, mode="offline", since_days=120, as_of=AS_OF,
        state_path=tmp_path / "seen.json", artifact_dir=tmp_path / "art",
        send_email=False)
    assert out["new_count"] >= 4
    assert out["emailed"] is False
    assert (tmp_path / "art" / "last_digest.md").exists()
    assert (tmp_path / "art" / "history.jsonl").exists()


def test_triple_score_attached_and_gates(cfg, tmp_path):
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", persist=False)
    # every alert carries the three-axis score + gate verdict
    for a in r.alerts:
        assert {"person_signal", "freshness", "tradeability", "gate_pass",
                "final_trade_score"} <= set(a.triple)
    # the Thiel SC 13D/A -> PLTR (fresh, liquid in fixtures) clears all 3 axes
    pltr = next(a for a in r.alerts if a.subject_ticker == "PLTR")
    assert pltr.triple["gate_pass"] is True
    # an unresolved Form 4 (no ticker) can never be tradeable -> gate fails
    form4 = next(a for a in r.alerts if a.filing_type == "Form 4")
    assert form4.triple["gate_pass"] is False
    assert "tradeability" in form4.triple["failing_axes"]
    # trade_candidates only contains gate-passers
    assert all(x.triple["gate_pass"] for x in r.trade_candidates)
    assert any(getattr(x, "subject_ticker", None) == "PLTR"
               for x in r.trade_candidates)


def test_no_tradeability_means_no_trade_candidates(cfg, tmp_path):
    # without the options/market pipeline, tradeability cannot be confirmed ->
    # nothing clears the gate (honest: no liquid call verified)
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", score_tradeability=False,
                    persist=False)
    assert r.trade_candidates == []
    for a in r.alerts:
        assert a.triple["tradeability"]["value"] in (0.0, None) or \
               a.triple["tradeability"]["value"] <= 4.0


def test_ev_gate_is_attached_and_blocks_without_iv_history(cfg, tmp_path):
    # The forward-EV hard gate runs on every triple-gate passer. Offline there is
    # no IV history; with allow_rv_proxy_prewarmup (production config) richness
    # is judged via the conservative realised-vol proxy instead of a blanket
    # block — so the candidate is now stopped by the NEXT honest gate (the
    # fixture tickers have earnings inside the holding window). Nothing is
    # sizeable, and nothing is blocked for the un-judgeable-richness dead-end.
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", persist=False)
    assert r.trade_candidates                       # triple gate still passes some
    for x in r.trade_candidates:
        assert x.ev.get("passed") is False
        reasons = x.ev.get("reasons", [])
        assert any(("earnings" in reason) or ("pre-warmup proxy" in reason)
                   for reason in reasons)
        # the blind "cannot judge richness" dead-end is gone (proxy basis exists)
        assert not any("IV-rank unknown" in reason for reason in reasons)
    assert r.ev_trade_candidates == []              # nothing is sizeable offline


def test_score_ev_false_skips_the_gate(cfg, tmp_path):
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", score_ev=False,
                    persist=False)
    assert all(x.ev == {} for x in r.trade_candidates)
    assert r.ev_trade_candidates == []


def test_iv_store_clears_the_no_iv_history_block(cfg, tmp_path):
    # feeding a WARMED IV-history store removes the "no IV history" hard-block,
    # so the EV gate can proceed to the actual expectancy test — the wiring that
    # lets a live candidate ever clear the gate.
    from datetime import date as _date, timedelta
    from call_options_intel.person_intel.iv_history import IVHistoryStore
    ivp = tmp_path / "iv.jsonl"
    store = IVHistoryStore(ivp)
    for t in ("PLTR", "VST"):
        for i in range(25):                          # >= warmup_min_obs (20)
            store.record(t, 0.40 + 0.01 * (i % 5),
                         as_of=_date(2026, 1, 1) + timedelta(days=i))
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", iv_store_path=ivp,
                    persist=False)
    assert r.trade_candidates
    for x in r.trade_candidates:
        reasons = x.ev.get("reasons", [])
        assert not any("IV history" in reason for reason in reasons)


def test_portfolio_risk_property_present_and_empty_is_ok(cfg, tmp_path):
    # offline nothing clears the EV gate → no sizeable basket → portfolio ok/empty
    r = run_monitor(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                    state_path=tmp_path / "seen.json", persist=False)
    pr = r.portfolio_risk
    assert pr["ok"] is True
    assert pr["n"] == 0
    assert pr["breaches"] == []


def test_no_new_signals_renders_clean(cfg, tmp_path):
    sp = tmp_path / "seen.json"
    monitor_and_notify(config=cfg, mode="offline", since_days=120, as_of=AS_OF,
                       state_path=sp, artifact_dir=tmp_path / "art",
                       send_email=False)
    out = monitor_and_notify(config=cfg, mode="offline", since_days=120,
                             as_of=AS_OF, state_path=sp,
                             artifact_dir=tmp_path / "art", send_email=False)
    assert out["new_count"] == 0
    assert out["emailed"] is False
    digest = (tmp_path / "art" / "last_digest.md").read_text()
    assert "no new signals" in digest
