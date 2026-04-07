"""
scanner/output/dashboard_generator.py
Generiert das GitHub Pages Haupt-Dashboard.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..utils.config import Config
from ..utils.state_manager import StateManager

logger = logging.getLogger(__name__)


def build_dashboard(state_manager: StateManager, regime: dict):
    Config.ensure_dirs()

    conn = sqlite3.connect(str(Config.DB_PATH))
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    recent_signals = conn.execute(
        """SELECT ticker, conviction, gate_status, regime_mode, date
           FROM signals WHERE date >= ? ORDER BY conviction DESC LIMIT 50""",
        (cutoff,)
    ).fetchall()

    cutoff14 = (datetime.utcnow() - timedelta(days=14)).date().isoformat()
    cards = conn.execute(
        """SELECT ticker, conviction, gate_status, laufzeit_months, date, html_path
           FROM trading_cards WHERE date >= ? AND gate_status = 'PASS'
           ORDER BY conviction DESC""",
        (cutoff14,)
    ).fetchall()

    positions = conn.execute(
        "SELECT * FROM active_positions WHERE status = 'OPEN'"
    ).fetchall()

    runs = conn.execute(
        """SELECT run_id, started_at, regime_mode, candidates,
                  claude_calls, cards_generated
           FROM run_log ORDER BY started_at DESC LIMIT 10"""
    ).fetchall()

    conn.close()

    regime_trend = state_manager.get_regime_trend(30)

    now        = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    mode       = regime.get("mode", "NORMAL")
    mode_color = "#ff4444" if mode == "STRESS" else "#00d4ff"

    # FIX: iv_rank_avg kann None sein (Warmup) — None-sicheres Format
    iv_raw     = regime.get("iv_rank_avg")
    iv_display = f"{iv_raw:.1f}%" if iv_raw is not None else "N/A (warmup)"

    energy_raw     = regime.get("energy_breadth", 0.5)
    energy_display = f"{energy_raw:.0%}" if energy_raw is not None else "N/A"

    stability_raw     = regime.get("regime_stability", 0.5)
    stability_display = f"{stability_raw:.2f}" if stability_raw is not None else "N/A"

    cards_html = ""
    for c in cards:
        fname = Path(c["html_path"]).name if c["html_path"] else f"{c['ticker']}.html"
        cards_html += f"""
        <a href="cards/{fname}" class="card-link">
          <div class="mini-card">
            <div class="mini-ticker">{c['ticker']}</div>
            <div class="mini-conviction">{c['conviction']:.1f}</div>
            <div class="mini-laufzeit">{c['laufzeit_months']}M CALL</div>
            <div class="mini-date">{c['date']}</div>
          </div>
        </a>"""

    if not cards_html:
        cards_html = '<div class="no-cards">Keine Trading Cards in den letzten 14 Tagen</div>'

    signals_html = ""
    for s in recent_signals[:20]:
        gate_c = {
            "PASS": "#44ff88",
            "WATCHLIST": "#ffd166",
            "NO_SIGNAL": "#5a7a9a",
            "BLOCKED_CONTRARIAN": "#ff4444",
            "CLAUDE_PARSE_FAILED": "#ff9944",
        }.get(s["gate_status"], "#5a7a9a")
        signals_html += f"""
        <tr>
          <td style="color:var(--accent)">{s['ticker']}</td>
          <td style="color:{gate_c}">{s['conviction']:.2f}</td>
          <td style="color:{gate_c}">{s['gate_status']}</td>
          <td>{s['regime_mode']}</td>
          <td>{s['date']}</td>
        </tr>"""

    runs_html = ""
    for r in runs:
        runs_html += f"""
        <tr>
          <td>{r['started_at'][:16] if r['started_at'] else ''}</td>
          <td style="color:{mode_color}">{r['regime_mode'] or '?'}</td>
          <td>{r['candidates'] or 0}</td>
          <td>{r['claude_calls'] or 0}</td>
          <td style="color:var(--accent)">{r['cards_generated'] or 0}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>Situational Awareness Scanner — Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Bebas+Neue&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
  :root {{
    --bg:#0a0b0d;--surface:#10131a;--surface2:#161c28;
    --border:#1f2d42;--border-bright:#2a3f5c;
    --accent:#00d4ff;--text:#c8d8e8;--text-dim:#5a7a9a;--text-bright:#eaf4ff;
    --green:#44ff88;--red:#ff4444;--amber:#ffd166;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);
        font-family:'IBM Plex Sans',sans-serif;font-size:13px;
        padding:20px;max-width:1200px;margin:0 auto}}
  h1{{font-family:'Bebas Neue';font-size:36px;letter-spacing:4px;
      color:var(--accent);margin-bottom:4px}}
  .subtitle{{font-family:'IBM Plex Mono';font-size:10px;color:var(--text-dim);
             letter-spacing:2px;margin-bottom:24px}}
  .regime-banner{{padding:12px 16px;border:1px solid {mode_color};
                  color:{mode_color};font-family:'IBM Plex Mono';
                  font-size:11px;letter-spacing:2px;margin-bottom:20px;
                  display:flex;justify-content:space-between}}
  .grid-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px}}
  .stat-box{{background:var(--surface);border:1px solid var(--border);
             padding:16px;text-align:center}}
  .stat-num{{font-family:'Bebas Neue';font-size:42px;color:var(--accent)}}
  .stat-label{{font-family:'IBM Plex Mono';font-size:9px;color:var(--text-dim);
               letter-spacing:2px}}
  .section{{background:var(--surface);border:1px solid var(--border);
            padding:16px;margin-bottom:16px}}
  .section-title{{font-family:'IBM Plex Mono';font-size:9px;letter-spacing:3px;
                  color:var(--text-dim);margin-bottom:12px;
                  padding-bottom:6px;border-bottom:1px solid var(--border)}}
  .cards-grid{{display:flex;flex-wrap:wrap;gap:10px}}
  .card-link{{text-decoration:none}}
  .mini-card{{background:var(--surface2);border:1px solid var(--accent);
              padding:12px 14px;min-width:120px;transition:border-color 0.2s}}
  .mini-card:hover{{border-color:var(--green)}}
  .mini-ticker{{font-family:'Bebas Neue';font-size:28px;color:var(--accent)}}
  .mini-conviction{{font-family:'IBM Plex Mono';font-size:18px;color:var(--green)}}
  .mini-laufzeit{{font-family:'IBM Plex Mono';font-size:10px;color:var(--amber)}}
  .mini-date{{font-family:'IBM Plex Mono';font-size:9px;color:var(--text-dim)}}
  .no-cards{{font-family:'IBM Plex Mono';font-size:11px;color:var(--text-dim);padding:10px}}
  table{{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono';font-size:11px}}
  th{{color:var(--text-dim);font-weight:400;letter-spacing:1px;text-align:left;
      padding:4px 8px;border-bottom:1px solid var(--border)}}
  td{{padding:5px 8px;border-bottom:1px solid var(--border);color:var(--text)}}
  tr:last-child td{{border-bottom:none}}
  .footer{{font-family:'IBM Plex Mono';font-size:9px;color:var(--text-dim);
           text-align:center;margin-top:20px;padding-top:10px;
           border-top:1px solid var(--border)}}
  @media(max-width:700px){{.grid-3{{grid-template-columns:1fr}}}}
</style>
</head>
<body>

<h1>SITUATIONAL AWARENESS</h1>
<div class="subtitle">SCANNER · CALL-OPTIONS INTELLIGENCE · v4.0 · {now}</div>

<div class="regime-banner">
  <span>REGIME: {mode} | IV-Rank Avg: {iv_display} |
    Energy Breadth: {energy_display} |
    Stability: {stability_display}</span>
  <span>CONVICTION THRESHOLD: {regime.get('conviction_threshold', 7.5)}</span>
</div>

<div class="grid-3">
  <div class="stat-box">
    <div class="stat-num">{len(cards)}</div>
    <div class="stat-label">TRADING CARDS (14 TAGE)</div>
  </div>
  <div class="stat-box">
    <div class="stat-num">{len(recent_signals)}</div>
    <div class="stat-label">SIGNALE (7 TAGE)</div>
  </div>
  <div class="stat-box">
    <div class="stat-num" style="color:{mode_color}">{mode}</div>
    <div class="stat-label">AKTUELLES REGIME</div>
  </div>
</div>

<div class="section">
  <div class="section-title">AKTIVE TRADING CARDS — PASS</div>
  <div class="cards-grid">{cards_html}</div>
</div>

<div class="section">
  <div class="section-title">SIGNAL-LOG (LETZTE 7 TAGE)</div>
  <table>
    <thead><tr>
      <th>TICKER</th><th>CONVICTION</th><th>STATUS</th><th>REGIME</th><th>DATUM</th>
    </tr></thead>
    <tbody>{signals_html}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">SCANNER RUNS</div>
  <table>
    <thead><tr>
      <th>ZEIT</th><th>REGIME</th><th>KANDIDATEN</th><th>CLAUDE CALLS</th><th>CARDS</th>
    </tr></thead>
    <tbody>{runs_html}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-title">REGIME-TREND (30 TAGE)</div>
  <div style="font-family:'IBM Plex Mono';font-size:11px;padding:8px">
    Trend: <span style="color:{mode_color}">{regime_trend.get('trend','?')}</span> |
    Stress: <span style="color:#ff4444">{regime_trend.get('stress_pct',0):.1f}%</span> |
    Normal: <span style="color:#44ff88">{regime_trend.get('normal_pct',0):.1f}%</span>
  </div>
</div>

<div class="footer">
  SA SCANNER v4.0 · Anthropic API + Tradier (Vollzugriff) + yfinance + EIA + FRED + EDGAR · {now}
</div>

</body>
</html>"""

    out = Config.DASH_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    logger.info(f"Dashboard generated: {out}")
