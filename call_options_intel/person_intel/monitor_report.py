"""
monitor_report.py
=================
Rendering + notification for the person-signal monitor.

  * ``render_markdown`` / ``render_email_html`` — turn a :class:`MonitorResult`
    into a human digest that keeps **facts (the filing) separate from
    hypotheses (the derived subject / second-order lead)**.
  * ``send_person_email`` — Gmail SMTP, credentials read from the environment
    only (``GMAIL_USER`` / ``GMAIL_APP_PASSWORD`` / ``NOTIFY_EMAIL``). No secret
    is ever hard-coded or logged.
  * ``monitor_and_notify`` — the twice-daily entrypoint: run the monitor, write
    the digest artifacts, and email **only when there is a genuinely new signal**.

Every alert carries a counter-weight: a subject that could not be sourced is
shown as *pending review*, a Form D as a *private / second-order* lead, and the
falsification condition of the mapped thesis cluster (when any) is printed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from ..config_loader import AppConfig
from .monitor import MonitorResult, PersonAlert, run_monitor

logger = logging.getLogger("coi.person.monitor_report")

DISCLAIMER = ("Research/paper only — not investment advice. A filing is a fact; "
              "a derived ticker or second-order target is a hypothesis until "
              "independently verified.")

DEFAULT_ARTIFACT_DIR = Path("data/person_intel")


def _principal_badge(a: PersonAlert) -> str:
    return {"thiel": "THIEL", "aschenbrenner": "ASCHENBRENNER"}.get(
        a.principal, "NETWORK")


def _subject_str(a: PersonAlert) -> str:
    if a.subject_ticker:
        flag = " ⚠needs-review" if a.needs_human_review else ""
        return f"{a.subject_ticker} (conf {a.subject_confidence:.2f}){flag}"
    if a.is_private:
        iss = a.subject_issuer or "private issuer"
        return f"{iss} — PRIVATE (no public ticker)"
    if a.subject_issuer:
        return f"{a.subject_issuer} — subject pending review"
    return "subject pending review"


def _brief_md(lines: list, brief: dict, principal: str = "") -> None:
    """The extremely action-oriented 5-section brief — the lead of every alert."""
    if not brief:
        return
    pn = {"thiel": "Thiel", "aschenbrenner": "Aschenbrenner"}.get(principal, "Person")
    rows = [
        ("🆕 Was ist neu?", brief.get("whats_new")),
        (f"🔗 Warum {pn}-relevant?", brief.get("why_relevant")),
        ("⏱️ Warum früh?", brief.get("why_early")),
        ("🎯 Bester öffentlicher Proxy?", brief.get("best_proxy")),
        ("🛑 Warum jetzt NICHT handeln?", brief.get("why_not_now")),
    ]
    for label, val in rows:
        if val:
            lines.append(f"- **{label}** {val}")


def _triple_md(triple: dict) -> str:
    if not triple:
        return ""
    p = triple.get("person_signal", {}).get("value")
    f = triple.get("freshness", {}).get("value")
    t = triple.get("tradeability", {}).get("value")
    verdict = triple.get("label", "")
    fmt = lambda v: "–" if v is None else f"{v:.1f}"        # noqa: E731
    tail = ""
    if not triple.get("gate_pass") and triple.get("failing_axes"):
        tail = f" · weak: {', '.join(triple['failing_axes'])}"
    return (f"Person **{fmt(p)}** · Freshness **{fmt(f)}** · "
            f"Tradeability **{fmt(t)}** → _{verdict}_{tail}")


# ── markdown digest ─────────────────────────────────────────────────────────
def _ev_md(ev: dict) -> str:
    """One-line forward-EV verdict for a candidate: PASS shows expected P&L,
    FAIL shows the hard-gate reason (why it is NOT sizeable)."""
    if not ev:
        return "💰 EV-Gate: not evaluated"
    reason = (ev.get("reasons") or ["—"])[0]
    if ev.get("passed"):
        return f"💰 EV-Gate ✅ SIZEABLE — {reason}"
    return f"💰 EV-Gate ⛔ NOT sizeable — {reason}"


def _portfolio_risk_md(lines: list, result: MonitorResult) -> None:
    """Aggregate guardrails over the EV-cleared basket — only shown when there is
    something to size. A single positive-EV trade is not a robust book."""
    pr = result.portfolio_risk
    if not pr or pr.get("n", 0) == 0:
        return
    agg = pr.get("aggregates", {})
    status = "✅ within limits" if pr.get("ok") else "⛔ LIMIT BREACH"
    lines.append(f"## 🧮 Portfolio risk — {status} ({pr['n']} sizeable)")
    lines.append("")
    lines.append(f"_net delta {agg.get('net_delta')} · aggregate vega "
                 f"{agg.get('aggregate_vega')} · notional {agg.get('total_notional')}_")
    for b in pr.get("breaches", []):
        lines.append(f"- ⛔ {b}")
    lines.append("")


def _trade_candidates_md(lines: list, result: MonitorResult) -> None:
    """Top section: signals where all three axes cleared the gate AND, of those,
    which also clear the forward-EV hard gate (the only sizeable set)."""
    cands = result.trade_candidates
    if not cands:
        return
    sizeable = result.ev_trade_candidates
    lines.append(f"## ✅ TRADE-CANDIDATES — {len(sizeable)}/{len(cands)} clear the "
                 f"EV gate (sizeable)")
    lines.append("")
    lines.append("_Three axes (person · freshness · tradeability) prove the thesis "
                 "is real and early; the **forward-EV gate** then proves the actual "
                 "call pays after fills, theta and exit-rules. Only EV-cleared names "
                 "are sizeable — the rest are thesis-ready, not trade-ready._")
    lines.append("")
    for x in cands:
        tri = x.triple
        is_stmt = hasattr(x, "derived_candidates")
        tkr = (tri.get("ticker")
               or (x.derived_candidates[0] if is_stmt and x.derived_candidates
                   else "?"))
        src = ("conviction statement (Denkbewegung)" if is_stmt
               else f"{x.filing_type} (Kapitalbewegung)")
        who = _principal_badge(x)
        mark = "🟢" if (x.ev or {}).get("passed") else "🟡"
        lines.append(f"- {mark} **{tkr}** · score {tri.get('final_trade_score', 0):.1f} "
                     f"· {who} · via {src}")
        lines.append(f"    - {_triple_md(tri)}")
        lines.append(f"    - {_ev_md(x.ev)}")
        if is_stmt:
            lines.append("    - ⚠ derived/second-order (HYPOTHESIS) — corroborate "
                         "with a filing before sizing")
    lines.append("")


def _new_entities_md(lines: list, result: MonitorResult) -> None:
    """Vorfeld discoveries: filers not yet in the tracked graph (EDGAR FTS)."""
    new = [a for a in result.alerts if a.is_new_entity]
    if not new:
        return
    lines.append("## 🆕 NEW ENTITIES discovered (EDGAR full-text search)")
    lines.append("")
    lines.append("_Filers that NAME a tracked principal but are not yet in the "
                 "entity graph — verify the control link, then add the CIK._")
    lines.append("")
    for a in new:
        lines.append(f"- **{a.entity}** (CIK {a.cik}) · {a.filing_type} · "
                     f"named via “{a.matched_term}” → {a.principal.upper()}"
                     + (f" · {a.age_days}d ago" if a.age_days is not None else ""))
        if a.url:
            lines.append(f"    - {a.url}")
    lines.append("")


def _ranked_proxies_md(lines: list, result: MonitorResult) -> None:
    """Best public proxy per private theme touched this run (the real edge:
    private theme A → most liquid/undervalued/optionable listed proxy B)."""
    rp = getattr(result, "ranked_proxies", {}) or {}
    if not rp:
        return
    lines.append("## 🎯 Best public proxy per private theme")
    lines.append("")
    lines.append("_private theme A → the most liquid, undervalued, optionable "
                 "listed proxy B (edge = link × liquidity × options × valuation)._")
    lines.append("")
    for cluster in sorted(rp):
        ranked = rp[cluster]
        if not ranked:
            continue
        lines.append(f"**«{cluster}»**")
        for r in ranked:
            live = "live" if r.get("options_live") else "prior"
            lines.append(
                f"- **{r['ticker']}** · edge {r['edge_score']:.1f} "
                f"(link {r['link']:.2f} · liq {r['liquidity']:.2f} · "
                f"opt {r['options_quality']:.2f}/{live} · val {r['valuation']:.2f}) "
                f"— {r.get('rationale', '')}")
        fals = ranked[0].get("falsification", "")
        if fals:
            lines.append(f"  - _falsify:_ {fals}")
        lines.append("")


def _network_context_md(lines: list, result: MonitorResult) -> None:
    """One context line per principal that appears in this run's signals."""
    principals = []
    for a in result.alerts:
        if a.principal in ("thiel", "aschenbrenner") and a.principal not in principals:
            principals.append(a.principal)
    for s in result.statements:
        if s.principal in ("thiel", "aschenbrenner") and s.principal not in principals:
            principals.append(s.principal)
    if not principals:
        return
    try:
        from .entities import load_graph
        from .network import network_summary
        graph = load_graph(None)
    except Exception:                                # pragma: no cover
        return
    label = {"thiel": "Thiel", "aschenbrenner": "Aschenbrenner"}
    emitted = False
    for p in principals:
        line = network_summary(graph, p).context_line()
        if line:
            if not emitted:
                lines.append("**Documented network context** "
                             "(public reporting; private names are second-order, "
                             "not tradeable):")
                lines.append("")
                emitted = True
            lines.append(f"- _{label.get(p, p)} network_ — {line}")
    if emitted:
        lines.append("")


