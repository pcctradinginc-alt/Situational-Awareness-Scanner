"""
scanner/utils/config.py
Zentrale Konfiguration. Alle Konstanten, Gewichte, Schwellenwerte.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── API KEYS ────────────────────────────────────────────────
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    TRADIER_API_KEY   = os.environ.get("TRADIER_API_KEY")
    FINNHUB_API_KEY   = os.environ.get("FINNHUB_API_KEY")
    EIA_API_KEY       = os.environ.get("EIA_API_KEY")
    FRED_API_KEY      = os.environ.get("FRED_API_KEY")

    # ── TRADIER ENDPOINTS (Vollzugriff) ─────────────────────────
    TRADIER_BASE    = "https://api.tradier.com/v1"
    TRADIER_HEADERS = {
        "Authorization": f"Bearer {os.environ.get('TRADIER_API_KEY', '')}",
        "Accept": "application/json",
    }

    # ── PFADE ───────────────────────────────────────────────────
    BASE_DIR    = Path(__file__).parent.parent.parent
    DB_PATH     = BASE_DIR / "data" / "scanner.db"
    SIGNALS_DIR = BASE_DIR / "data" / "signals"
    CARDS_DIR   = BASE_DIR / "data" / "cards"
    HIST_DIR    = BASE_DIR / "data" / "historical"
    DASH_DIR    = BASE_DIR / "dashboard"
    CACHE_DIR   = BASE_DIR / "data" / "cache"

    # ── DYNAMISCHE TICKER (neu) ─────────────────────────────────
    DYNAMIC_TICKERS_PATH = BASE_DIR / "data" / "dynamic_tickers.json"

    # ── SCORING SCHWELLENWERTE ──────────────────────────────────
    CONVICTION_NORMAL        = 7.5
    CONVICTION_STRESS        = 8.0
    CONVICTION_WATCHLIST_MIN = 6.5
    PRE_FILTER_THRESHOLD     = 2.0

    # ── REGIME-SCHWELLENWERTE ───────────────────────────────────
    IV_RANK_STRESS_THRESHOLD          = 60.0
    REGIME_STABILITY_STRESS           = 0.4
    CAPEX_FALLING_QUARTERS_FOR_STRESS = 2

    # ── GEWICHTE NORMALMODUS / STRESSMODUS ──────────────────────
    WEIGHTS_NORMAL = { "salp": 0.40, "thiel": 0.14, "shulman": 0.15, "multigate": 0.04, "regime": 0.15, "contrarian": 0.12 }
    WEIGHTS_STRESS = { "salp": 0.50, "thiel": 0.10, "shulman": 0.13, "multigate": 0.03, "regime": 0.12, "contrarian": 0.12 }

    # ── PORTFOLIO LIMITS ────────────────────────────────────────
    MAX_ACTIVE_POSITIONS      = 3
    MAX_CAPITAL_PER_POSITION  = 0.20
    MIN_PORTFOLIO_CONVICTION  = 8.0
    MAX_SAME_SECTOR_POSITIONS = 2

    # ── KATECHON / SHULMAN / CONTRARIAN / GREEKS ───────────────
    KATECHON_BONUS_VALUE       = 0.3
    KATECHON_BONUS_PER_QUARTER = 1
    KATECHON_WINDOW_DAYS       = 30
    KATECHON_MIN_ACTORS        = 2

    SHULMAN_EMPIRICAL_FULL_WEIGHT  = 2
    SHULMAN_EMPIRICAL_BONUS        = 0.3
    SHULMAN_HALF_WEIGHT_MULTIPLIER = 0.5
    SHULMAN_SALP_BEGLEIT_BONUS     = 0.5

    SHULMAN_EIA_GROWTH_THRESHOLD   = 0.05
    SHULMAN_CAPEX_GROWTH_THRESHOLD = 0.10
    SHULMAN_NVDA_GROWTH_THRESHOLD  = 0.20

    CONTRARIAN_BLOCK_THRESHOLD = -3.0
    CONTRARIAN_RSI_HIGH        = 75.0
    CONTRARIAN_RSI_ELEVATED    = 65.0
    CONTRARIAN_HYPE_HIGH       = 0.7

    MIN_DAILY_VOLUME         = 500
    MAX_VEGA_OTM_LEAPS       = 0.15
    MAX_THETA_12M_LEAPS      = -0.05
    MAX_BID_ASK_SPREAD_PCT   = 0.08

    STOP_PREMIUM_LOSS_PCT    = -0.40
    STOP_IV_CRUSH_POINTS     = 15.0

    LAUFZEIT_6M_MAX = 8.0
    LAUFZEIT_9M_MAX = 9.0

    IV_RANK_LOOKBACK_DAYS   = 365
    IV_RANK_MIN_DATAPOINTS  = 30
    IV_RANK_HIGH_CONFIDENCE = 200
    IV_RANK_WARMUP_DEFAULT  = 50.0

    # ── FILING-GEWICHTE (zentral, damit sec_edgar.py + pre_filter konsistent) ──
    FILING_WEIGHT_13F       = 1.0
    FILING_WEIGHT_13F_AMEND = 0.8
    FILING_WEIGHT_SC13D     = 1.5
    FILING_WEIGHT_SC13D_AMD = 1.3
    FILING_WEIGHT_SC13G     = 0.8
    FILING_WEIGHT_SC13G_AMD = 0.6
    FILING_WEIGHT_FORM4     = 1.6   # erhöht: 2-Tage-Frist, deutlich aktueller als 13F
    FILING_WEIGHT_8K        = 1.8   # Material Events — höchste Zeitrelevanz
    FILING_WEIGHT_8K_AMEND  = 1.4

    # ── FRED SERIES IDs ─────────────────────────────────────────
    FRED_SERIES = {
        "IPG2211A2N":   "electric_power_production",
        "INDPRO":       "industrial_production",
        "CPIAUCSL":     "cpi_inflation",
        "T10Y2Y":       "yield_curve_spread",    # 10Y-2Y Spread — Inversion = Stress
        "BAMLH0A0HYM2": "hy_credit_spread",      # High-Yield-Spreads — Kreditstress
    }
    YIELD_CURVE_INVERSION_THRESHOLD = -0.5   # < -50bps = invertiert = Stress
    HY_SPREAD_STRESS_THRESHOLD      = 5.0    # > 500bps = Kreditstress

    # ── OI-DELTA (Unusual Options Activity) ─────────────────────
    OI_SPIKE_THRESHOLD = 0.50   # > 50% OI-Anstieg ggü. 30-Tage-Durchschnitt = unusual

    # ── PRE-FILTER AUTO-THRESHOLD ────────────────────────────────
    PRE_FILTER_THRESHOLD_INITIAL  = 2.0   # Warmup: kaum Filing-Daten
    PRE_FILTER_THRESHOLD_ACTIVE   = 5.0   # Aktiv: regelmäßige 13F-Daten vorhanden
    PRE_FILTER_FILING_COUNT_LIMIT = 50    # Ab dieser Anzahl gespeicherter Holdings → aktiver Threshold

    # ── RATE LIMITS & CACHE ─────────────────────────────────────
    RATE_LIMITS = { "tradier": 60, "finnhub": 60, "yfinance": 30, "sec_edgar": 10, "anthropic": 5, "fred": 120, "eia": 5000 }
    CACHE_EXPIRE = { "yfinance": 21600, "fred": 86400, "eia": 86400, "finnhub": 21600, "rss": 3600, "edgar": 21600, "tradier": 3600 }

    # ── FESTE TICKER (Basis-Pool) ───────────────────────────────
    TARGET_TICKERS = [
        "VST", "CEG", "NRG", "PLTR",
        "NVDA", "TSM", "AVGO",
        "LMT", "RTX",
    ]

    # ── ENERGIE-BREADTH TICKERS ─────────────────────────────────
    ENERGY_TICKERS = [
        "VST", "CEG", "NRG", "NEE", "SO", "D",
        "XEL", "AEE", "ETR", "EXC",
    ]

    # ── RSS, SEC CIK, CLAUDE ────────────────────────────────────
    RSS_FEEDS = {
        "lesswrong":    ("https://www.lesswrong.com/feed.xml",                    0.85),
        "alignment_forum": ("https://www.alignmentforum.org/feed.xml",            0.90),
        "arxiv_cs_ai":  ("https://rss.arxiv.org/rss/cs.AI",                      0.80),
        "ft_tech":      ("https://www.ft.com/technology?format=rss",              0.85),
        "bloomberg_tech": ("https://feeds.bloomberg.com/technology/news.rss",     0.88),
        "reuters_tech": ("https://feeds.reuters.com/reuters/technologyNews",      0.85),
        "eia_news":     ("https://www.eia.gov/rss/news.xml",                      0.80),
        "defenseone":   ("https://www.defenseone.com/rss/all/",                   0.80),
    }

    # CIKs verified against the SEC EDGAR submissions API (2026-06-25). Original
    # values pointed at unrelated, non-13F entities. NOTE: signal strength is
    # weighted per FILING TYPE (FILING_WEIGHT_FORM4/SC13D > 13F), so individuals
    # / growth funds that only file Form 4 / SC 13D-G (Thiel personally, Mithril)
    # automatically score high on timeliness — 13F is the weakest, most lagged
    # signal in this set.
    SEC_CIK_TARGETS = {
        # ── Aschenbrenner universe (13F) ──────────────────────────────
        "situational_awareness_lp":       "0002045724",  # 13F 2026-05-18
        "situational_awareness_partners": "0002038540",  # 13F 2026-05-18 (sister)
        # ── Thiel network ─────────────────────────────────────────────
        "thiel_macro":                    "0001562087",  # 13F 2026-05-15
        "thiel_peter_individual":         "0001211060",  # Form4 2026-03-04, SC13D/G
        "founders_fund_iv":               "0001616081",  # 13F 2026-02-17
        "founders_fund_v":                "0001697733",  # 13F 2026-02-17 (newest vintage)
        "mithril_lp":                     "0001552273",  # SC13D/G + Form4 (no 13F)
        "mithril_ii_lp":                  "0001669609",  # SC13D/G + Form4 (no 13F)
        # ── Adjacent AI-infra smart money (13F) ───────────────────────
        "duquesne_family_office":         "0001536411",  # Druckenmiller; 13F 2026-05-15
        "altimeter_capital":              "0001541617",  # Gerstner; 13F 2026-05-15
        "coatue_management":              "0001135730",  # 13F 2026-05-15
    }

    CLAUDE_MODEL      = "claude-sonnet-4-6"
    CLAUDE_MAX_TOKENS = 4000

    @classmethod
    def validate(cls) -> list:
        missing = []
        required = ["ANTHROPIC_API_KEY", "TRADIER_API_KEY", "FINNHUB_API_KEY"]
        for key in required:
            if not getattr(cls, key):
                missing.append(key)
        return missing

    @classmethod
    def ensure_dirs(cls):
        """Erstellt alle benötigten Ordner inkl. dynamic_tickers.json"""
        for d in [
            cls.SIGNALS_DIR,
            cls.CARDS_DIR,
            cls.HIST_DIR,
            cls.DASH_DIR,
            cls.CACHE_DIR,
            cls.DASH_DIR / "cards",
        ]:
            d.mkdir(parents=True, exist_ok=True)
        
        # Dynamische Ticker-Datei sicherstellen
        cls.DYNAMIC_TICKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not cls.DYNAMIC_TICKERS_PATH.exists():
            default = {
                "tickers": cls.TARGET_TICKERS.copy(),
                "last_updated": "",
                "source": "initial"
            }
            cls.DYNAMIC_TICKERS_PATH.write_text(json.dumps(default, indent=2))

    @classmethod
    def get_all_target_tickers(cls) -> list:
        """Gibt feste + dynamische Ticker zurück (wird jetzt überall verwendet)"""
        base = cls.TARGET_TICKERS.copy()
        
        dynamic_path = cls.DYNAMIC_TICKERS_PATH
        if dynamic_path.exists():
            try:
                data = json.loads(dynamic_path.read_text())
                dynamic = data.get("tickers", [])
                # Duplikate entfernen, Reihenfolge erhalten
                return list(dict.fromkeys(base + dynamic))
            except Exception:
                pass
        return base
