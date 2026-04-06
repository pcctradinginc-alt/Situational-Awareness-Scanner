"""
scanner/signals/regime_detector.py
Bestimmt Normal- oder Stressmodus basierend auf harten Kennzahlen.
IV-Rank aus Tradier-basierter SQLite-Historie.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from ..utils.config import Config

logger = logging.getLogger(__name__)


class RegimeDetector:

    def detect(self, all_data: dict, state_manager) -> dict:
        energy_data  = all_data.get("energy_breadth", {})
        capex_data   = all_data.get("hyperscaler_capex", {})
        options_data = all_data.get("options", {})

        # Energy Breadth
        energy_breadth = energy_data.get("energy_breadth", 0.5)

        # CapEx Trend aus SQLite-Historie
        capex_trend = state_manager.get_capex_trend()
        if capex_data.get("capex_trend"):
            capex_trend = capex_data["capex_trend"]

        # R-03 FIX: IV-Rank None bei INSUFFICIENT_DATA nicht als 50.0 behandeln
        iv_ranks       = []
        iv_warmup_count = 0
        for ticker, odata in options_data.items():
            iv_rank_info = odata.get("iv_rank", {})
            if not isinstance(iv_rank_info, dict):
                continue
            confidence = iv_rank_info.get("confidence", "")
            iv_val     = iv_rank_info.get("iv_rank")
            if confidence == "INSUFFICIENT_DATA" or iv_val is None:
                iv_warmup_count += 1
            else:
                iv_ranks.append(iv_val)

        if iv_ranks:
            avg_iv_rank   = sum(iv_ranks) / len(iv_ranks)
            iv_confidence = "HIGH" if len(iv_ranks) >= 5 else "LOW"
        else:
            avg_iv_rank   = None  # Kein Default — explizit neutral behandeln
            iv_confidence = "INSUFFICIENT_DATA"
            logger.warning(
                f"IV-Rank: {iv_warmup_count} tickers in warmup — "
                f"regime score set to neutral=5.0"
            )

        # Grid Queue (vereinfacht: EIA als Proxy)
        eia_data     = all_data.get("eia", {})
        eia_growth   = eia_data.get("growth_yoy") or 0
        grid_growth  = min(max(eia_growth, 0), 1)  # Normalisiert 0-1

        # Regime-Stabilitäts-Faktor
        regime_stability = (energy_breadth + grid_growth) / 2

        # Stress-Bedingungen prüfen
        stress_reasons = []
        # R-03: avg_iv_rank kann None sein — kein Stress-Signal bei Warmup
        if avg_iv_rank is not None and avg_iv_rank > Config.IV_RANK_STRESS_THRESHOLD:
            stress_reasons.append(f"IV-Rank {avg_iv_rank:.1f} > {Config.IV_RANK_STRESS_THRESHOLD}")
        if capex_trend == "falling_two_quarters":
            stress_reasons.append("Hyperscaler CapEx falling two quarters")
        if regime_stability < Config.REGIME_STABILITY_STRESS:
            stress_reasons.append(f"Regime stability {regime_stability:.2f} < {Config.REGIME_STABILITY_STRESS}")

        mode     = "STRESS" if stress_reasons else "NORMAL"
        weights  = Config.WEIGHTS_STRESS if mode == "STRESS" else Config.WEIGHTS_NORMAL
        threshold = Config.CONVICTION_STRESS if mode == "STRESS" else Config.CONVICTION_NORMAL

        # R-03: Regime-Score bei None IV-Rank neutral setzen (5.0)
        iv_for_score = avg_iv_rank if avg_iv_rank is not None else 50.0
        regime_score = self._calculate_regime_score(
            energy_breadth, iv_for_score, capex_trend, eia_growth
        )

        result = {
            "mode":               mode,
            "iv_rank_avg":        round(avg_iv_rank, 1) if avg_iv_rank is not None else None,
            "iv_confidence":      iv_confidence,
            "energy_breadth":     round(energy_breadth, 3),
            "capex_trend":        capex_trend,
            "regime_stability":   round(regime_stability, 3),
            "conviction_threshold": threshold,
            "weights":            weights,
            "stress_reasons":     stress_reasons,
            "regime_score":       regime_score,
            "determined_at":      datetime.utcnow().isoformat(),
        }

        state_manager.store_regime(result)

        # Output schreiben
        out = Config.SIGNALS_DIR / "regime.json"
        out.write_text(json.dumps(result, indent=2))

        logger.info(f"Regime: {mode} | IV-Rank: {avg_iv_rank:.1f} | "
                    f"Energy Breadth: {energy_breadth:.2f} | "
                    f"CapEx: {capex_trend} | Score: {regime_score}")
        return result

    def _calculate_regime_score(self, energy_breadth: float,
                                 iv_rank: float, capex_trend: str,
                                 eia_growth: float) -> float:
        score = 5.0  # Neutral-Start

        # Energy Breadth
        if energy_breadth > 0.70:
            score += 2.0
        elif energy_breadth > 0.55:
            score += 1.0
        elif energy_breadth < 0.35:
            score -= 2.0
        elif energy_breadth < 0.45:
            score -= 1.0

        # IV-Rank (niedrig = günstig für Optionskauf)
        if iv_rank < 25:
            score += 1.5
        elif iv_rank < 40:
            score += 0.5
        elif iv_rank > 60:
            score -= 1.5
        elif iv_rank > 50:
            score -= 0.5

        # CapEx-Trend
        capex_map = {
            "rising":              1.5,
            "stable":              0.0,
            "falling":            -1.0,
            "falling_two_quarters":-2.0,
            "unknown":             0.0,
        }
        score += capex_map.get(capex_trend, 0)

        # EIA Energie-Wachstum
        if eia_growth > 0.08:
            score += 1.0
        elif eia_growth > 0.03:
            score += 0.5
        elif eia_growth < 0:
            score -= 0.5

        return round(min(max(score, 1.0), 10.0), 1)