_VORFELD_LABEL = {"adv_iapd": "ADV/IAPD", "job_postings": "Hiring",
                  "domain_watch": "Website", "cert_transparency": "New-Domain (CT)"}


def _vorfeld_block_md(lines: list, vorfeld: list) -> None:
    if not vorfeld:
        return
    lines.append("## 📡 Vorfeld change-signals (ADV/IAPD · hiring · website)")
    lines.append("")
    lines.append("_Non-filing early context (change-detected). Advisory / "
                 "second-order — corroborate with a filing._")
    lines.append("")
    for v in vorfeld:
        src = _VORFELD_LABEL.get(v.source, v.source)
        princ = v.principal.upper() if v.principal else "—"
        lines.append(f"#### [{src}] {princ}"
                     + (f" · «{v.cluster}»" if v.cluster else ""))
        _brief_md(lines, v.brief, v.principal)
        if not v.brief:
            lines.append(f"- {v.headline}" + (f" — {v.detail}" if v.detail else ""))
        if v.triple:
            lines.append(f"- 3-axis: {_triple_md(v.triple)}")
        if v.url:
            lines.append(f"- {v.url}")
        lines.append("")
    lines.append("")


def _statement_block_md(lines: list, statements: list) -> None:
    if not statements:
        return
    lines.append("## Conviction / statement signals (what they SAY — derived, "
                 "second-order)")
    lines.append("")
    for s in statements:
        lines.append(f"### {s.speaker} · «{s.dominant_cluster}»")
        lines.append("")
        _brief_md(lines, s.brief, s.principal)
        lines.append(f"- **Source:** {s.source} ({s.tier}) · {s.date}"
                     + (f" ({s.age_days}d ago)" if s.age_days is not None else ""))
        lines.append(f"- **Excerpt:** {s.excerpt}")
        if s.triple:
            lines.append(f"- **3-axis:** {_triple_md(s.triple)}")
        if s.falsification:
            lines.append(f"- **Falsification:** {s.falsification}")
        if s.url:
            lines.append(f"- **Link:** {s.url}")
        lines.append("")


def render_markdown(result: MonitorResult) -> str:
    if result.total_new == 0:
        return (f"# Person-Intel Monitor — no new signals\n\n"
                f"_{result.run_at} · mode={result.mode}_\n\n"
                f"No new EDGAR filings or conviction statements for the tracked "
                f"Thiel / Aschenbrenner vehicles.\n")

    n = result.total_new
    lines: list[str] = []
    lines.append(f"# Person-Intel Monitor — {n} new signal{'s' if n != 1 else ''}")
    lines.append("")
    lines.append(f"_{result.run_at} · mode={result.mode} · "
                 f"{result.new_count} filing / {result.statement_count} statement / "
                 f"{result.vorfeld_count} vorfeld · "
                 f"{result.principal_count} directly principal-linked_")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    _trade_candidates_md(lines, result)
    _portfolio_risk_md(lines, result)
    _ranked_proxies_md(lines, result)
    _new_entities_md(lines, result)
    _network_context_md(lines, result)

    # split principal-linked (the real radar) from network/derived
    principal = [a for a in result.alerts if a.path_weight >= 0.75]
    network = [a for a in result.alerts if a.path_weight < 0.75]

    def _block(title: str, alerts: list[PersonAlert]) -> None:
        if not alerts:
            return
        lines.append(f"## {title}")
        lines.append("")
        for a in alerts:
            role = "🟢 EARLY" if a.role == "early" else "🔵 CONFIRMATION"
            lines.append(f"### {_principal_badge(a)} · {a.category}")
            lines.append("")
            if a.brief:
                _brief_md(lines, a.brief, a.principal)
            else:                                     # fallback without a brief
                lines.append(f"- **Subject (hypothesis until verified):** "
                             f"{_subject_str(a)}")
                lines.append(f"- **Why it matters:** {a.why_it_matters}")
            lines.append(f"- **Filer (fact):** {a.entity} "
                         f"(CIK {a.cik}, path {a.path_weight:.2f} · "
                         f"{a.link_confidence}) · {a.filing_type} · {role}"
                         + (f" · {a.age_days}d ago" if a.age_days is not None else ""))
            if a.triple:
                lines.append(f"- **3-axis:** {_triple_md(a.triple)}")
            if a.position_changes:
                lines.append("- **Position changes (new/add/trim/exit):**")
                for c in a.position_changes[:8]:
                    t = c.get("ticker") or f"{c.get('issuer_name', '')[:24]} "\
                        f"(CUSIP {c.get('cusip')})"
                    pc = c.get("pct_change")
                    pcs = "" if pc is None else f" {pc:+.0%}"
                    rv = " ⚠review" if c.get("needs_human_review") else ""
                    note = f" — {c['instrument_note']}" if c.get("instrument_note") else ""
                    lines.append(f"    - {c.get('action', '').upper()} "
                                 f"**{t}**{pcs}{rv}{note}")
            if a.falsification:
                lines.append(f"- **Falsification:** {a.falsification}")
            if a.url:
                lines.append(f"- **Source:** {a.url}")
            lines.append("")

    _block("Principal-linked signals (Thiel / Aschenbrenner vehicles)", principal)
    _block("Network / derived (second-order — adjacent smart money)", network)
    _statement_block_md(lines, result.statements)
    _vorfeld_block_md(lines, result.vorfeld)
    return "\n".join(lines)


