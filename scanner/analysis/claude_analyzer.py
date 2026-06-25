"""
scanner/analysis/claude_analyzer.py

FIXES v2:
    R5: Claude bekommt jetzt den vollständigen RSS-Kontext der den
        Pre-Filter-Bonus ausgelöst hat — nicht nur Ticker-gefilterte
        Artikel. Separater Block "pre_filter_trigger_articles" im Prompt.
"""

import json
import logging
from datetime import datetime

import anthropic

from ..utils.config import Config
from ..utils.ticker_mapper import TickerMapper
from .pre_filter import PreFilter
# R-17 FIX: ConvictionResult war importiert aber nie verwendet — entfernt.
from .scoring_engine import ScoringEngine
from ..signals.regime_detector import RegimeDetector
from ..signals.contrarian_gate import ContrarianGate
from ..signals.shulman_layer import ShulmanLayer
from ..signals.thiel_layer import ThielLayer

logger  = logging.getLogger(__name__)
client  = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
mapper  = TickerMapper()


# ── PROMPT A: Thesis-Analyse + Scoring (cacheable System-Teil) ────────────────
# Statischer Kontext — wird bei jedem Run gecacht (Anthropic Prompt Caching).
PROMPT_A_SYSTEM = """Du bist der Analyse-Layer des Situational Awareness Scanner Systems.
Deine Aufgabe ist eine strukturierte Thesis-Analyse basierend auf Leopold Aschenbrenners
"The Decade Ahead" (SALP-Framework: Situational Awareness LP).

Framework-Layer:
- SALP: Portfoliobewegungen von Aschenbrenner / Situational Awareness LP (13F/SC13D/Form4)
- THIEL: Peter Thiel Netzwerk-Signale (Katechon, Zero-to-One, Sovereign AI)
- SHULMAN: Carl Shulman empirische Loops (Robot Doublings, Compute-Substitutability, IGE-Loop)
- MULTIGATE: Qualitäts-gewichtete RSS/News-Signale
- REGIME: Markt-Regime (Normal/Stress) basierend auf IV-Rank, CapEx, Energie-Breadth
- CONTRARIAN: Aktive Gegenthesen (AI-Winter, Scaling-Plateau, Regulation)

Scoring-Skala: 1.0–10.0 pro Layer. Conviction-Total = gewichteter Durchschnitt.
Gate-Regeln: PASS >= 7.5 (Normal) / 8.0 (Stress) | WATCHLIST >= 6.5 | sonst NO_SIGNAL

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt ohne führenden oder nachfolgenden Text."""

