"""
scanner/output/email_notifier.py
Sendet HTML-Email im Apple-Design wenn Trading Cards generiert wurden.
Nutzt Gmail SMTP mit App-Passwort.
"""

import json
import logging
import smtplib
import sqlite3
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from ..utils.config import Config

logger = logging.getLogger(__name__)


def build_card_html(card: dict) -> str:
    """Baut HTML-Block für eine Trading Card."""
    ticker    = card.get("ticker", "???")
    company   = card.get("company_name", "")
    sector    = card.get("sector", "").replace("_", " ").upper()
    bt        = card.get("bottleneck_type", "?")
    conviction = card.get("conviction_total", 0.0)
    laufzeit  = card.get("laufzeit_months", 0)
    rationale = card.get("rationale", "")
    gegen     = card.get("gegen_szenario", "")

    option    = card.get("option", {})
    strike_pct = option.get("strike_pct_otm", 0)
    strike_abs = option.get("strike_absolute", 0)
    expiry     = option.get("expiration", "")
    entry      = option.get("entry_premium", 0)
    target_mult= option.get("target_multiplier", 0)
    stop_thesis= option.get("stop_thesis_trigger", "")
    laufzeit_b = option.get("laufzeit_begruendung", "")

    scores    = card.get("scores", {})
    s_salp    = scores.get("salp",       {}).get("score", 0)
    s_thiel   = scores.get("thiel",      {}).get("score", 0)
    s_shulman = scores.get("shulman",    {}).get("score", 0)
    s_regime  = scores.get("regime",     {}).get("score", 0)
    s_contra  = scores.get("contrarian", {}).get("score", 0)

    deep_net  = card.get("deep_network_signal", False)
    tags      = card.get("signal_tags", [])
    tags_str  = "  ·  ".join(tags[:6]) if tags else ""

    bt_color = {
        "ENERGIE": "#FF9500",
        "RECHEN":  "#007AFF",
        "BEIDE":   "#AF52DE",
    }.get(bt, "#8E8E93")

    conviction_color = (
        "#34C759" if conviction >= 8.5 else
        "#007AFF" if conviction >= 7.5 else
        "#FF9500"
    )

    return f"""
    <!-- TRADING CARD -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td style="background:#1C1C1E;border-radius:16px;padding:0;overflow:hidden;">

          <!-- Card Header -->
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:24px 24px 16px 24px;border-bottom:1px solid #2C2C2E;">
                <table width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;
                                  font-size:32px;font-weight:700;color:#FFFFFF;
                                  letter-spacing:-0.5px;line-height:1;">{ticker}</div>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                                  font-size:13px;color:#8E8E93;margin-top:4px;">{company}</div>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                                  font-size:12px;color:#636366;margin-top:2px;">{sector}</div>
                    </td>
                    <td style="text-align:right;vertical-align:top;">
                      <div style="display:inline-block;background:{bt_color}22;
                                  border:1px solid {bt_color};border-radius:8px;
                                  padding:6px 12px;
                                  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                                  font-size:11px;font-weight:600;color:{bt_color};
                                  letter-spacing:0.5px;">{bt}-FLASCHENHALS</div>
                      {f'<div style="margin-top:6px;font-size:10px;color:#007AFF;font-family:-apple-system,sans-serif;">◈ DEEP NETWORK</div>' if deep_net else ''}
                    </td>
                  </tr>
                </table>
              </td>
            </tr>

            <!-- Conviction Score -->
            <tr>
              <td style="padding:20px 24px;border-bottom:1px solid #2C2C2E;">
                <table width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                                  font-size:11px;color:#8E8E93;letter-spacing:1px;
                                  text-transform:uppercase;margin-bottom:4px;">CONVICTION</div>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;
                                  font-size:52px;font-weight:700;color:{conviction_color};
                                  line-height:1;">{conviction:.1f}</div>
                      <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                                  font-size:12px;color:#636366;margin-top:4px;">
                                  Laufzeit: {laufzeit} Monate</div>
                    </td>
                    <td style="vertical-align:middle;">
                      <!-- Score Bars -->
                      <table cellpadding="0" cellspacing="0">
                        {_score_bar("SA LP", s_salp, "#007AFF")}
                        {_score_bar("THIEL", s_thiel, "#FF9500")}
                        {_score_bar("SHULMAN", s_shulman, "#AF52DE")}
                        {_score_bar("REGIME", s_regime, "#34C759")}
                        {_score_bar("CONTRARIAN", max(s_contra,0), "#FF3B30")}
                      </table>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>

            <!-- Option Parameters -->
            <tr>
              <td style="padding:20px 24px;border-bottom:1px solid #2C2C2E;">
                <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                            font-size:11px;color:#8E8E93;letter-spacing:1px;
                            text-transform:uppercase;margin-bottom:12px;">CALL OPTION</div>
                <table width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    {_option_field("TYP", "CALL", "#34C759")}
                    {_option_field("STRIKE OTM", f"+{strike_pct:.1f}%", "#FFFFFF")}
                    {_option_field("STRIKE ABS", f"${strike_abs:.0f}" if strike_abs else "—", "#FFFFFF")}
                  </tr>
                  <tr><td colspan="3" style="height:10px;"></td></tr>
                  <tr>
                    {_option_field("EXPIRATION", expiry, "#FFFFFF")}
                    {_option_field("ENTRY", f"${entry:.2f}", "#FFFFFF")}
                    {_option_field("TARGET", f"{target_mult:.1f}x", "#34C759")}
                  </tr>
                </table>
                {f'<div style="margin-top:10px;font-size:11px;color:#636366;font-family:-apple-system,sans-serif;">{laufzeit_b}</div>' if laufzeit_b else ''}
              </td>
            </tr>

            <!-- Rationale -->
            <tr>
              <td style="padding:20px 24px;border-bottom:1px solid #2C2C2E;">
                <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                            font-size:11px;color:#8E8E93;letter-spacing:1px;
                            text-transform:uppercase;margin-bottom:8px;">ANALYSE</div>
                <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                            font-size:14px;color:#EBEBF5;line-height:1.6;">{rationale}</div>
              </td>
            </tr>

            <!-- Stop / Gegen-Szenario -->
            <tr>
              <td style="padding:20px 24px;border-bottom:1px solid #2C2C2E;">
                <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                            font-size:11px;color:#FF3B30;letter-spacing:1px;
                            text-transform:uppercase;margin-bottom:8px;">STOP / GEGEN-SZENARIO</div>
                <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;
                            font-size:13px;color:#FF6B6B;line-height:1.5;
                            background:#FF3B3011;border-left:3px solid #FF3B30;
                            padding:10px 12px;border-radius:0 8px 8px 0;">
                  <strong>Thesis-Trigger:</strong> {stop_thesis}<br><br>
                  <strong>Gegen-Szenario:</strong> {gegen}
                </div>
              </td>
            </tr>

            <!-- Tags -->
            {f'''<tr>
              <td style="padding:16px 24px;">
                <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                            font-size:11px;color:#636366;">{tags_str}</div>
              </td>
            </tr>''' if tags_str else ''}

          </table>
        </td>
      </tr>
    </table>"""


