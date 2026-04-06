"""
scanner/signals/shulman_layer.py

FIXES v2:
    R3: Explizite Unterscheidung zwischen DATA GAP und echtem Negativsignal.
        "Daten fehlen" ≠ "Schwellenwert verfehlt"
        Wenn Daten fehlen: Shulman-Gewicht auf 25% (statt 50%) reduziert
        und Data-Gap-Flag im Output gesetzt.
        Wenn Daten vorhanden aber Score < Schwellenwert: normales Signal.
"""

import logging
from datetime import datetime

from ..utils.config import Config

logger = logging.getLogger(__name__)

SHULMAN_FORUM_KEYWORDS = [
    "doubling times", "compute-overhang", "intelligence explosion",
    "recursive self-improvement", "automated research",
    "robot doublings", "scaling laws", "algorithmic progress",
]

SEKTOR_WEIGHTS = {
    "energy_infrastructure": 0.35,
    "compute_hardware":       0.25,
    "physical_ai":            0.20,
    "sovereign_ai_defense":   0.12,
    "cyber_bio_nuclear":      0.08,
}


class ShulmanLayer:

    def evaluate(self, all_data: dict, ticker: str, sector: str) -> dict:

        # ── QUANTITATIVE PROXIES ──────────────────────────────────
        eia_point   = all_data.get("eia", {}).get("empirical_point", 0)
        capex_point = all_data.get("hyperscaler_capex", {}).get("empirical_point", 0)
        nvda_point  = all_data.get("nvda_revenue", {}).get("empirical_point", 0)

        empirical_score = eia_point + capex_point + nvda_point

        # ── R3 FIX: DATA GAP TRACKING ────────────────────────────
        # Unterscheidung: fehlen Daten oder ist der Score wirklich 0?
        data_gaps = all_data.get("shulman_data_gaps", {})
        eia_gap   = data_gaps.get("eia_gap", False)
        capex_gap = data_gaps.get("capex_gap", False)
        nvda_gap  = data_gaps.get("nvda_gap", False)
        any_gap   = data_gaps.get("any_gap", False)
        all_gaps  = data_gaps.get("all_gaps", False)

        # Anzahl der Datenpunkte mit echten Daten (nicht Gap)
        available_datapoints = sum([
            not eia_gap,
            not capex_gap,
            not nvda_gap,
        ])

        # R3: Logging der Ursache für Score 0
        if empirical_score == 0:
            if all_gaps:
                logger.warning(
                    f"Shulman [{ticker}]: empirical=0/3 — "
                    f"ALL DATA GAPS (no API data available). "
                    f"This is a data availability issue, NOT a negative signal."
                )
            elif any_gap:
                gap_list = []
                if eia_gap:   gap_list.append("EIA")
                if capex_gap: gap_list.append("CapEx")
                if nvda_gap:  gap_list.append("NVDA")
                logger.warning(
                    f"Shulman [{ticker}]: empirical=0/3 — "
                    f"DATA GAPS: {gap_list} | "
                    f"Available: {available_datapoints}/3 datapoints. "
                    f"Score 0 may reflect missing data, not negative signal."
                )
            else:
                logger.info(
                    f"Shulman [{ticker}]: empirical=0/3 — "
                    f"All data available, thresholds not met. "
                    f"This IS a genuine negative signal."
                )

        # ── GEWICHTS-ANPASSUNG (R3 FIX) ──────────────────────────
        # Drei Stufen statt zwei:
        # Score >= 2 + Daten verfügbar: volles Gewicht
        # Score < 2 + Daten verfügbar: halbes Gewicht (echtes Signal)
        # Daten fehlen (Gap): Viertel-Gewicht (Unsicherheit, kein Signal)

        # ── R-04 FIX: GEWICHTS-ANPASSUNG ─────────────────────────
        # Drei Stufen mit klarer Semantik:
        # 1. Score >= 2 + Daten verfügbar: volles Gewicht (echtes Signal)
        # 2. Daten fehlen (any_gap, score=0): Gewicht auf 0 — kein Signal
        #    KEIN PreFilter-Malus — Data Gap ist neutral, nicht negativ
        # 3. Daten verfügbar, Schwellenwert verfehlt: halbes Gewicht

        if empirical_score >= Config.SHULMAN_EMPIRICAL_FULL_WEIGHT:
            weight_modifier    = 1.0
            weight_reason      = "FULL_WEIGHT_empirical_confirmed"
            conviction_bonus   = (
                Config.SHULMAN_EMPIRICAL_BONUS
                if empirical_score >= 3 else 0.0
            )
        elif all_gaps:
            # R-04 KORREKTUR: 0.0 statt 0.25 — kein Einfluss bei vollständigem Gap
            # KEIN PreFilter-Malus — fehlende API-Daten sind nicht bearish
            weight_modifier    = 0.0
            weight_reason      = "ZERO_WEIGHT_complete_data_unavailability"
            conviction_bonus   = 0.0
            logger.warning(
                f"Shulman [{ticker}]: weight=0.0 — complete data gap. "
                f"Signal neutralized, NOT treated as negative signal."
            )
        elif any_gap and empirical_score == 0:
            # Teilweise Daten fehlen, Score 0 — Unsicherheit, stark reduziert
            weight_modifier    = 0.25
            weight_reason      = "QUARTER_WEIGHT_partial_gap_score_zero"
            conviction_bonus   = 0.0
        else:
            # Daten verfügbar, Schwellenwert nicht erreicht — echtes Negativsignal
            weight_modifier    = 0.5
            weight_reason      = "HALF_WEIGHT_threshold_not_met"
            conviction_bonus   = 0.0

        # ── QUALITATIVE EXTRAKTION AUS RSS ───────────────────────
        rss_articles     = all_data.get("rss", [])
        all_text         = " ".join([
            a.get("title", "") + " " + a.get("summary", "")
            for a in rss_articles
            if a.get("signals", {}).get("shulman")
        ]).lower()

        forum_keywords_found = [
            kw for kw in SHULMAN_FORUM_KEYWORDS
            if kw.lower() in all_text
        ]
        shulman_articles     = [
            a for a in rss_articles
            if a.get("signals", {}).get("shulman")
        ]
        ige_language         = any(
            k in all_text for k in [
                "intelligence explosion",
                "recursive self-improvement",
                "automated research",
            ]
        )
        doubling_language    = any(
            k in all_text for k in [
                "doubling times", "robot doublings", "doubling rate"
            ]
        )

        # ── SALP-BEGLEITTEXT-BONUS ───────────────────────────────
        shulman_bonus = all_data.get(
            "shulman_empirical", {}
        ).get("salp_begleit_bonus", 0.0)

        # ── SEKTOR-MULTIPLIKATOR ─────────────────────────────────
        sektor_mult = SEKTOR_WEIGHTS.get(sector, 0.10)

        # ── SHULMAN SUB-SCORE (1-10) ─────────────────────────────
        base_score = 1.0

        # Empirische Punkte — nur aus verfügbaren Daten
        if available_datapoints > 0:
            # Normalisiere auf verfügbare Datenpunkte
            normalized_empirical = (
                empirical_score / available_datapoints
                if available_datapoints > 0 else 0
            )
            base_score += normalized_empirical * 4.0  # Max +4 bei perfektem Score
        
        # Qualitative Signale (max +2)
        if forum_keywords_found:
            base_score += min(len(forum_keywords_found) * 0.5, 1.5)
        if ige_language:
            base_score += 0.5

        # Data Gaps reduzieren den Score leicht
        if all_gaps:
            base_score = min(base_score, 3.0)  # Cap bei Datenlücken

        shulman_score = round(min(base_score, 10.0), 1)

        logger.info(
            f"Shulman [{ticker}]: score={shulman_score} | "
            f"empirical={empirical_score}/3 | "
            f"available_datapoints={available_datapoints}/3 | "
            f"weight_mod={weight_modifier} ({weight_reason}) | "
            f"forum_kw={len(forum_keywords_found)} | "
            f"gaps: EIA={eia_gap} CapEx={capex_gap} NVDA={nvda_gap}"
        )

        return {
            "shulman_score":          shulman_score,
            "empirical_score":        empirical_score,
            "weight_modifier":        weight_modifier,
            "weight_reason":          weight_reason,
            "conviction_bonus":       conviction_bonus,
            "shulman_bonus":          shulman_bonus,
            # Quantitative Proxies mit Gap-Info
            "eia_empirical_point":    eia_point,
            "capex_empirical_point":  capex_point,
            "nvda_empirical_point":   nvda_point,
            "available_datapoints":   available_datapoints,
            # R3: Data Gap Flags
            "data_gaps": {
                "eia_gap":    eia_gap,
                "capex_gap":  capex_gap,
                "nvda_gap":   nvda_gap,
                "any_gap":    any_gap,
                "all_gaps":   all_gaps,
            },
            # Qualitativ
            "qualitative": {
                "forum_keywords_found": forum_keywords_found,
                "shulman_articles":     len(shulman_articles),
                "ige_language":         ige_language,
                "doubling_language":    doubling_language,
                "confidence":           (
                    "LOW" if all_gaps else
                    "MEDIUM" if any_gap else
                    "DATA_DRIVEN"
                ),
            },
            "sektor_multiplikator":  sektor_mult,
            "sektor":                sector,
            # Nicht messbare Metriken — explizit None
            "robot_doublings_verified": None,
            "flops_per_dollar_growth":  None,
            "ige_loop_confirmed":       None,
            "evaluated_at":             datetime.utcnow().isoformat(),
        }
