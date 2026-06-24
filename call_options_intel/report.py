"""
report.py
=========
Renders a ScanResult to Markdown, CSV, JSON and (optionally) HTML.

Every report ALWAYS includes: disclaimers, per-candidate reasons-against,
data-quality flags and a paper-trading recommendation. The goal is an
explainable, non-overconfident research artifact — not a buy list.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import DISCLAIMER
from .models import OptionCandidate, ScanResult

logger = logging.getLogger("coi.report")


class ReportGenerator:
    def __init__(self, report_cfg: dict):
        self.cfg = (report_cfg or {}).get("report", {})
        self.disclaimers = self.cfg.get("disclaimers", [DISCLAIMER])

    # ── public ─────────────────────────────────────────────────────────
    def render(self, result: ScanResult, fmt: str) -> str:
        fmt = fmt.lower()
        if fmt in ("markdown", "md"):
            return self.to_markdown(result)
        if fmt == "csv":
            return self.to_csv(result)
        if fmt == "json":
            return self.to_json(result)
        if fmt == "html":
            return self.to_html(result)
        raise ValueError(f"Unknown report format: {fmt}")

    def write(self, result: ScanResult, formats: list[str],
              output_dir: str | Path, stem: str = "scan") -> dict[str, Path]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = {"markdown": "md", "md": "md", "csv": "csv",
               "json": "json", "html": "html"}
        written: dict[str, Path] = {}
        for fmt in formats:
            content = self.render(result, fmt)
            path = out_dir / f"{stem}.{ext.get(fmt.lower(), fmt.lower())}"
            path.write_text(content, encoding="utf-8")
            written[fmt] = path
            logger.info("Wrote %s report -> %s", fmt, path)
        return written

    # ── markdown ────────────────────────────────────────────────────────
    def to_markdown(self, r: ScanResult) -> str:
        L: list[str] = []
        L.append(f"# {self.cfg.get('title', 'CALL-Options Intelligence')}")
        L.append("")
        L.append(f"_Generated: {r.generated_at} · mode: **{r.mode}** · "
                 f"universe: {r.universe_size} tickers_")
        L.append("")
        L.append("> " + "  \n> ".join(self.disclaimers))
        L.append("")
        if r.mode != "live":
            L.append("> ⚠️ **OFFLINE / DEMO DATA** — every price, option quote, IV and "
                     "13F figure below comes from bundled **synthetic fixtures** for "
                     "testing. These are **NOT real market data** and must not be "
                     "traded on. Run with `--live` to pull real free data.")
            L.append("")

        # Executive summary
        L.append("## Executive Summary")
        L.append("")
        L.append(f"- Top candidates: **{len(r.top)}**")
        L.append(f"- Watchlist: **{len(r.watchlist)}**")
        L.append(f"- Option contracts ranked: **{len(r.candidates)}**")
        L.append(f"- Contracts rejected by filters: **{len(r.rejected_contracts)}**")
        if r.regime:
            L.append(f"- Volatility regime: **{r.regime.get('label', 'n/a')}** "
                     f"(VIX {r.regime.get('vix', 'n/a')})")
        if r.data_quality_warnings:
            L.append(f"- ⚠️ Data-quality warnings: {len(r.data_quality_warnings)}")
        L.append("")

        # Top candidates
        L.append("## Top CALL Candidates")
        L.append("")
        if not r.top:
            L.append("_No candidates cleared the top-score gate this run._")
        for i, c in enumerate(r.top, 1):
            L.extend(self._candidate_block(i, c))
        L.append("")

        # Watchlist
        L.append("## Watchlist (good thesis — wait for entry)")
        L.append("")
        if not r.watchlist:
            L.append("_Empty._")
        else:
            L.append("| Ticker | Score | Conf | Why wait |")
            L.append("|---|---|---|---|")
            for ev in r.watchlist[: self.cfg.get("watchlist_size", 15)]:
                why = "; ".join(ev.reasons_against[:2]) or "below top gate"
                L.append(f"| {ev.ticker} | {ev.breakdown.final_score:.2f} | "
                         f"{ev.breakdown.confidence_label} | {why} |")
        L.append("")

        # 13F changes
        L.append("## 13F Smart-Money Changes")
        L.append("")
        if not r.thirteen_f_changes:
            L.append("_No 13F data this run._")
        else:
            L.append("| Ticker | Action | Conviction | Managers | Qtrs |")
            L.append("|---|---|---|---|---|")
            for t, ch in sorted(r.thirteen_f_changes.items(),
                                key=lambda kv: kv[1].conviction_score, reverse=True):
                L.append(f"| {t} | {ch.action} | {ch.conviction_score:+.2f} | "
                         f"{', '.join(ch.managers) or '—'} | {ch.quarters_held} |")
        L.append("")

        # Thesis map
        L.append("## Thesis Map")
        L.append("")
        if r.thesis_vector and r.thesis_vector.weights:
            L.append(f"_Thesis corpus confidence: {r.thesis_vector.confidence:.2f} "
                     f"from {len(r.thesis_vector.sources)} source(s)._")
            L.append("")
            L.append("| Theme | Intensity |")
            L.append("|---|---|")
            for theme, w in sorted(r.thesis_vector.weights.items(),
                                   key=lambda kv: kv[1], reverse=True):
                L.append(f"| {theme} | {w:.2f} |")
        else:
            L.append("_No thesis corpus ingested; relevance priors used as fallback._")
        L.append("")

        # Market regime
        L.append("## Market Regime")
        L.append("")
        if r.regime:
            for k, v in r.regime.items():
                L.append(f"- **{k}**: {v}")
        else:
            L.append("_n/a_")
        L.append("")

        # Rejected
        L.append("## Rejected Contracts (transparency)")
        L.append("")
        if not r.rejected_contracts:
            L.append("_None._")
        else:
            by_reason: dict[str, int] = {}
            for rc in r.rejected_contracts:
                by_reason[rc.reason] = by_reason.get(rc.reason, 0) + 1
            L.append("| Reason | Count |")
            L.append("|---|---|")
            for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
                L.append(f"| {reason} | {n} |")
            L.append("")
            L.append("Sample:")
            for rc in r.rejected_contracts[: self.cfg.get("rejected_sample", 20)]:
                L.append(f"- `{rc.ticker}` — {rc.reason}: {rc.detail}")
        L.append("")

        # Data quality warnings
        L.append("## Data Quality Warnings")
        L.append("")
        warns = list(r.data_quality_warnings) + list(r.config_warnings)
        if not warns:
            L.append("_None._")
        else:
            for w in warns:
                L.append(f"- ⚠️ {w}")
        L.append("")

        # Paper-trading note
        L.append("## Test / Paper-Trading Recommendation")
        L.append("")
        L.append("This is a **research artifact**. Recommended workflow:")
        L.append("")
        L.append("1. Treat every row as a hypothesis, not a trade.")
        L.append("2. Log candidates to the signal store (`backtest record`) and "
                 "evaluate underlying/option-proxy returns after 7/14/30/60 days.")
        L.append("3. Compare hit-rate vs. simply buying the underlying / QQQ / SMH "
                 "before trusting any edge.")
        L.append("4. Never size a position from a low-confidence row.")
        L.append("")
        return "\n".join(L)

    def _candidate_block(self, i: int, c: OptionCandidate) -> list[str]:
        ct = c.contract
        tag = " · ⚡EVENT" if c.is_event_trade else ""
        L = [
            f"### {i}. {c.ticker} — {c.name}  ·  score **{c.final_score:.2f}** "
            f"({c.confidence_label} confidence){tag}",
            "",
            f"- **Contract**: {ct.strike:g} CALL exp {ct.expiry} "
            f"({ct.dte} DTE, {c.strike_zone})",
            f"- **Pricing**: bid {fmt(ct.bid)} / ask {fmt(ct.ask)} / mid {fmt(ct.mid)} "
            f"· spread {pct(ct.spread_pct)} · IV {pct(ct.iv)}",
            f"- **Liquidity**: OI {ct.open_interest} · vol {ct.volume} · "
            f"delta {fmt(ct.delta)}{' (est.)' if ct.delta_estimated else ''} · "
            f"moneyness {fmt(ct.moneyness)}",
            f"- **Suggested window**: {c.suggested_expiry_window}",
            f"- **Thesis**: {c.thesis_explanation}",
            "- **Signal breakdown**: " + ", ".join(
                f"{k}={v}" for k, v in c.breakdown.as_dict().items()),
        ]
        if c.catalysts:
            L.append(f"- **Catalysts**: {', '.join(c.catalysts)}")
        if c.reasons_for:
            L.append("- **For**: " + "; ".join(dedupe(c.reasons_for)[:6]))
        # Always render reasons-against (research integrity: never a one-sided pick)
        against = dedupe(c.reasons_against)[:6] or [
            "no automatic red flags — long calls can still lose 100%; verify "
            "live IV/spread/catalyst before sizing"]
        L.append("- **Against**: " + "; ".join(against))
        if c.risk_notes:
            L.append("- **Risk notes**: " + "; ".join(dedupe(c.risk_notes)))
        L.append(f"- **Paper recommendation**: {c.paper_recommendation}")
        L.append("")
        return L

    # ── csv ─────────────────────────────────────────────────────────────
    def to_csv(self, r: ScanResult) -> str:
        buf = io.StringIO()
        cols = [
            "rank", "ticker", "name", "category", "expiry", "strike", "dte",
            "strike_zone", "bid", "ask", "mid", "spread_pct", "iv",
            "open_interest", "volume", "delta", "delta_estimated", "moneyness",
            "final_score", "confidence", "confidence_label",
            "thesis", "thirteen_f", "market", "options", "catalyst",
            "risk_penalty", "is_event_trade", "reasons_against", "data_flags",
            "paper_recommendation",
        ]
        w = csv.writer(buf)
        w.writerow(cols)
        for rank, c in enumerate(r.candidates, 1):
            ct = c.contract
            bd = c.breakdown
            w.writerow([
                rank, c.ticker, c.name, c.category, ct.expiry, ct.strike, ct.dte,
                c.strike_zone, ct.bid, ct.ask, ct.mid, ct.spread_pct, ct.iv,
                ct.open_interest, ct.volume, ct.delta, ct.delta_estimated, ct.moneyness,
                bd.final_score, bd.confidence, bd.confidence_label,
                bd.thesis, bd.thirteen_f, bd.market, bd.options, bd.catalyst,
                bd.risk_penalty, c.is_event_trade,
                " | ".join(dedupe(c.reasons_against)),
                " | ".join(dedupe(c.data_flags)),
                c.paper_recommendation,
            ])
        return buf.getvalue()

    # ── json ────────────────────────────────────────────────────────────
    def to_json(self, r: ScanResult) -> str:
        return json.dumps(_to_jsonable(r), indent=2, default=str)

    # ── html (optional, simple) ─────────────────────────────────────────
    def to_html(self, r: ScanResult) -> str:
        rows = []
        for rank, c in enumerate(r.candidates, 1):
            ct = c.contract
            rows.append(
                f"<tr><td>{rank}</td><td>{c.ticker}</td><td>{ct.strike:g}</td>"
                f"<td>{ct.expiry}</td><td>{ct.dte}</td>"
                f"<td>{c.final_score:.2f}</td><td>{c.confidence_label}</td>"
                f"<td>{'⚡' if c.is_event_trade else ''}</td>"
                f"<td>{'; '.join(dedupe(c.reasons_against)[:3])}</td></tr>"
            )
        disc = "<br>".join(self.disclaimers)
        offline = "" if r.mode == "live" else (
            '<div class="demo">⚠️ OFFLINE / DEMO DATA — all figures are synthetic '
            'bundled fixtures, NOT real market data. Run with --live for real free '
            'data.</div>')
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{self.cfg.get('title', 'CALL-Options Intelligence')}</title>
<style>body{{font-family:system-ui,Arial;margin:2rem;color:#111}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px;font-size:13px}}
.warn{{background:#fff3cd;padding:1rem;border:1px solid #ffe69c;border-radius:6px}}
.demo{{background:#f8d7da;padding:1rem;border:1px solid #f1aeb5;border-radius:6px;margin-top:8px;font-weight:bold}}</style></head>
<body><h1>{self.cfg.get('title', 'CALL-Options Intelligence')}</h1>
<p><em>Generated {r.generated_at} · mode {r.mode} · universe {r.universe_size}</em></p>
<div class="warn">{disc}</div>{offline}
<h2>Ranked CALL candidates</h2>
<table><tr><th>#</th><th>Ticker</th><th>Strike</th><th>Expiry</th><th>DTE</th>
<th>Score</th><th>Conf</th><th>Event</th><th>Reasons against</th></tr>
{''.join(rows) or '<tr><td colspan=9>No candidates</td></tr>'}</table>
</body></html>"""


# ── helpers ────────────────────────────────────────────────────────────────
def fmt(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.2f}"
    return str(x)


def pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _to_jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj
