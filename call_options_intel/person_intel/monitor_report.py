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


# ── markdown digest ─────────────────────────────────────────────────────────
def render_markdown(result: MonitorResult) -> str:
    if not result.alerts:
        return (f"# Person-Intel Monitor — no new signals\n\n"
                f"_{result.run_at} · mode={result.mode}_\n\n"
                f"No new EDGAR person-signals for the tracked Thiel / "
                f"Aschenbrenner vehicles.\n")

    lines: list[str] = []
    lines.append(f"# Person-Intel Monitor — {result.new_count} new signal"
                 f"{'s' if result.new_count != 1 else ''}")
    lines.append("")
    lines.append(f"_{result.run_at} · mode={result.mode} · "
                 f"{result.principal_count} directly principal-linked_")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")

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
            lines.append(f"- **Filer (fact):** {a.entity} "
                         f"(CIK {a.cik}, path {a.path_weight:.2f} · "
                         f"{a.link_confidence})")
            lines.append(f"- **Filing:** {a.filing_type} · {role} · "
                         f"filed {a.filing_date}"
                         + (f" ({a.age_days}d ago)" if a.age_days is not None else ""))
            lines.append(f"- **Subject (hypothesis until verified):** "
                         f"{_subject_str(a)}")
            lines.append(f"- **Why it matters:** {a.why_it_matters}")
            if a.falsification:
                lines.append(f"- **Falsification:** {a.falsification}")
            if a.url:
                lines.append(f"- **Source:** {a.url}")
            lines.append("")

    _block("Principal-linked signals (Thiel / Aschenbrenner vehicles)", principal)
    _block("Network / derived (second-order — adjacent smart money)", network)
    return "\n".join(lines)


# ── email (HTML) ────────────────────────────────────────────────────────────
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
        <div style="font-size:13px;color:#EBEBF5;line-height:1.5;margin-top:10px;">{a.why_it_matters}</div>
        {fals}{src}
      </td></tr>
    </table>"""


def render_email_html(result: MonitorResult, run_label: str) -> str:
    cards = "".join(_alert_card_html(a) for a in result.alerts)
    n = result.new_count
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
  <tr><td>{cards}</td></tr>
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

    if not result.alerts:
        logger.info("no new person-signals — no email sent")
        return False
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_email = os.environ.get("NOTIFY_EMAIL", gmail_user)
    if not gmail_user or not gmail_pass:
        logger.warning("email not configured (GMAIL_USER / GMAIL_APP_PASSWORD "
                       "missing) — skipping send")
        return False

    n = result.new_count
    subjects = []
    for a in result.alerts[:3]:
        tag = a.subject_ticker or _principal_badge(a)
        subjects.append(f"{tag}:{a.filing_type}")
    head = ", ".join(subjects)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"📡 Person-Intel: {n} new Thiel/Aschenbrenner signal"
                      f"{'s' if n != 1 else ''} — {head}")
    msg["From"] = f"Person-Intel Radar <{gmail_user}>"
    msg["To"] = to_email

    plain_lines = [f"Person-Intel Radar — {n} new signal(s)", run_label, ""]
    for a in result.alerts:
        plain_lines += [
            f"[{_principal_badge(a)}] {a.category}",
            f"  Filer (fact): {a.entity} (path {a.path_weight:.2f})",
            f"  Subject (hypothesis): {_subject_str(a)}",
            f"  {a.why_it_matters}",
            f"  {a.url}" if a.url else "", ""]
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
    if result.alerts:
        hist = d / "history.jsonl"
        with hist.open("a", encoding="utf-8") as fh:
            for a in result.alerts:
                fh.write(json.dumps({"logged_at": result.run_at, **a.to_dict()})
                         + "\n")
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
                       min_path_weight: float = 0.0) -> dict:
    """Run one monitor pass, write artifacts, and email ONLY on a new signal.

    Returns a summary dict (suitable for a CI step to gate on ``new_count``).
    """
    result = run_monitor(
        config=config, mode=mode, since_days=since_days, state_path=state_path,
        fixtures_dir=fixtures_dir, as_of=as_of, resolve=resolve,
        min_path_weight=min_path_weight, persist=True)

    artifacts = write_artifacts(result, artifact_dir)
    run_label = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    emailed = False
    if send_email and result.new_count > 0:
        emailed = send_person_email(result, run_label)

    return {
        "new_count": result.new_count,
        "principal_count": result.principal_count,
        "mode": result.mode,
        "emailed": emailed,
        "artifacts": artifacts,
    }