def _score_bar(label: str, score: float, color: str) -> str:
    pct = int(score / 10 * 80)
    return f"""
        <tr>
          <td style="padding:2px 0;">
            <div style="font-family:-apple-system,sans-serif;font-size:10px;
                        color:#8E8E93;width:70px;">{label}</div>
          </td>
          <td style="padding:2px 8px;">
            <div style="background:#2C2C2E;border-radius:3px;width:80px;height:5px;">
              <div style="background:{color};width:{pct}px;height:5px;border-radius:3px;"></div>
            </div>
          </td>
          <td style="padding:2px 0;">
            <div style="font-family:-apple-system,sans-serif;font-size:11px;
                        font-weight:600;color:{color};width:24px;">{score:.0f}</div>
          </td>
        </tr>"""


def _option_field(label: str, value: str, color: str) -> str:
    return f"""
        <td style="padding:0 16px 0 0;vertical-align:top;">
          <div style="font-family:-apple-system,sans-serif;font-size:10px;
                      color:#636366;letter-spacing:0.5px;margin-bottom:3px;">{label}</div>
          <div style="font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;
                      font-size:16px;font-weight:600;color:{color};">{value}</div>
        </td>"""


def build_email_html(cards: list, regime: dict, run_date: str) -> str:
    """Baut vollständige HTML-Email im Apple-Design."""
    mode       = regime.get("mode", "NORMAL")
    mode_color = "#FF3B30" if mode == "STRESS" else "#007AFF"
    energy_b   = regime.get("energy_breadth", 0)
    iv_rank    = regime.get("iv_rank_avg", 50)
    n_cards    = len(cards)

    cards_html = "".join([build_card_html(c) for c in cards])

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SA Scanner — {n_cards} Trading Card{'s' if n_cards > 1 else ''}</title>
</head>
<body style="margin:0;padding:0;background:#000000;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',Helvetica,sans-serif;">

  <!-- Outer wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#000000;padding:20px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="padding:0 0 24px 0;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <div style="font-size:13px;color:#636366;letter-spacing:2px;
                                text-transform:uppercase;margin-bottom:4px;">
                      SITUATIONAL AWARENESS SCANNER</div>
                    <div style="font-size:28px;font-weight:700;color:#FFFFFF;
                                letter-spacing:-0.5px;">
                      {n_cards} Trading Card{'s' if n_cards > 1 else ''} generiert</div>
                    <div style="font-size:13px;color:#636366;margin-top:4px;">{run_date}</div>
                  </td>
                  <td style="text-align:right;vertical-align:top;">
                    <div style="display:inline-block;background:{mode_color}22;
                                border:1px solid {mode_color};border-radius:10px;
                                padding:8px 14px;font-size:12px;font-weight:600;
                                color:{mode_color};letter-spacing:1px;">{mode} REGIME</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Regime Stats -->
          <tr>
            <td style="padding:0 0 24px 0;">
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="background:#1C1C1E;border-radius:12px;padding:16px;">
                <tr>
                  <td style="padding:0 24px 0 0;text-align:center;">
                    <div style="font-size:10px;color:#636366;letter-spacing:1px;
                                text-transform:uppercase;margin-bottom:4px;">ENERGY BREADTH</div>
                    <div style="font-size:22px;font-weight:700;color:#34C759;">{energy_b:.0%}</div>
                  </td>
                  <td style="padding:0 24px;text-align:center;
                             border-left:1px solid #2C2C2E;border-right:1px solid #2C2C2E;">
                    <div style="font-size:10px;color:#636366;letter-spacing:1px;
                                text-transform:uppercase;margin-bottom:4px;">IV RANK AVG</div>
                    <div style="font-size:22px;font-weight:700;color:#FF9500;">{iv_rank:.0f}%</div>
                  </td>
                  <td style="padding:0 0 0 24px;text-align:center;">
                    <div style="font-size:10px;color:#636366;letter-spacing:1px;
                                text-transform:uppercase;margin-bottom:4px;">CONVICTION MIN</div>
                    <div style="font-size:22px;font-weight:700;color:#007AFF;">
                      {'8.0' if mode == 'STRESS' else '7.5'}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Trading Cards -->
          <tr>
            <td>
              {cards_html}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:24px 0 0 0;border-top:1px solid #1C1C1E;">
              <div style="font-size:11px;color:#3A3A3C;text-align:center;line-height:1.6;">
                SA Scanner v4.0 · Anthropic API + Tradier + yfinance + EIA + FRED + EDGAR<br>
                Basis: Aschenbrenner „The Decade Ahead" · {run_date}<br><br>
                <strong style="color:#FF3B30;">⚠ Kein Anlageberater.</strong>
                Dieses System liefert Richtungs-Signale, keine Garantien.
                Options-Trading birgt erhebliche Verlustrisiken.
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""