# ── email (HTML) ────────────────────────────────────────────────────────────
def _brief_html(brief: dict, principal: str = "") -> str:
    """The 5-section action brief, rendered as the lead of an email card."""
    if not brief:
        return ""
    pn = {"thiel": "Thiel", "aschenbrenner": "Aschenbrenner"}.get(principal, "Person")
    rows = [
        ("🆕 Neu", brief.get("whats_new"), "#FFFFFF"),
        (f"🔗 {pn}-Bezug", brief.get("why_relevant"), "#EBEBF5"),
        ("⏱️ Früh", brief.get("why_early"), "#34C759"),
        ("🎯 Bester Proxy", brief.get("best_proxy"), "#FF9500"),
        ("🛑 Nicht jetzt", brief.get("why_not_now"), "#FF6B6B"),
    ]
    cells = []
    for label, val, col in rows:
        if not val:
            continue
        cells.append(
            f'<tr><td style="padding:4px 0;vertical-align:top;width:108px;">'
            f'<span style="font-size:11px;color:#8E8E93;font-weight:600;">{label}</span></td>'
            f'<td style="padding:4px 0 4px 8px;font-size:13px;color:{col};line-height:1.45;">'
            f'{val}</td></tr>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-top:10px;background:#0A0A0A;border-radius:10px;'
            f'padding:6px 12px;">{"".join(cells)}</table>')