# ── PROMPT A: Variable Teil (per-ticker Analyse-Request) ──────────────────────
PROMPT_A_TEMPLATE = """<analysis_request>

  <ticker>{ticker}</ticker>
  <sector>{sector}</sector>
  <regime_mode>{regime_mode}</regime_mode>
  <conviction_threshold>{threshold}</conviction_threshold>

  <pre_computed_scores>
{pre_scores_json}
  </pre_computed_scores>

  <shulman_data_quality>
    Empirical Score: {empirical_score}/3
    Available Datapoints: {available_datapoints}/3
    Data Gaps: EIA={eia_gap} | CapEx={capex_gap} | NVDA={nvda_gap}
    Note: {shulman_quality_note}
  </shulman_data_quality>

  <market_data>
{market_data_json}
  </market_data>

  <pre_filter_trigger_articles>
    Diese Artikel haben den Pre-Filter-Bonus für {ticker} ausgelöst:
{trigger_articles_json}
  </pre_filter_trigger_articles>

  <ticker_specific_articles>
{ticker_articles_json}
  </ticker_specific_articles>

  <general_signal_articles>
    Thiel-Artikel: {thiel_article_count}
    Energie-Artikel: {energy_article_count}
    SALP-Artikel: {salp_article_count}
{general_articles_json}
  </general_signal_articles>

  <step_1_bottleneck>
    Ist diese News primär eine Lösung für einen RECHEN-Flaschenhals
    oder ENERGIE-Flaschenhals? Antworte: "RECHEN", "ENERGIE" oder "BEIDE".
  </step_1_bottleneck>

  <step_2_salp_analysis>
    SA LP Filing-Klasse: {filing_class}
    Bewerte die Portfoliobewegung im Kontext der Flaschenhals-These.
    Shulman-Begleittext gefunden: {shulman_begleit}
  </step_2_salp_analysis>

  <step_3_thiel_thesis>
    Antichrist-Check: ZENTRALISIERENDE KRAFT oder DEZENTRALE/SOVEREIGN LÖSUNG (Katechon)?
    Zero to One: Baut {ticker} ein echtes Monopol auf?
    Aktive Netzwerk-Akteure: {active_actors}
    Deep Network Signal: {deep_network}
    Signal-Typ: {thiel_signal_type}
  </step_3_thiel_thesis>

  <step_4_shulman>
    WICHTIG: Bei Data Gaps Score nicht als negatives Signal werten.
    EIA Stromwachstum: {eia_growth} | CapEx-Trend: {capex_trend} | NVDA-Wachstum: {nvda_growth}
  </step_4_shulman>

  <step_5_contrarian>
    Contrarian-Score: {contrarian_score}
    Aktive Gegenthesen: {gegenthesen}
    RSI: {rsi} | Put/Call-Ratio: {put_call}
  </step_5_contrarian>

  <output_format>
    {{
      "ticker": "{ticker}",
      "company_name": "...",
      "sector": "{sector}",
      "bottleneck_type": "RECHEN|ENERGIE|BEIDE",
      "regime_mode": "{regime_mode}",
      "scores": {{
        "salp":       {{"score": float, "filing_class": "...", "rationale": "..."}},
        "thiel":      {{"score": float, "signal_type": "...", "katechon_bonus": float, "rationale": "..."}},
        "shulman":    {{"score": float, "empirical_score": int, "weight_modifier": float, "data_gaps": bool, "rationale": "..."}},
        "multigate":  {{"score": float, "rationale": "..."}},
        "regime":     {{"score": float, "rationale": "..."}},
        "contrarian": {{"score": float, "gate_blocked": bool, "gegenthesen": [], "rationale": "..."}}
      }},
      "conviction_total": float,
      "conviction_gate": "PASS|WATCHLIST|NO_SIGNAL|BLOCKED_CONTRARIAN",
      "deep_network_signal": bool,
      "laufzeit_months": int,
      "rationale": "3-5 Sätze alle aktiven Layer",
      "gegen_szenario": "Spezifisches Widerlegungs-Ereignis",
      "signal_tags": []
    }}
  </output_format>

</analysis_request>"""

# ── PROMPT B: Options-Parameter (nur bei PASS, separater API-Call) ─────────────
PROMPT_B_TEMPLATE = """Du bist der Options-Parameter-Layer des SA Scanners.
Conviction PASS für {ticker} ({conviction:.2f}/{threshold}). Laufzeit: {laufzeit_months} Monate.

Optionsdaten (beste Calls nach Liquidität):
{top_calls_json}

Empfehle die optimale CALL-Option. Antworte NUR mit einem validen JSON-Objekt:

{{
  "option": {{
    "type": "CALL",
    "strike_pct_otm": float,
    "strike_absolute": float,
    "expiration": "YYYY-MM-DD",
    "entry_premium": float,
    "target_multiplier": float,
    "laufzeit_begruendung": "...",
    "stop_thesis_trigger": "Spezifisches Ereignis",
    "stop_technical_trigger": "-40% Prämie in 30 Tagen ODER IV-Crush >15 Punkte",
    "checkpoint_90d": "...",
    "checkpoint_180d": "...",
    "checkpoint_monthly": "..."
  }},
  "liquidity_flags": []
}}"""


