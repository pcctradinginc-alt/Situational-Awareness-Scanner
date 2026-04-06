"""
scanner/sources/data_fetcher.py

FIXES v3:
    EIA: Entfernt facets[location] Filter der nur 1 Monat zurückgab.
         Nutzt jetzt direktere Series-ID für US-Gesamtstrom.
         Fallback auf FRED Series ELEC.GEN.TOTAL-US-99.A wenn EIA leer.

    CAPEX: FRED Series A169RC1Q027SBEA als Ersatz für Finnhub Free Tier.
           US Corporate Capital Expenditure — kostenlos, zuverlässig,
           quartalsweise mit langer Historie.
           Finnhub bleibt als sekundäre Quelle wenn FRED verfügbar.
"""

import json
import logging
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import requests_cache
import feedparser
import yfinance as yf
import finnhub
import pandas as pd
import numpy as np

from ..utils.config import Config
from ..utils.rate_limiter import rate_limiter
from ..utils.ticker_mapper import TickerMapper
from .tradier_client import TradierClient

logger = logging.getLogger(__name__)

Config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
requests_cache.install_cache(
    str(Config.CACHE_DIR / "sa_scanner_cache"),
    expire_after=3600,
)


class DataFetcher:

    def __init__(self, state_manager=None):
        self.state   = state_manager
        self.mapper  = TickerMapper()
        self.tradier = TradierClient()
        self.fh      = finnhub.Client(api_key=Config.FINNHUB_API_KEY or "")
        self.quality = {}

    # ── ENERGY BREADTH ───────────────────────────────────────────

    def get_energy_breadth(self) -> dict:
        logger.info("Fetching energy breadth via yfinance")
        above_200d = 0
        total      = 0
        details    = {}

        for ticker in Config.ENERGY_TICKERS:
            try:
                rate_limiter.wait("yfinance")
                data = yf.Ticker(ticker).history(
                    period="1y", auto_adjust=True
                )
                if len(data) >= 200:
                    current = data["Close"].iloc[-1]
                    ma200   = data["Close"].rolling(200).mean().iloc[-1]
                    above   = bool(current > ma200)
                    details[ticker] = {
                        "current": round(float(current), 2),
                        "ma200":   round(float(ma200), 2),
                        "above":   above,
                    }
                    if above:
                        above_200d += 1
                    total += 1
            except Exception as e:
                logger.warning(f"yfinance {ticker}: {e}")

        breadth = above_200d / total if total > 0 else 0.5
        self.quality["energy_breadth"] = "OK" if total > 5 else "PARTIAL"
        return {
            "energy_breadth":  round(breadth, 3),
            "above_200d":      above_200d,
            "total_checked":   total,
            "details":         details,
            "fetched_at":      datetime.utcnow().isoformat(),
        }

    def get_rsi(self, ticker: str, period: int = 14) -> float:
        try:
            rate_limiter.wait("yfinance")
            data  = yf.Ticker(ticker).history(period="3mo")["Close"]
            if len(data) < period + 1:
                return 50.0
            delta = data.diff()
            gain  = delta.clip(lower=0).rolling(period).mean()
            loss  = (-delta.clip(upper=0)).rolling(period).mean()
            rs    = gain / loss
            rsi   = 100 - (100 / (1 + rs))
            return round(float(rsi.iloc[-1]), 1)
        except Exception as e:
            logger.warning(f"RSI {ticker}: {e}")
            return 50.0

    # ── EIA ELECTRICITY (FIXED v3) ───────────────────────────────

    def get_eia_electricity_growth(self) -> dict:
        """
        FIX v3: Entfernt facets[location][]=US der nur 1 Monat gab.
        Nutzt jetzt EIA v2 Bulk-Endpoint ohne Location-Filter.
        Fallback auf FRED wenn EIA weniger als 13 Monate liefert.
        """
        logger.info("Fetching EIA electricity demand")

        if not Config.EIA_API_KEY:
            self.quality["eia"] = "NO_KEY"
            logger.warning("EIA: No API key — trying FRED fallback")
            return self._get_eia_via_fred()

        # Primärer Versuch: EIA v2 ohne Location-Filter
        result = self._get_eia_direct()
        if result.get("data_gap"):
            logger.info("EIA direct: insufficient data — trying FRED fallback")
            fred_result = self._get_eia_via_fred()
            if not fred_result.get("data_gap"):
                return fred_result
            return result  # Beide fehlgeschlagen — Data Gap zurückgeben

        return result

    def _get_eia_direct(self) -> dict:
        """EIA v2 API ohne Location-Filter."""
        try:
            rate_limiter.wait("eia")
            url    = "https://api.eia.gov/v2/electricity/electric-power-operational-data/data/"
            params = {
                "api_key":              Config.EIA_API_KEY,
                "frequency":            "monthly",
                "data[0]":              "generation",
                "facets[fueltypeid][]": "ALL",
                # KEIN location-Filter — gibt US-Gesamtdaten über alle States
                "sort[0][column]":      "period",
                "sort[0][direction]":   "desc",
                "length":               200,  # Groß genug für 13 Monate alle States
            }
            r    = requests.get(url, params=params, timeout=25)
            r.raise_for_status()
            data = r.json().get("response", {}).get("data", [])

            if not data:
                return {"growth_yoy": None, "empirical_point": 0, "data_gap": True,
                        "reason": "empty_eia_response"}

            # Aggregiere nach Periode über alle States und Fuel-Types
            by_month: dict = {}
            for row in data:
                period = row.get("period", "")
                gen    = row.get("generation")
                if period and gen is not None:
                    try:
                        by_month[period] = by_month.get(period, 0) + float(gen)
                    except (ValueError, TypeError):
                        continue

            sorted_months = sorted(by_month.keys(), reverse=True)

            if len(sorted_months) < 13:
                logger.warning(
                    f"EIA direct: {len(sorted_months)} months — need 13"
                )
                return {"growth_yoy": None, "empirical_point": 0,
                        "data_gap": True, "months": len(sorted_months)}

            latest   = by_month[sorted_months[0]]
            year_ago = by_month[sorted_months[12]]

            if year_ago == 0:
                return {"growth_yoy": None, "empirical_point": 0, "data_gap": True}

            growth         = (latest - year_ago) / year_ago
            shulman_signal = growth > Config.SHULMAN_EIA_GROWTH_THRESHOLD

            logger.info(
                f"EIA direct: growth={growth:.1%} | "
                f"months={len(sorted_months)} | "
                f"empirical_point={1 if shulman_signal else 0}"
            )
            self.quality["eia"] = "OK"
            return {
                "growth_yoy":      round(growth, 4),
                "latest_period":   sorted_months[0],
                "months_available": len(sorted_months),
                "source":          "eia_direct",
                "shulman_signal":  shulman_signal,
                "empirical_point": 1 if shulman_signal else 0,
                "data_gap":        False,
                "fetched_at":      datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"EIA direct error: {e}")
            return {"growth_yoy": None, "empirical_point": 0,
                    "data_gap": True, "error": str(e)}

    def _get_eia_via_fred(self) -> dict:
        """
        FRED Fallback für EIA-Daten.
        Series IPG2211A2N = Electric Power Generation Index
        Gute Näherung für Stromwachstum.
        """
        if not Config.FRED_API_KEY:
            return {"growth_yoy": None, "empirical_point": 0,
                    "data_gap": True, "reason": "no_fred_key"}
        try:
            rate_limiter.wait("fred")
            url    = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                "series_id":         "IPG2211A2N",
                "api_key":           Config.FRED_API_KEY,
                "file_type":         "json",
                "observation_start": (
                    datetime.utcnow() - timedelta(days=400)
                ).strftime("%Y-%m-%d"),
                "sort_order":        "desc",
                "limit":             15,
            }
            r    = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            obs  = r.json().get("observations", [])
            valid = [
                (o["date"], float(o["value"]))
                for o in obs if o["value"] != "."
            ]

            if len(valid) < 13:
                logger.warning(
                    f"FRED EIA fallback: only {len(valid)} obs — DATA GAP"
                )
                return {"growth_yoy": None, "empirical_point": 0,
                        "data_gap": True, "months": len(valid)}

            latest   = valid[0][1]
            year_ago = valid[12][1]
            growth   = (latest - year_ago) / year_ago

            shulman_signal = growth > Config.SHULMAN_EIA_GROWTH_THRESHOLD
            logger.info(
                f"FRED EIA fallback: growth={growth:.1%} | "
                f"empirical_point={1 if shulman_signal else 0}"
            )
            self.quality["eia"] = "OK_FRED_FALLBACK"
            return {
                "growth_yoy":      round(growth, 4),
                "latest_period":   valid[0][0],
                "source":          "fred_ipg2211a2n",
                "shulman_signal":  shulman_signal,
                "empirical_point": 1 if shulman_signal else 0,
                "data_gap":        False,
                "fetched_at":      datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"FRED EIA fallback error: {e}")
            return {"growth_yoy": None, "empirical_point": 0,
                    "data_gap": True, "error": str(e)}

    # ── FRED MAKRO + CAPEX (FIXED v3) ───────────────────────────

    def get_fred_data(self) -> dict:
        logger.info("Fetching FRED macro data")
        if not Config.FRED_API_KEY:
            self.quality["fred"] = "NO_KEY"
            return {"error": "No FRED API key", "data_gap": True}

        results = {}
        series  = {
            "IPG2211A2N": "electric_power_production",
            "INDPRO":     "industrial_production",
            "CPIAUCSL":   "cpi_inflation",
        }

        for series_id, name in series.items():
            try:
                rate_limiter.wait("fred")
                url    = "https://api.stlouisfed.org/fred/series/observations"
                params = {
                    "series_id":         series_id,
                    "api_key":           Config.FRED_API_KEY,
                    "file_type":         "json",
                    "observation_start": (
                        datetime.utcnow() - timedelta(days=400)
                    ).strftime("%Y-%m-%d"),
                    "sort_order":        "desc",
                    "limit":             15,
                }
                r    = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                obs  = r.json().get("observations", [])
                valid = [
                    (o["date"], float(o["value"]))
                    for o in obs if o["value"] != "."
                ]

                if len(valid) >= 13:
                    latest   = valid[0][1]
                    year_ago = valid[12][1]
                    growth   = (latest - year_ago) / year_ago
                    results[name] = {
                        "latest":      round(latest, 2),
                        "year_ago":    round(year_ago, 2),
                        "growth_yoy":  round(growth, 4),
                        "period":      valid[0][0],
                        "data_gap":    False,
                    }
                else:
                    logger.warning(
                        f"FRED {series_id}: {len(valid)} obs — DATA GAP"
                    )
                    results[name] = {
                        "data_gap": True,
                        "obs_count": len(valid),
                    }

            except Exception as e:
                logger.warning(f"FRED {series_id}: {e}")
                results[name] = {"error": str(e), "data_gap": True}

        self.quality["fred"] = "OK" if any(
            not v.get("data_gap") for v in results.values()
            if isinstance(v, dict)
        ) else "DATA_GAP"

        results["fetched_at"] = datetime.utcnow().isoformat()
        return results

    def get_hyperscaler_capex(self) -> dict:
        """
        FIX v3: FRED Series A169RC1Q027SBEA als primäre CapEx-Quelle.
        US Corporate Capital Expenditure — quartalsweise, kostenlos.
        Finnhub bleibt als sekundäre Validierung.
        """
        logger.info("Fetching hyperscaler CapEx (FRED primary + Finnhub secondary)")

        # Primär: FRED US Corporate CapEx
        fred_result = self._get_capex_via_fred()

        # Sekundär: Finnhub für einzelne Ticker (best effort)
        finnhub_result = self._get_capex_via_finnhub()

        # Kombinierter empirical_point
        # FRED gibt den breiteren Trend, Finnhub gibt Einzelticker
        empirical_p = 0
        if not fred_result.get("data_gap") and fred_result.get("growth_yoy"):
            if fred_result["growth_yoy"] > Config.SHULMAN_CAPEX_GROWTH_THRESHOLD:
                empirical_p = 1
                logger.info(
                    f"CapEx empirical_point=1 from FRED: "
                    f"{fred_result['growth_yoy']:.1%}"
                )

        # Capex-Trend aus FRED (zuverlässiger als Finnhub Free)
        capex_trend = fred_result.get("capex_trend", "unknown")

        return {
            "fred_capex":      fred_result,
            "finnhub_capex":   finnhub_result,
            "capex_trend":     capex_trend,
            "avg_growth_yoy":  fred_result.get("growth_yoy"),
            "empirical_point": empirical_p,
            "data_gap":        fred_result.get("data_gap", True),
            "primary_source":  "fred_a169rc1q027sbea",
            "fetched_at":      datetime.utcnow().isoformat(),
        }

    def _get_capex_via_fred(self) -> dict:
        """
        FRED Series A169RC1Q027SBEA:
        Gross Private Domestic Investment: Fixed Investment: Nonresidential:
        Equipment: Information Processing Equipment
        Guter Proxy für Hyperscaler CapEx-Trend.
        """
        if not Config.FRED_API_KEY:
            return {"data_gap": True, "reason": "no_fred_key"}

        try:
            rate_limiter.wait("fred")
            url    = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                # A169RC1Q027SBEA = Information Processing Equipment Investment
                # Proxy für Hyperscaler/Datacenter CapEx
                "series_id":         "A169RC1Q027SBEA",
                "api_key":           Config.FRED_API_KEY,
                "file_type":         "json",
                "observation_start": (
                    datetime.utcnow() - timedelta(days=550)
                ).strftime("%Y-%m-%d"),
                "sort_order":        "desc",
                "limit":             10,
            }
            r    = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            obs  = r.json().get("observations", [])
            valid = [
                (o["date"], float(o["value"]))
                for o in obs if o["value"] != "."
            ]

            if len(valid) < 5:
                # Fallback: PNFIQ (Nonresidential Fixed Investment)
                return self._get_capex_via_fred_fallback()

            latest   = valid[0][1]
            year_ago = valid[4][1] if len(valid) >= 5 else valid[-1][1]
            growth   = (latest - year_ago) / year_ago if year_ago else None

            trend = (
                "rising"  if growth and growth > 0.05 else
                "falling" if growth and growth < -0.05 else
                "stable"
            )

            logger.info(
                f"FRED CapEx (A169RC1Q027SBEA): "
                f"growth={growth:.1%} if growth else 'N/A' | "
                f"trend={trend}"
            )

            return {
                "growth_yoy":  round(growth, 4) if growth else None,
                "capex_trend": trend,
                "period":      valid[0][0],
                "series":      "A169RC1Q027SBEA",
                "data_gap":    False,
            }

        except Exception as e:
            logger.warning(f"FRED CapEx primary error: {e}")
            return self._get_capex_via_fred_fallback()

    def _get_capex_via_fred_fallback(self) -> dict:
        """Fallback: FRED PNFIQ — Nonresidential Fixed Investment."""
        if not Config.FRED_API_KEY:
            return {"data_gap": True}
        try:
            rate_limiter.wait("fred")
            url    = "https://api.stlouisfed.org/fred/series/observations"
            params = {
                "series_id":         "PNFIQ",
                "api_key":           Config.FRED_API_KEY,
                "file_type":         "json",
                "observation_start": (
                    datetime.utcnow() - timedelta(days=550)
                ).strftime("%Y-%m-%d"),
                "sort_order":        "desc",
                "limit":             8,
            }
            r    = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            obs  = r.json().get("observations", [])
            valid = [
                (o["date"], float(o["value"]))
                for o in obs if o["value"] != "."
            ]

            if len(valid) < 5:
                return {"data_gap": True, "obs_count": len(valid)}

            latest   = valid[0][1]
            year_ago = valid[4][1]
            growth   = (latest - year_ago) / year_ago if year_ago else None
            trend    = (
                "rising"  if growth and growth > 0.03 else
                "falling" if growth and growth < -0.03 else
                "stable"
            )

            logger.info(f"FRED CapEx fallback (PNFIQ): {growth:.1%} | {trend}")
            return {
                "growth_yoy":  round(growth, 4) if growth else None,
                "capex_trend": trend,
                "series":      "PNFIQ",
                "data_gap":    False,
            }
        except Exception as e:
            logger.warning(f"FRED CapEx fallback error: {e}")
            return {"data_gap": True, "error": str(e)}

    def _get_capex_via_finnhub(self) -> dict:
        """Finnhub als sekundäre CapEx-Quelle (best effort)."""
        results = {}
        for ticker in ["MSFT", "GOOGL", "AMZN", "META", "NVDA"]:
            try:
                rate_limiter.wait("finnhub")
                data   = self.fh.company_basic_financials(ticker, "all")
                series = (data.get("series", {}) or {}).get("annual", {}) or {}
                capex  = series.get("capitalExpenditures", []) or []

                if not capex or len(capex) < 2:
                    results[ticker] = {"data_gap": True}
                    continue

                recent   = sorted(capex, key=lambda x: x.get("period", ""), reverse=True)
                latest   = recent[0].get("v", 0) or 0
                previous = recent[1].get("v", 0) or 1
                growth   = (latest - previous) / abs(previous)
                results[ticker] = {
                    "growth_yoy": round(growth, 4),
                    "data_gap":   False,
                }
            except Exception:
                results[ticker] = {"data_gap": True}

        valid = [v["growth_yoy"] for v in results.values()
                 if not v.get("data_gap") and v.get("growth_yoy") is not None]
        return {
            "by_ticker":    results,
            "valid_count":  len(valid),
            "avg_growth":   round(sum(valid)/len(valid), 4) if valid else None,
            "data_gap":     len(valid) == 0,
        }

    # ── NVDA REVENUE ─────────────────────────────────────────────

    def get_nvda_revenue_growth(self) -> dict:
        """R6: Schwellenwert 10% statt 20%."""
        try:
            rate_limiter.wait("finnhub")
            data   = self.fh.company_basic_financials("NVDA", "all")
            series = (data.get("series", {}) or {}).get("annual", {}) or {}
            rev    = series.get("revenue", []) or []

            if not rev or len(rev) < 2:
                logger.warning(
                    f"NVDA Revenue: {'empty' if not rev else len(rev)} "
                    f"quarters — DATA GAP"
                )
                return {"growth_yoy": None, "empirical_point": 0, "data_gap": True}

            recent   = sorted(rev, key=lambda x: x.get("period", ""), reverse=True)
            latest   = recent[0].get("v", 0) or 0
            previous = recent[1].get("v", 0) or 0

            if previous == 0:
                return {"growth_yoy": None, "empirical_point": 0, "data_gap": True}

            growth      = (latest - previous) / abs(previous)
            THRESHOLD   = 0.10  # R6 Fix: 10% statt 20%
            empirical_p = 1 if growth > THRESHOLD else 0

            logger.info(
                f"NVDA Revenue: {growth:.1%} | "
                f"threshold={THRESHOLD:.0%} | "
                f"empirical_point={empirical_p}"
            )
            return {
                "growth_yoy":      round(growth, 4),
                "empirical_point": empirical_p,
                "threshold_used":  THRESHOLD,
                "data_gap":        False,
            }

        except Exception as e:
            logger.warning(f"NVDA revenue: {e}")
            return {"growth_yoy": None, "empirical_point": 0,
                    "data_gap": True, "error": str(e)}

    # ── RSS FEEDS (R7: korrigiertes Credibility-Scoring) ─────────

    THIEL_KEYWORDS = [
        "Peter Thiel", "Katechon", "Sovereign AI",
        "Founders Fund", "Thiel Capital", "Anduril",
        "Antichrist", "permanent crisis", "Palmer Luckey",
        "David Sacks", "J.D. Vance", "Alex Karp",
    ]
    SHULMAN_KEYWORDS = [
        "Carl Shulman", "doubling times", "compute overhang",
        "intelligence explosion", "robot doublings",
        "algorithmic efficiency", "compute substitut",
    ]
    CONTRARIAN_KEYWORDS = [
        "AI winter", "software overhang", "energy abundance",
        "scaling plateau", "efficiency breakthrough",
        "AI regulation", "AI moratorium", "AI ban",
    ]
    BOTTLENECK_COMPUTE = [
        "Nvidia", "H100", "H200", "GPU shortage",
        "compute capacity", "TSMC", "chip demand",
    ]
    BOTTLENECK_ENERGY = [
        "power demand", "grid capacity", "SMR", "nuclear AI",
        "data center energy", "hyperscaler power",
        "electricity AI", "energy infrastructure",
    ]
    SALP_KEYWORDS = [
        "Situational Awareness", "Leopold Aschenbrenner",
        "SALP", "AI safety fund", "Decade Ahead",
    ]

    def fetch_rss(self) -> list:
        """R7: Credibility-dominantes Scoring."""
        logger.info("Fetching RSS feeds")
        results = []
        cutoff  = datetime.utcnow() - timedelta(hours=36)

        for feed_name, (url, credibility) in Config.RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    try:
                        pub = datetime(*entry.published_parsed[:6])
                    except Exception:
                        continue
                    if pub < cutoff:
                        continue

                    text    = f"{entry.title} {entry.get('summary', '')}"
                    tickers = self.mapper.extract_tickers_from_text(text)

                    signals = {
                        "thiel":              any(k.lower() in text.lower() for k in self.THIEL_KEYWORDS),
                        "shulman":            any(k.lower() in text.lower() for k in self.SHULMAN_KEYWORDS),
                        "contrarian":         any(k.lower() in text.lower() for k in self.CONTRARIAN_KEYWORDS),
                        "bottleneck_compute": any(k.lower() in text.lower() for k in self.BOTTLENECK_COMPUTE),
                        "bottleneck_energy":  any(k.lower() in text.lower() for k in self.BOTTLENECK_ENERGY),
                        "salp":               any(k.lower() in text.lower() for k in self.SALP_KEYWORDS),
                    }

                    if not any(signals.values()):
                        continue

                    signal_count = sum(signals.values())
                    # R7: Credibility-dominantes Scoring
                    quality_score = credibility ** 2 * (1 + math.log(signal_count + 1))

                    results.append({
                        "source":           feed_name,
                        "credibility":      credibility,
                        "title":            entry.title,
                        "summary":          entry.get("summary", "")[:500],
                        "url":              entry.link,
                        "published":        pub.isoformat(),
                        "signals":          signals,
                        "signal_count":     signal_count,
                        "tickers":          tickers,
                        "quality_score":    round(quality_score, 4),
                        "weighted_relevance": quality_score,
                    })

            except Exception as e:
                logger.warning(f"RSS {feed_name}: {e}")

        results.sort(key=lambda x: x["quality_score"], reverse=True)
        self.quality["rss"] = f"{len(results)} articles"
        logger.info(
            f"RSS: {len(results)} relevant articles | "
            f"top source: {results[0]['source'] if results else 'none'}"
        )
        return results

    # ── OPTIONS DATA ─────────────────────────────────────────────

    def fetch_options_data(self, state_manager,
                           laufzeit_months: int = 6) -> dict:
        logger.info(f"Fetching Tradier option data ({laufzeit_months}M)")
        results = {}
        for ticker in Config.TARGET_TICKERS:
            try:
                data = self.tradier.analyze_ticker_options(
                    ticker, laufzeit_months, state_manager
                )
                data["rsi"] = self.get_rsi(ticker)
                results[ticker] = data
            except Exception as e:
                logger.error(f"Options {ticker}: {e}")
                results[ticker] = {"error": str(e)}
        self.quality["tradier"] = "OK"
        return results

    # ── MAIN ORCHESTRATION ───────────────────────────────────────

    def fetch_all(self, state_manager, laufzeit_months: int = 6) -> dict:
        logger.info("=== DataFetcher: starting full fetch ===")
        Config.ensure_dirs()
        all_data = {}
        errors   = []

        # 1. Energy Breadth
        try:
            all_data["energy_breadth"] = self.get_energy_breadth()
        except Exception as e:
            errors.append(f"energy_breadth: {e}")
            all_data["energy_breadth"] = {"energy_breadth": 0.5, "data_gap": True}

        # 2. EIA (FIXED v3)
        try:
            all_data["eia"] = self.get_eia_electricity_growth()
        except Exception as e:
            errors.append(f"eia: {e}")
            all_data["eia"] = {"growth_yoy": None, "empirical_point": 0, "data_gap": True}

        # 3. FRED Makro
        try:
            all_data["fred"] = self.get_fred_data()
        except Exception as e:
            errors.append(f"fred: {e}")
            all_data["fred"] = {"data_gap": True}

        # 4. CapEx (FIXED v3: FRED primary)
        try:
            all_data["hyperscaler_capex"] = self.get_hyperscaler_capex()
        except Exception as e:
            errors.append(f"capex: {e}")
            all_data["hyperscaler_capex"] = {
                "capex_trend": "unknown", "empirical_point": 0, "data_gap": True
            }

        # 5. NVDA Revenue
        try:
            all_data["nvda_revenue"] = self.get_nvda_revenue_growth()
        except Exception as e:
            errors.append(f"nvda: {e}")
            all_data["nvda_revenue"] = {"empirical_point": 0, "data_gap": True}

        # 6. RSS
        try:
            all_data["rss"] = self.fetch_rss()
        except Exception as e:
            errors.append(f"rss: {e}")
            all_data["rss"] = []

        # 7. Options (Tradier)
        try:
            all_data["options"] = self.fetch_options_data(
                state_manager, laufzeit_months
            )
        except Exception as e:
            errors.append(f"options: {e}")
            all_data["options"] = {}

        # 8. Shulman Empirical Score
        eia_point   = all_data["eia"].get("empirical_point", 0)
        capex_point = all_data["hyperscaler_capex"].get("empirical_point", 0)
        nvda_point  = all_data["nvda_revenue"].get("empirical_point", 0)
        empirical   = eia_point + capex_point + nvda_point

        eia_gap   = all_data["eia"].get("data_gap", False)
        capex_gap = all_data["hyperscaler_capex"].get("data_gap", False)
        nvda_gap  = all_data["nvda_revenue"].get("data_gap", False)

        all_data["shulman_empirical_score"] = empirical
        all_data["shulman_data_gaps"] = {
            "eia_gap":   eia_gap,
            "capex_gap": capex_gap,
            "nvda_gap":  nvda_gap,
            "any_gap":   any([eia_gap, capex_gap, nvda_gap]),
            "all_gaps":  all([eia_gap, capex_gap, nvda_gap]),
        }

        # 9. Quality Report
        all_data["data_quality"] = {
            **self.quality,
            "errors":                  errors,
            "shulman_empirical_score": empirical,
            "shulman_data_gaps":       all_data["shulman_data_gaps"],
            "fetched_at":              datetime.utcnow().isoformat(),
        }

        self._write_outputs(all_data)

        logger.info(
            f"=== DataFetcher done | "
            f"Shulman Empirical: {empirical}/3 | "
            f"EIA={not eia_gap} CapEx={not capex_gap} NVDA={not nvda_gap} | "
            f"Errors: {len(errors)} ==="
        )
        return all_data

    def _write_outputs(self, all_data: dict):
        sig = Config.SIGNALS_DIR
        outputs = {
            "market_regime.json": {
                "energy_breadth":    all_data.get("energy_breadth", {}),
                "hyperscaler_capex": all_data.get("hyperscaler_capex", {}),
                "fred":              all_data.get("fred", {}),
                "eia":               all_data.get("eia", {}),
            },
            "options_data.json":    all_data.get("options", {}),
            "energy_data.json":     all_data.get("eia", {}),
            "rss_signals.json":     all_data.get("rss", []),
            "fundamentals.json": {
                "hyperscaler_capex": all_data.get("hyperscaler_capex", {}),
                "nvda_revenue":      all_data.get("nvda_revenue", {}),
            },
            "data_quality.json":    all_data.get("data_quality", {}),
            "shulman_empirical.json": {
                "score":       all_data.get("shulman_empirical_score", 0),
                "eia_point":   all_data["eia"].get("empirical_point", 0),
                "capex_point": all_data["hyperscaler_capex"].get("empirical_point", 0),
                "nvda_point":  all_data["nvda_revenue"].get("empirical_point", 0),
                "data_gaps":   all_data.get("shulman_data_gaps", {}),
            },
        }
        for filename, data in outputs.items():
            try:
                (sig / filename).write_text(
                    json.dumps(data, indent=2, default=str)
                )
            except Exception as e:
                logger.error(f"Write {filename}: {e}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    logging.basicConfig(level=logging.INFO)
    from scanner.utils.state_manager import StateManager
    with StateManager() as sm:
        fetcher  = DataFetcher(sm)
        all_data = fetcher.fetch_all(sm)
        print(f"\nShulman Empirical: {all_data['shulman_empirical_score']}/3")
        print(f"EIA: {all_data['eia'].get('growth_yoy')}")
        print(f"CapEx: {all_data['hyperscaler_capex'].get('capex_trend')}")