def _alert_card_html(a: PersonAlert) -> str:
    accent = "#34C759" if a.role == "early" else "#007AFF"
    badge = _principal_badge(a)
    badge_color = {"THIEL": "#FF9500", "ASCHENBRENNER": "#AF52DE"}.get(
        badge, "#8E8E93")
    subj = _subject_str(a)
    review = ("background:#FF3B3011;border-left:3px solid #FF9500;"
              if a.needs_human_review or a.is_private else
              "background:#34C75911;border-left:3px solid #34C759;")
    fals = (f'<div style="margin-top:8px;font-size:12px;color:#FF6B6B;">'
            f'<strong>Falsification:</strong> {a.falsification}</div>'
            if a.falsification else "")
    src = (f'<div style="margin-top:8px;font-size:11px;color:#636366;">'
           f'<a href="{a.url}" style="color:#636366;">SEC source ↗</a></div>'
           if a.url else "")
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr><td style="background:#1C1C1E;border-radius:14px;padding:18px 20px;">
        <table width="100%"><tr>
          <td>
            <span style="display:inline-block;background:{badge_color}22;border:1px solid {badge_color};
                  border-radius:6px;padding:3px 9px;font-size:10px;font-weight:700;
                  color:{badge_color};letter-spacing:.5px;">{badge}</span>
            <span style="display:inline-block;background:{accent}22;border:1px solid {accent};
                  border-radius:6px;padding:3px 9px;font-size:10px;font-weight:700;
                  color:{accent};letter-spacing:.5px;margin-left:6px;">
                  {'EARLY' if a.role == 'early' else 'CONFIRMATION'}</span>
          </td>
          <td style="text-align:right;font-size:11px;color:#636366;">{a.filing_date}</td>
        </tr></table>
        <div style="font-size:17px;font-weight:700;color:#FFFFFF;margin-top:10px;">{a.category}</div>
        <div style="font-size:13px;color:#8E8E93;margin-top:3px;">
          Filer (fact): {a.entity} · path {a.path_weight:.2f} ({a.link_confidence})</div>
        <div style="{review}border-radius:0 8px 8px 0;padding:8px 12px;margin-top:10px;
              font-size:13px;color:#EBEBF5;">
          <strong>Subject:</strong> {subj}<br>
          <span style="font-size:11px;color:#8E8E93;">
            (filing = fact · subject = hypothesis until verified)</span></div>
        {_brief_html(a.brief, a.principal)}
        {_triple_html(a.triple)}{_position_changes_html(a)}{fals}{src}
      </td></tr>
    </table>"""


def _position_changes_html(a: PersonAlert) -> str:
    if not a.position_changes:
        return ""
    rows = []
    for c in a.position_changes[:8]:
        t = c.get("ticker") or f"CUSIP {c.get('cusip')}"
        pc = c.get("pct_change")
        pcs = "" if pc is None else f" {pc:+.0%}"
        color = {"new": "#34C759", "add": "#34C759",
                 "trim": "#FF9500", "exit": "#FF3B30"}.get(c.get("action"), "#8E8E93")
        rv = " ⚠" if c.get("needs_human_review") else ""
        rows.append(f'<span style="color:{color};font-size:12px;">'
                    f'{c.get("action", "").upper()} <strong>{t}</strong>{pcs}{rv}</span>')
    inner = " &nbsp;·&nbsp; ".join(rows)
    return (f'<div style="margin-top:10px;padding:8px 12px;background:#0A0A0A;'
            f'border-radius:8px;font-size:12px;color:#8E8E93;">'
            f'<strong style="color:#EBEBF5;">Position changes:</strong> {inner}</div>')


def _statement_card_html(s) -> str:
    badge = {"thiel": "THIEL", "aschenbrenner": "ASCHENBRENNER"}.get(
        s.principal, "NETWORK")
    badge_color = {"THIEL": "#FF9500", "ASCHENBRENNER": "#AF52DE"}.get(
        badge, "#8E8E93")
    cands = ", ".join(s.derived_candidates) or "—"
    fals = (f'<div style="margin-top:8px;font-size:12px;color:#FF6B6B;">'
            f'<strong>Falsification:</strong> {s.falsification}</div>'
            if s.falsification else "")
    src = (f'<div style="margin-top:8px;font-size:11px;color:#636366;">'
           f'<a href="{s.url}" style="color:#636366;">{s.source} ↗</a></div>'
           if s.url else "")
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      <tr><td style="background:#1C1C1E;border-radius:14px;padding:18px 20px;">
        <table width="100%"><tr>
          <td>
            <span style="display:inline-block;background:{badge_color}22;border:1px solid {badge_color};
                  border-radius:6px;padding:3px 9px;font-size:10px;font-weight:700;
                  color:{badge_color};letter-spacing:.5px;">{badge}</span>
            <span style="display:inline-block;background:#5856D622;border:1px solid #5856D6;
                  border-radius:6px;padding:3px 9px;font-size:10px;font-weight:700;
                  color:#5856D6;letter-spacing:.5px;margin-left:6px;">CONVICTION</span>
          </td>
          <td style="text-align:right;font-size:11px;color:#636366;">{s.date} · {s.tier}</td>
        </tr></table>
        <div style="font-size:15px;font-weight:700;color:#FFFFFF;margin-top:10px;">{s.headline}</div>
        <div style="font-size:13px;color:#8E8E93;margin-top:6px;line-height:1.5;">{s.excerpt}</div>
        <div style="background:#FF950011;border-left:3px solid #FF9500;border-radius:0 8px 8px 0;
              padding:8px 12px;margin-top:10px;font-size:13px;color:#EBEBF5;">
          <strong>Derived candidates:</strong> {cands}<br>
          <span style="font-size:11px;color:#8E8E93;">
            (HYPOTHESIS / watchlist — second-order, not a confirmed investment)</span></div>
        {_brief_html(s.brief, s.principal)}
        {_triple_html(s.triple)}{fals}{src}
      </td></tr>
    </table>"""


def _triple_html(triple: dict) -> str:
    if not triple:
        return ""
    def chip(label, comp, ok):
        v = comp.get("value")
        vs = "–" if v is None else f"{v:.1f}"
        col = "#34C759" if ok else "#FF9500"
        return (f'<span style="display:inline-block;margin-right:6px;font-size:11px;'
                f'color:{col};">{label} {vs}</span>')
    p = triple.get("person_signal", {})
    f = triple.get("freshness", {})
    t = triple.get("tradeability", {})
    fa = set(triple.get("failing_axes", []))
    verdict = triple.get("label", "")
    vcol = "#34C759" if triple.get("gate_pass") else "#8E8E93"
    return (f'<div style="margin-top:8px;font-size:11px;">'
            f'{chip("Person", p, "person_signal" not in fa)}'
            f'{chip("Fresh", f, "freshness" not in fa)}'
            f'{chip("Trade", t, "tradeability" not in fa)}'
            f'<span style="color:{vcol};font-weight:700;">→ {verdict}</span></div>')


def _trade_candidates_banner_html(result: MonitorResult) -> str:
    cands = result.trade_candidates
    if not cands:
        return ""
    sizeable = result.ev_trade_candidates
    rows = []
    for x in cands:
        tri = x.triple
        is_stmt = hasattr(x, "derived_candidates")
        tkr = (tri.get("ticker")
               or (x.derived_candidates[0] if is_stmt and x.derived_candidates
                   else "?"))
        src = "statement" if is_stmt else x.filing_type
        ev = x.ev or {}
        passed = ev.get("passed")
        ev_reason = (ev.get("reasons") or ["EV not evaluated"])[0]
        ev_col = "#34C759" if passed else "#FF9500"
        ev_tag = "EV ✅ sizeable" if passed else "EV ⛔ not sizeable"
        rows.append(
            f'<div style="font-size:14px;color:#EBEBF5;margin-top:8px;">'
            f'<strong style="color:#34C759;">{tkr}</strong> '
            f'· score {tri.get("final_trade_score", 0):.1f} '
            f'<span style="color:#8E8E93;font-size:12px;">'
            f'({_principal_badge(x)} · via {src})</span></div>'
            f'<div style="font-size:11px;color:{ev_col};margin-top:2px;">'
            f'{ev_tag} — {ev_reason}</div>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-bottom:16px;"><tr><td style="background:#0E2A14;'
            f'border:1px solid #34C759;border-radius:14px;padding:16px 20px;">'
            f'<div style="font-size:12px;color:#34C759;letter-spacing:1px;'
            f'text-transform:uppercase;font-weight:700;">✅ Trade-Candidates — '
            f'{len(sizeable)}/{len(cands)} clear the EV gate</div>{"".join(rows)}'
            f'<div style="font-size:11px;color:#8E8E93;margin-top:8px;">'
            f'Three axes prove the thesis is real + early; the forward-EV gate '
            f'proves the call pays after costs. Only EV-cleared names are sizeable.'
            f'</div></td></tr></table>')