class ClaudeAnalyzer:

    def __init__(self):
        self.pre_filter = PreFilter()
        self.scorer     = ScoringEngine()
        self.contrarian = ContrarianGate()
        self.shulman    = ShulmanLayer()
        self.thiel      = ThielLayer()

    def _get_trigger_articles(self, ticker: str,
                               rss: list, all_data: dict) -> list:
        """
        R5 FIX: Sammelt ALLE Artikel die zum Pre-Filter-Bonus beigetragen haben.
        Nicht nur ticker-spezifische — auch allgemeine Thiel/Energie-Artikel.
        """
        trigger = []

        # 1. Ticker-spezifische Artikel
        ticker_articles = [
            a for a in rss
            if ticker in str(a.get("tickers", []))
        ]
        trigger.extend(ticker_articles)

        # 2. Thiel-Artikel (auch ohne Ticker-Bezug)
        thiel_articles = [
            a for a in rss
            if a.get("signals", {}).get("thiel")
            and a not in trigger
        ]
        trigger.extend(thiel_articles[:3])

        # 3. Energie-Artikel für Energie-Sektor-Ticker
        sector = mapper.get_sector(ticker)
        if sector == "energy_infrastructure":
            energy_articles = [
                a for a in rss
                if a.get("signals", {}).get("bottleneck_energy")
                and a not in trigger
            ]
            trigger.extend(energy_articles[:2])

        # 4. SALP-Artikel
        salp_articles = [
            a for a in rss
            if a.get("signals", {}).get("salp")
            and a not in trigger
        ]
        trigger.extend(salp_articles[:2])

        # Sortiert nach quality_score (R7 Fix)
        trigger.sort(
            key=lambda x: x.get("quality_score", x.get("weighted_relevance", 0)),
            reverse=True
        )

        # Kompakte Darstellung für Prompt
        return [
            {
                "source":      a.get("source"),
                "credibility": a.get("credibility"),
                "title":       a.get("title"),
                "summary":     a.get("summary", "")[:300],
                "signals":     a.get("signals", {}),
                "tickers":     a.get("tickers", []),
            }
            for a in trigger[:8]  # Max 8 Artikel
        ]

    def analyze_ticker(self, ticker: str, all_data: dict,
                       regime: dict, sec_data: dict,
                       state_manager) -> dict | None:

        sector = mapper.get_sector(ticker)

        # Pre-Filter (state_manager für Auto-Threshold)
        should_call, pre_score = self.pre_filter.should_call_claude(
            ticker, all_data, regime, sec_data, state_manager
        )
        if not should_call:
            return None

        logger.info(f"Claude analysis: {ticker} (pre_score={pre_score})")

        rss          = all_data.get("rss", [])
        options_data = all_data.get("options", {})

        # Contrarian Gate
        contrarian_data = self.contrarian.evaluate(rss, ticker, options_data)
        if contrarian_data["gate_blocked"]:
            return {
                "ticker":          ticker,
                "conviction_gate": "BLOCKED_CONTRARIAN",
                "conviction_total": 0.0,
                "contrarian_data": contrarian_data,
            }

        shulman_data = self.shulman.evaluate(all_data, ticker, sector)
        thiel_data   = self.thiel.evaluate(rss, sec_data, state_manager)

        # Ticker-spezifische Options-Daten
        ticker_opts = options_data.get(ticker, {})
        top_calls   = (
            ticker_opts.get("target_calls", {}).get("calls", []) or []
        )[:3]

        # SALP Score
        classifications = sec_data.get("classifications", [])
        ticker_class    = next(
            (c for c in classifications if c["ticker"] == ticker), None
        )
        salp_score   = ticker_class["score"] if ticker_class else 3.0
        filing_class = ticker_class["class"] if ticker_class else "NONE"

        regime_score   = regime.get("regime_score", 5.0)
        multigate_articles = [
            a for a in rss
            if a.get("signals", {}).get("salp") or
               a.get("credibility", 0) > 0.85
        ]
        multigate_score = min(5.0 + len(multigate_articles) * 0.5, 10.0)

        # R5 FIX: Trigger-Artikel sammeln
        trigger_articles = self._get_trigger_articles(ticker, rss, all_data)

        # Ticker-spezifische Artikel
        ticker_articles = [
            a for a in rss
            if ticker in str(a.get("tickers", []))
        ]

        # Allgemeine Signal-Artikel für Context
        thiel_articles  = [a for a in rss if a.get("signals", {}).get("thiel")]
        energy_articles = [a for a in rss if a.get("signals", {}).get("bottleneck_energy")]
        salp_articles   = [a for a in rss if a.get("signals", {}).get("salp")]

        # Shulman Data Gap Info für Prompt
        data_gaps = shulman_data.get("data_gaps", {})
        shulman_quality_note = (
            "ALLE Datenpunkte fehlen (API-Verfügbarkeit). "
            "Score 0 ist DATA GAP, kein negatives Signal."
            if data_gaps.get("all_gaps") else
            f"Teilweise Datenlücken: "
            f"EIA={data_gaps.get('eia_gap')} "
            f"CapEx={data_gaps.get('capex_gap')} "
            f"NVDA={data_gaps.get('nvda_gap')}. "
            f"Score basiert auf {shulman_data.get('available_datapoints', 0)}/3 Datenpunkten."
            if data_gaps.get("any_gap") else
            "Alle Datenpunkte verfügbar. Score ist echtes Signal."
        )

        # Pre-Scores
        pre_scores = {
            "salp":           salp_score,
            "thiel":          thiel_data["thiel_score"],
            "shulman":        shulman_data["shulman_score"],
            "multigate":      multigate_score,
            "regime":         regime_score,
            "contrarian":     contrarian_data["contrarian_score"],
            "katechon_bonus": thiel_data["katechon_bonus"],
        }

        # R-3 FIX: ScoringEngine.calculate() statt manueller Gewichtsberechnung.
        # Damit sind Shulman-Surplus-Umverteilung, Contrarian-Malus und
        # Katechon-Additivbonus (R-16) korrekt angewendet.
        scorer_result  = self.scorer.calculate(ticker, pre_scores, regime, shulman_data)
        pre_conviction = scorer_result.conviction_total
        laufzeit       = scorer_result.laufzeit_months or (
            12 if pre_conviction >= 9.0 else
            9  if pre_conviction >= 8.0 else
            6
        )

        eia_data    = all_data.get("eia", {})
        capex_data  = all_data.get("hyperscaler_capex", {})
        nvda_data   = all_data.get("nvda_revenue", {})

        prompt_a = PROMPT_A_TEMPLATE.format(
            ticker=ticker,
            sector=sector,
            regime_mode=regime.get("mode", "NORMAL"),
            threshold=regime.get("conviction_threshold", 7.5),
            pre_scores_json=json.dumps(pre_scores, indent=6),
            empirical_score=shulman_data.get("empirical_score", 0),
            available_datapoints=shulman_data.get("available_datapoints", 0),
            eia_gap=data_gaps.get("eia_gap", "unknown"),
            capex_gap=data_gaps.get("capex_gap", "unknown"),
            nvda_gap=data_gaps.get("nvda_gap", "unknown"),
            shulman_quality_note=shulman_quality_note,
            market_data_json=json.dumps({
                "options_summary": {
                    "current_price": ticker_opts.get("current_price"),
                    "current_iv":    ticker_opts.get("current_iv"),
                    "iv_rank":       ticker_opts.get("iv_rank", {}),
                    "options_flow":  ticker_opts.get("options_flow", {}),
                },
                "regime": {
                    "mode":           regime.get("mode"),
                    "energy_breadth": regime.get("energy_breadth"),
                    "iv_rank_avg":    regime.get("iv_rank_avg"),
                    "yield_curve":    regime.get("yield_curve"),
                    "hy_credit_spread": regime.get("hy_credit_spread"),
                },
            }, indent=6, default=str),
            trigger_articles_json=json.dumps(
                trigger_articles, indent=8, default=str
            ),
            ticker_articles_json=json.dumps(
                [{"title": a.get("title"), "summary": a.get("summary", "")[:200]}
                 for a in ticker_articles[:3]],
                indent=8, default=str
            ),
            thiel_article_count=len(thiel_articles),
            energy_article_count=len(energy_articles),
            salp_article_count=len(salp_articles),
            general_articles_json=json.dumps(
                [{"source": a.get("source"), "title": a.get("title")}
                 for a in (thiel_articles + energy_articles)[:4]],
                indent=8, default=str
            ),
            filing_class=filing_class,
            shulman_begleit=str(shulman_data.get("shulman_bonus", 0) > 0),
            active_actors=", ".join(thiel_data.get("active_actors", [])),
            deep_network=thiel_data.get("deep_network_signal", False),
            thiel_signal_type=thiel_data.get("signal_type", "KEIN_SIGNAL"),
            eia_growth=(
                f"{eia_data.get('growth_yoy'):.1%}"
                if eia_data.get("growth_yoy") is not None else
                "N/A (DATA GAP)"
            ),
            capex_trend=capex_data.get("capex_trend", "unknown"),
            nvda_growth=(
                f"{nvda_data.get('growth_yoy'):.1%}"
                if nvda_data.get("growth_yoy") is not None else
                "N/A (DATA GAP)"
            ),
            contrarian_score=contrarian_data.get("contrarian_score", 0),
            gegenthesen=", ".join(
                contrarian_data.get("gegenthesen_aktiv", [])
            ) or "keine",
            rsi=ticker_opts.get("rsi", 50),
            put_call=ticker_opts.get(
                "options_flow", {}
            ).get("put_call_volume", "N/A"),
            laufzeit_months=laufzeit,
        )

        try:
            # Prompt A: Thesis + Scoring mit Prompt Caching (system-Block gecacht)
            response = client.messages.create(
                model=Config.CLAUDE_MODEL,
                max_tokens=Config.CLAUDE_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": PROMPT_A_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt_a}],
            )
            raw = response.content[0].text.strip()

            if "```" in raw:
                parts = raw.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        raw = part
                        break

            # ── R-01: SCHEMA-VALIDATION + KORREKTER FALLBACK ──────
            # Post-Condition: jeder Pre-Filter-PASS erzeugt immer
            # einen store_signal-Eintrag — auch bei Claude-Fehler.
            try:
                parsed = json.loads(raw)

                # Pflichtfelder prüfen.
                # BUGFIX: "option" gehört NICHT zu Prompt A — es wird erst von
                # Prompt B (nur bei PASS) erzeugt. Es als Pflichtfeld zu fordern
                # ließ JEDE gültige Prompt-A-Antwort als "schema violation"
                # scheitern (0 Cards). Option wird unten als {} vorbelegt.
                required = [
                    "ticker", "conviction_total", "conviction_gate",
                    "scores", "rationale"
                ]
                missing = [f for f in required if f not in parsed]
                if missing:
                    raise ValueError(f"Missing fields: {missing}")

                # Typ + Range-Validierung
                ct = parsed.get("conviction_total", -1)
                if not isinstance(ct, (int, float)):
                    raise ValueError("conviction_total must be numeric")
                if ct < 0 or ct > 10:
                    raise ValueError(f"conviction_total out of range: {ct}")

                result = parsed
                result.setdefault("option", {})   # filled by Prompt B on PASS
                result["schema_valid"] = True

            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.error(
                    f"Claude schema violation {ticker}: {e} | "
                    f"raw[:200]={raw[:200]}"
                )
                # R-4 FIX: store_signal hier NICHT aufrufen — run_daily_analysis
                # ruft es für jedes non-None result bereits auf. Doppelschreibung
                # würde zwei Einträge pro Parse-Fehler in signals erzeugen.
                result = {
                    "ticker":           ticker,
                    "sector":           sector,
                    "conviction_total": 0.0,
                    "conviction_gate":  "CLAUDE_PARSE_FAILED",
                    "schema_valid":     False,
                    "fallback":         True,
                    "fallback_reason":  str(e),
                    "pre_filter_score": pre_score,
                    "rationale": (
                        f"Claude JSON invalid: {e}. "
                        f"Pre-filter score was {pre_score:.1f}. "
                        f"No trade recommendation possible."
                    ),
                    "scores": {}, "option": {},
                    "gegen_szenario": "N/A — parse error",
                    "signal_tags": ["PARSE_ERROR"],
                    "analyzed_at": datetime.utcnow().isoformat(),
                }
                return result

            ok, reason = state_manager.check_portfolio_limits(ticker, sector)
            result["portfolio_check"]  = {"passed": ok, "reason": reason}
            result["pre_filter_score"] = pre_score
            result["analyzed_at"]      = datetime.utcnow().isoformat()

            if not ok:
                result["conviction_gate"] = f"PORTFOLIO_BLOCKED_{reason}"

            # Prompt B: Options-Parameter — NUR bei PASS (spart Tokens bei WATCHLIST/NO_SIGNAL)
            if result.get("conviction_gate") == "PASS":
                try:
                    prompt_b = PROMPT_B_TEMPLATE.format(
                        ticker=ticker,
                        conviction=result.get("conviction_total", 0),
                        threshold=regime.get("conviction_threshold", 7.5),
                        laufzeit_months=result.get("laufzeit_months", laufzeit),
                        top_calls_json=json.dumps(top_calls, indent=4, default=str),
                    )
                    resp_b = client.messages.create(
                        model=Config.CLAUDE_MODEL,
                        max_tokens=1000,
                        system=[{
                            "type": "text",
                            "text": PROMPT_A_SYSTEM,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        messages=[{"role": "user", "content": prompt_b}],
                    )
                    raw_b = resp_b.content[0].text.strip()
                    if "```" in raw_b:
                        for part in raw_b.split("```"):
                            part = part.strip()
                            if part.startswith("json"):
                                part = part[4:].strip()
                            if part.startswith("{"):
                                raw_b = part
                                break
                    parsed_b = json.loads(raw_b)
                    if "option" in parsed_b:
                        result["option"] = parsed_b["option"]
                    if "liquidity_flags" in parsed_b:
                        result["liquidity_flags"] = parsed_b["liquidity_flags"]
                    logger.info(
                        f"Prompt B (options) OK for {ticker}: "
                        f"strike={result.get('option', {}).get('strike_absolute')}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Prompt B failed for {ticker}: {e} — "
                        f"keeping Prompt A option data if present"
                    )

            return result

        except Exception as e:
            logger.error(f"Claude error {ticker}: {e}")
            return None

    def run_daily_analysis(self, all_data: dict, regime: dict,
                           sec_data: dict, state_manager) -> list:
        logger.info("=== Claude Analyzer: starting analysis ===")

        candidates = set(Config.TARGET_TICKERS)

        for cls in sec_data.get("classifications", []):
            if cls["class"] in ["A", "B"]:
                candidates.add(cls["ticker"])

        for article in all_data.get("rss", []):
            for ticker in article.get("tickers", []):
                if ticker in Config.TARGET_TICKERS:
                    candidates.add(ticker)

        results      = []
        claude_calls = 0
        errors       = []

        for ticker in sorted(candidates):
            try:
                result = self.analyze_ticker(
                    ticker, all_data, regime, sec_data, state_manager
                )
                if result:
                    claude_calls += 1
                    conviction   = result.get("conviction_total", 0)
                    gate         = result.get("conviction_gate", "NO_SIGNAL")

                    state_manager.store_signal(
                        ticker, conviction, gate,
                        regime.get("mode", "NORMAL"),
                        result.get("bottleneck_type", "UNKNOWN"),
                        result,
                    )

                    if gate == "PASS":
                        state_manager.store_trading_card(
                            ticker, conviction, gate,
                            result.get("laufzeit_months", 6),
                            result,
                        )
                        results.append(result)
                        logger.info(
                            f"TRADING CARD: {ticker} | "
                            f"{conviction:.2f} | "
                            f"{result.get('laufzeit_months')}M CALL"
                        )

            except Exception as e:
                errors.append(f"{ticker}: {e}")
                logger.error(f"Analysis error {ticker}: {e}")

        state_manager.log_run_stats(
            candidates=len(candidates),
            claude_calls=claude_calls,
            cards_generated=len(results),
            regime_mode=regime.get("mode", "NORMAL"),
            errors=errors,
        )

        logger.info(
            f"=== Analysis done: {len(results)} cards | "
            f"{claude_calls} Claude calls | "
            f"{len(errors)} errors ==="
        )
        return results
