"""Tests for the 5-section action brief (what's new · relevant · early · proxy · not-now)."""
from datetime import date
from types import SimpleNamespace

from call_options_intel.person_intel import action_brief as ab
from call_options_intel.person_intel.monitor import run_monitor
from call_options_intel.person_intel.monitor_report import (
    render_markdown, render_email_html,
)

AS_OF = date(2026, 6, 27)
_KEYS = ("whats_new", "why_relevant", "why_early", "best_proxy", "why_not_now")


def _alert(**kw):
    base = dict(
        principal="thiel", path_weight=1.0, is_new_entity=False, matched_term="",
        subject_ticker="PLTR", subject_issuer=None, needs_human_review=False,
        is_private=False, category="Change to >5% activist stake (SC 13D/A)",
        filing_type="SC 13D/A", role="early", discovered_via="cik_feed",
        age_days=4, entity="THIEL PETER", cik="0001211060", position_changes=[],
        triple={"label": "TRADE-CANDIDATE", "gate_pass": True, "failing_axes": [],
                "freshness": {"value": 6.9}, "tradeability": {"value": 7.2}})
    base.update(kw)
    return SimpleNamespace(**base)


def _liquid_snap(iv=0.35, spread=0.03):
    return {"spot": 200.0, "strike": 220.0, "entry_premium": 11.0,
            "entry_delta": 0.42, "iv": iv, "dte": 90, "spread_pct": spread}


# ── filing briefs ────────────────────────────────────────────────────────────
def test_filing_brief_has_five_sections():
    b = ab.filing_brief(_alert(), snapshot=_liquid_snap(), ranked=[], cluster="defense_ai")
    assert all(k in b for k in _KEYS)
    assert "DIRECT" in b["why_relevant"]
    assert "EARLY" in b["why_early"] and "13F" in b["why_early"]


def test_liquid_low_iv_suggests_long_call():
    b = ab.filing_brief(_alert(), snapshot=_liquid_snap(iv=0.30), ranked=[],
                        cluster="defense_ai")
    assert "liquid long CALL" in b["best_proxy"]


def test_rich_iv_suggests_spread_and_warns():
    b = ab.filing_brief(_alert(), snapshot=_liquid_snap(iv=0.75), ranked=[],
                        cluster="defense_ai")
    assert "SPREAD" in b["best_proxy"]
    assert "IV rich" in b["why_not_now"]


def test_wide_spread_flagged_not_now():
    b = ab.filing_brief(_alert(), snapshot=_liquid_snap(spread=0.20), ranked=[],
                        cluster="defense_ai")
    assert "spread wide" in b["why_not_now"]


def test_unresolved_no_ticker_is_watchlist():
    a = _alert(subject_ticker=None, needs_human_review=True,
               triple={"label": "NO-TRADE", "gate_pass": False,
                       "failing_axes": ["tradeability"],
                       "freshness": {"value": 7.7}, "tradeability": {"value": 0.0}})
    b = ab.filing_brief(a, snapshot=None, ranked=[], cluster="")
    assert "WATCHLIST" in b["best_proxy"]
    assert "GATE NOT PASSED" in b["why_not_now"]
    assert "needs_human_review" in b["why_not_now"]


def test_new_entity_relevance_is_indirect():
    a = _alert(is_new_entity=True, subject_ticker=None, needs_human_review=True,
               entity="FF VENTURES SPV XII LLC", path_weight=0.5,
               category="NEW ENTITY · Private placement (Form D)")
    b = ab.filing_brief(a, snapshot=None, ranked=[], cluster="")
    assert "NEW ENTITY" in b["whats_new"]
    assert "UNCONFIRMED" in b["why_relevant"]


def test_confirmation_13f_not_early():
    a = _alert(role="confirmation", filing_type="13F-HR",
               category="Quarterly 13F holdings filed (13F-HR)")
    b = ab.filing_brief(a, snapshot=None, ranked=[], cluster="")
    assert "CONFIRMATION" in b["why_early"] and "not early" in b["why_early"]


# ── statement / vorfeld briefs ───────────────────────────────────────────────
def test_statement_official_is_first_party():
    s = SimpleNamespace(principal="aschenbrenner", speaker="Leopold Aschenbrenner",
                        tier="official", dominant_cluster="power_grid", age_days=3,
                        derived_candidates=["VST", "CEG"],
                        triple={"label": "WATCH", "gate_pass": False,
                                "failing_axes": [], "tradeability": {"value": 5.0}})
    b = ab.statement_brief(s, snapshot=_liquid_snap(), ranked=[])
    assert "FIRST-PARTY" in b["why_relevant"]
    assert "second-order/derived" in b["why_not_now"]


def test_statement_media_is_secondary():
    s = SimpleNamespace(principal="thiel", speaker="Peter Thiel", tier="media",
                        dominant_cluster="defense_ai", age_days=2,
                        derived_candidates=["PLTR"], triple={})
    b = ab.statement_brief(s, snapshot=None, ranked=[])
    assert "MEDIA" in b["why_relevant"]


def test_vorfeld_cert_is_very_early():
    v = SimpleNamespace(principal="thiel", entity="ff-x.foundersfund.com",
                        source="cert_transparency", kind="new_domain",
                        cluster="compute", detail="matches foundersfund",
                        headline="NEW domain", age_days=3, triple={})
    b = ab.vorfeld_brief(v, snapshot=None, ranked=[])
    assert "very early" in b["why_early"]
    assert "context-grade" in b["why_not_now"]


# ── integration: attached + rendered ─────────────────────────────────────────
def test_monitor_attaches_briefs(tmp_path):
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=120,
                      state_path=tmp_path / "seen.json", persist=False)
    for a in res.alerts:
        assert all(k in a.brief for k in _KEYS)
    for s in res.statements:
        assert all(k in s.brief for k in _KEYS)


def test_digest_and_email_show_five_sections(tmp_path):
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=120,
                      state_path=tmp_path / "seen.json", persist=False)
    md = render_markdown(res)
    assert "Was ist neu?" in md and "Warum jetzt NICHT handeln?" in md
    assert "Bester öffentlicher Proxy?" in md
    html = render_email_html(res, "now")
    assert "Bester Proxy" in html and "Nicht jetzt" in html