def _vorfeld_section_html(vorfeld: list) -> str:
    if not vorfeld:
        return ""
    rows = []
    for v in vorfeld:
        src = _VORFELD_LABEL.get(v.source, v.source)
        cl = f' · «{v.cluster}»' if v.cluster else ""
        rows.append(
            f'<div style="font-size:13px;color:#EBEBF5;margin-top:8px;">'
            f'<span style="color:#5856D6;font-weight:700;">[{src}]</span> '
            f'{v.headline}{cl}</div>' + _brief_html(v.brief, v.principal))
    return (f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-bottom:16px;"><tr><td style="background:#1C1C1E;'
            f'border-radius:14px;padding:16px 20px;">'
            f'<div style="font-size:12px;color:#5856D6;letter-spacing:1px;'
            f'text-transform:uppercase;font-weight:700;">📡 Vorfeld — '
            f'ADV/IAPD · hiring · website (change-detected, advisory)</div>'
            f'{"".join(rows)}</td></tr></table>')


def render_email_html(result: MonitorResult, run_label: str) -> str:
    banner = _trade_candidates_banner_html(result)
    cards = "".join(_alert_card_html(a) for a in result.alerts)
    cards += "".join(_statement_card_html(s) for s in result.statements)
    cards += _vorfeld_section_html(result.vorfeld)
    n = result.total_new
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#000;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#000;padding:20px 0;">
<tr><td align="center"><table width="600" style="max-width:600px;width:100%;">
  <tr><td style="padding:0 0 20px 0;">
    <div style="font-size:12px;color:#636366;letter-spacing:2px;text-transform:uppercase;">
      PERSON-INTEL RADAR · THIEL / ASCHENBRENNER</div>
    <div style="font-size:26px;font-weight:700;color:#FFFFFF;letter-spacing:-.5px;margin-top:4px;">
      {n} new investment signal{'s' if n != 1 else ''}</div>
    <div style="font-size:13px;color:#636366;margin-top:4px;">
      {run_label} · {result.principal_count} principal-linked</div>
  </td></tr>
  <tr><td>{banner}{cards}</td></tr>
  <tr><td style="padding:18px 0 0 0;border-top:1px solid #1C1C1E;">
    <div style="font-size:11px;color:#3A3A3C;text-align:center;line-height:1.6;">
      Sources: SEC EDGAR (Form 4 / SC 13D-G / Form D / 13F-HR). A filing is a fact;<br>
      a derived ticker or second-order target is a hypothesis until verified.<br><br>
      <strong style="color:#FF3B30;">⚠ Research/paper only — not investment advice.</strong>
    </div>
  </td></tr>
