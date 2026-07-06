"""Render the actionable alert email."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..utils import get_logger
from .format import FILTERS

log = get_logger("render.alert")
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "email_templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default=True),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    return env


def _latency_str(detected_at: str | None, source_date: str | None) -> str:
    """Return human-readable latency between source_date and detected_at."""
    if not detected_at:
        return ""
    try:
        det = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        minutes = int((now - det).total_seconds() / 60)
        if minutes < 60:
            return f"~{minutes} min ago"
        hours = minutes // 60
        return f"~{hours}h ago"
    except Exception:
        return ""


def _enrich_events(events: list[dict]) -> list[dict]:
    """Add latency_str to each event for display in the template."""
    for e in events:
        e["latency_str"] = _latency_str(e.get("detected_at"), e.get("source_date"))
    return events


def render(cfg: Config, new_events: list[dict], model: dict | None = None, tldr: dict | None = None, review_events: list[dict] | None = None) -> str:
    meta = {
        "person": cfg.person,
        "manager": cfg.primary_name,
        "subject_prefix": cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert"),
    }
    sec_events = _enrich_events([e for e in new_events if e.get("signal_type") in (
        "13f_position", "ownership_13dg", "ownership_13dg_amendment",
        "insider_trade", "sec_fts_mention",
    )])
    # Insider trades first — they are the most time-critical signal class.
    _sec_order = {"insider_trade": 0, "ownership_13dg": 1, "ownership_13dg_amendment": 1,
                  "sec_fts_mention": 2, "13f_position": 3}
    sec_events.sort(key=lambda e: _sec_order.get(e.get("signal_type") or "", 9))
    news_events = _enrich_events([e for e in new_events if e.get("signal_type") == "public_statement"])
    # Sort: alpha_signal first, then position_update, then others — then by confidence desc
    _tier_order = {"alpha_signal": 0, "position_update": 1}
    news_events.sort(key=lambda e: (
        _tier_order.get(e.get("signal_tier") or "", 9),
        -float(e.get("confidence", 0)),
    ))

    return _env().get_template("alert.html.j2").render(
        sec_events=sec_events,
        news_events=news_events,
        review_events=_enrich_events(list(review_events or [])),
        model=model or {},
        tldr=tldr or {},
        analysis_13f=(model or {}).get("llm_13f_analysis", ""),
        top_signals=(model or {}).get("top_signals", []),
        signal_backtest=(model or {}).get("signal_backtest"),
        sector_breakdown=(model or {}).get("sector_breakdown", []),
        meta=meta,
    )


def subject(cfg: Config, new_events: list[dict], tldr: dict | None = None) -> str:
    prefix = cfg.raw.get("alert", {}).get("subject_prefix", "SA Alert")
    tldr = tldr or {}
    quarter = tldr.get("quarter", "")
    new_buys = tldr.get("new_buys", [])
    puts = tldr.get("puts_shorts", [])

    # Form 3/4/5 first: a fresh insider trade beats every other subject line.
    insider = [e for e in new_events if e.get("signal_type") == "insider_trade"]
    if insider:
        src0 = (insider[0].get("sources") or [{}])[0]
        ticker = src0.get("issuer_ticker") or (insider[0].get("ticker_guess") or ["?"])[0]
        doc = src0.get("document_type", "4")
        return f"{prefix} · Form {doc}: {ticker} — Insider-Trade von {cfg.primary_name}"

    fts = [e for e in new_events if e.get("signal_type") == "sec_fts_mention"]
    if fts and not any(e.get("signal_type") == "13f_position" for e in new_events):
        src0 = (fts[0].get("sources") or [{}])[0]
        return f"{prefix} · SEC-Erwähnung: {src0.get('form', '?')} von {src0.get('entity', '?')[:40]}"

    has_13f = any(e.get("signal_type") == "13f_position" for e in new_events)

    if has_13f and new_buys:
        bought = ", ".join(new_buys[:3])
        short_str = f" | Short: {', '.join(puts[:2])}" if puts else ""
        return f"{prefix} · {quarter} · Neu: {bought}{short_str}"
    if has_13f:
        return f"{prefix} · Neues 13F-Filing · {quarter}"

    # News-only alert
    tickers = sorted({t for e in new_events for t in (e.get("ticker_guess") or [])})
    if tickers:
        return f"{prefix} · News: {', '.join(tickers[:4])}"
    first_summary = new_events[0].get("summary", "")[:55] if new_events else ""
    return f"{prefix} · {first_summary}"