def send_email(cards: list, regime: dict) -> bool:
    """
    Sendet HTML-Email mit allen Trading Cards.
    Benötigt GMAIL_USER und GMAIL_APP_PASSWORD als Secrets.
    """
    import os

    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_email   = os.environ.get("NOTIFY_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        logger.warning("Email not configured: GMAIL_USER or GMAIL_APP_PASSWORD missing")
        return False

    if not cards:
        logger.info("No cards to send")
        return False

    run_date  = datetime.utcnow().strftime("%d. %B %Y · %H:%M UTC")
    n_cards   = len(cards)
    tickers   = ", ".join([c.get("ticker", "?") for c in cards])

    html_body = build_email_html(cards, regime, run_date)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"🎯 SA Scanner: {n_cards} Trading Card{'s' if n_cards > 1 else ''} "
        f"— {tickers}"
    )
    msg["From"]    = f"SA Scanner <{gmail_user}>"
    msg["To"]      = to_email

    # Plain text fallback
    plain = f"""SA Scanner — {n_cards} Trading Card(s)
{run_date}

Ticker: {tickers}

"""
    for card in cards:
        plain += f"""
{card.get('ticker')} — Conviction: {card.get('conviction_total', 0):.1f}
Strike: +{card.get('option', {}).get('strike_pct_otm', 0):.1f}% OTM
Entry: ${card.get('option', {}).get('entry_premium', 0):.2f}
Target: {card.get('option', {}).get('target_multiplier', 0):.1f}x
Laufzeit: {card.get('laufzeit_months', 0)} Monate

{card.get('rationale', '')}
---
"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_email, msg.as_string())
        logger.info(f"Email sent: {n_cards} cards to {to_email}")
        return True

    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


def load_todays_cards() -> list:
    """Lädt alle heutigen PASS-Cards aus SQLite."""
    try:
        import sqlite3
        today = datetime.utcnow().date().isoformat()
        conn  = sqlite3.connect(str(Config.DB_PATH))
        conn.row_factory = sqlite3.Row
        rows  = conn.execute(
            """SELECT card_json FROM trading_cards
               WHERE date = ? AND gate_status = 'PASS'
               ORDER BY conviction DESC""",
            (today,)
        ).fetchall()
        conn.close()
        return [json.loads(r["card_json"]) for r in rows]
    except Exception as e:
        logger.error(f"Load cards error: {e}")
        return []


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    logging.basicConfig(level=logging.INFO)
    cards  = load_todays_cards()
    regime = {"mode": "NORMAL", "energy_breadth": 0.8, "iv_rank_avg": 50}
    if cards:
        send_email(cards, regime)
    else:
        logger.info("No cards today — no email sent")