</table></td></tr></table></body></html>"""


def send_person_email(result: MonitorResult, run_label: str) -> bool:
    """Send the digest via Gmail SMTP. Credentials come from the environment
    only. Returns False (without raising) if unconfigured or empty."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if result.total_new == 0:
        logger.info("no new person-signals — no email sent")
        return False
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_email = os.environ.get("NOTIFY_EMAIL", gmail_user)
    if not gmail_user or not gmail_pass:
        logger.warning("email not configured (GMAIL_USER / GMAIL_APP_PASSWORD "
                       "missing) — skipping send")
        return False

    n = result.total_new
    subjects = []
    for a in result.alerts[:3]:
        tag = a.subject_ticker or _principal_badge(a)
        subjects.append(f"{tag}:{a.filing_type}")
    for s in result.statements[:2]:
        subjects.append(f"{_principal_badge(s)}:«{s.dominant_cluster}»")
    head = ", ".join(subjects[:4])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"📡 Person-Intel: {n} new Thiel/Aschenbrenner signal"
                      f"{'s' if n != 1 else ''} — {head}")
    msg["From"] = f"Person-Intel Radar <{gmail_user}>"
    msg["To"] = to_email

    plain_lines = [f"Person-Intel Radar — {n} new signal(s) "
                   f"({result.new_count} filing / {result.statement_count} statement)",
                   run_label, ""]
    for a in result.alerts:
        plain_lines += [
            f"[{_principal_badge(a)}] {a.category}",
            f"  Filer (fact): {a.entity} (path {a.path_weight:.2f})",
            f"  Subject (hypothesis): {_subject_str(a)}",
            f"  {a.why_it_matters}",
            f"  {a.url}" if a.url else "", ""]
    for s in result.statements:
        plain_lines += [
            f"[{_principal_badge(s)}] CONVICTION «{s.dominant_cluster}»",
            f"  {s.headline}",
            f"  Derived (hypothesis): {', '.join(s.derived_candidates) or '—'}",
            f"  {s.url}" if s.url else "", ""]
    for v in result.vorfeld:
        plain_lines += [
            f"[VORFELD/{v.source}] {v.headline}",
            f"  {v.detail}" if v.detail else "",
            f"  {v.url}" if v.url else "", ""]
    msg.attach(MIMEText("\n".join(plain_lines), "plain"))
    msg.attach(MIMEText(render_email_html(result, run_label), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_email, msg.as_string())
        logger.info("person-intel email sent: %d signals to %s", n, to_email)
        return True
    except Exception as exc:
        logger.error("person-intel email send failed: %s", exc)
        return False


# ── artifacts + history ─────────────────────────────────────────────────────
def write_artifacts(result: MonitorResult,
                    artifact_dir: str | Path | None = None) -> dict:
    """Write the digest (md + json) and append new alerts to the history log."""
    d = Path(artifact_dir) if artifact_dir else DEFAULT_ARTIFACT_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "last_digest.md").write_text(render_markdown(result), encoding="utf-8")
    (d / "last_run.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    # append-only history (the historical signal archive)
    if result.alerts or result.statements or result.vorfeld:
        hist = d / "history.jsonl"
        with hist.open("a", encoding="utf-8") as fh:
            for a in result.alerts:
                fh.write(json.dumps({"logged_at": result.run_at,
                                     "type": "filing", **a.to_dict()}) + "\n")
            for s in result.statements:
                fh.write(json.dumps({"logged_at": result.run_at,
                                     "type": "statement", **s.to_dict()}) + "\n")
            for v in result.vorfeld:
                fh.write(json.dumps({"logged_at": result.run_at,
                                     "type": "vorfeld", **v.to_dict()}) + "\n")
    return {"digest_md": str(d / "last_digest.md"),
            "last_run_json": str(d / "last_run.json")}


# ── twice-daily entrypoint ──────────────────────────────────────────────────
def monitor_and_notify(config: Optional[AppConfig] = None, *,
                       mode: Optional[str] = None, since_days: int = 45,
                       state_path: str | Path | None = None,
                       fixtures_dir: str | Path | None = None,
                       artifact_dir: str | Path | None = None,
                       as_of: Optional[date] = None, resolve: bool = True,
                       send_email: bool = False,
                       include_statements: bool = True,
                       min_path_weight: float = 0.0,
                       record_outcomes: bool = False,
                       outcome_store_path: str | Path | None = None,
                       iv_store_path: str | Path | None = None) -> dict:
    """Run one monitor pass, write artifacts, email ONLY on a new signal, and
    (optionally) RECORD every new signal — incl. rejects — to the outcome store.

    ``iv_store_path`` feeds a warmed IV-history store into the forward-EV gate so
    a live candidate can actually clear it (no IV history ⇒ EV gate hard-blocks).

    Returns a summary dict (suitable for a CI step to gate on ``total_new``).
    """
    cfg = config or AppConfig()
    run_mode = mode or cfg.mode
    fixtures = fixtures_dir
    result = run_monitor(
        config=cfg, mode=run_mode, since_days=since_days, state_path=state_path,
        fixtures_dir=fixtures_dir, as_of=as_of, resolve=resolve,
        min_path_weight=min_path_weight, include_statements=include_statements,
        iv_store_path=iv_store_path, persist=True)

    artifacts = write_artifacts(result, artifact_dir)
    run_label = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    emailed = False
    if send_email and result.total_new > 0:
        emailed = send_person_email(result, run_label)

    recorded = 0
    if record_outcomes and result.total_new > 0:
        try:
            from ..pipeline import DEFAULT_FIXTURES
            from .outcome_recorder import make_pipeline_snapshot_fn, record_run, DEFAULT_STORE
            from .proxy_map import load_proxy_map
            fx = fixtures or DEFAULT_FIXTURES
            snap_fn = make_pipeline_snapshot_fn(cfg, run_mode, fx)
            recorded = record_run(
                result, load_proxy_map(cfg.config_dir),
                store_path=(outcome_store_path or DEFAULT_STORE),
                snapshot_fn=snap_fn, as_of=as_of)
        except Exception as exc:                        # pragma: no cover
            logger.warning("outcome recording failed: %s", exc)

    return {
        "new_count": result.new_count,
        "statement_count": result.statement_count,
        "vorfeld_count": result.vorfeld_count,
        "total_new": result.total_new,
        "principal_count": result.principal_count,
        "mode": result.mode,
        "emailed": emailed,
        "outcomes_recorded": recorded,
        "artifacts": artifacts,
    }
