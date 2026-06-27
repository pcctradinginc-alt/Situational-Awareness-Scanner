"""Tests for warmed-up IV-rank wiring into options scoring (Phase 3.3)."""
from call_options_intel.config_loader import AppConfig
from call_options_intel.models import OptionContract
from call_options_intel.pipeline import DEFAULT_FIXTURES, Pipeline
from call_options_intel.scoring import (
    SignalEngine, _richness_str, iv_richness_assessment,
)
from call_options_intel.person_intel.iv_history import IVHistoryStore


def _cfg():
    c = AppConfig()
    return SignalEngine(c.scoring, c.risk), c.risk


# ── assessment override ─────────────────────────────────────────────────────
def test_iv_rank_overrides_band():
    cfg = AppConfig().risk
    # high real percentile -> extreme regardless of realised-vol proxy
    _, pct, band = iv_richness_assessment(0.5, 0.4, cfg, iv_rank=95)
    assert band == "extreme" and pct == 95
    assert iv_richness_assessment(0.5, 0.4, cfg, iv_rank=10)[2] == "fair"
    assert iv_richness_assessment(0.5, 0.4, cfg, iv_rank=60)[2] == "mild"


def test_no_rank_keeps_realised_proxy():
    cfg = AppConfig().risk
    # unchanged legacy behaviour when no rank supplied
    rich, pct, band = iv_richness_assessment(0.6, 0.4, cfg)
    assert abs(rich - 1.5) < 1e-9 and band in ("elevated", "extreme", "mild")


def test_richness_str_honest():
    assert _richness_str(1.5, 60) == "1.50x realised"
    assert _richness_str(None, 88) == "IV-rank 88/100"
    assert _richness_str(None, None) == "n/a"


# ── engine uses the rank ────────────────────────────────────────────────────
def _liquid_contracts(iv):
    return [OptionContract(ticker="X", expiry="2026-12-18", strike=100, dte=90,
                           bid=4.9, ask=5.1, iv=iv, open_interest=8000, volume=500,
                           delta=0.45, spot=100)]


def test_score_options_env_penalises_high_iv_rank():
    engine, _ = _cfg()
    contracts = _liquid_contracts(0.45)
    hi_score, _, hi_against, hi_pen = engine.score_options_env(contracts, 0.40, iv_rank=96)
    lo_score, _, _, lo_pen = engine.score_options_env(contracts, 0.40, iv_rank=15)
    assert hi_score < lo_score
    assert "extreme_iv" in hi_pen and "extreme_iv" not in lo_pen
    assert any("very rich" in r for r in hi_against)


# ── pipeline integration ────────────────────────────────────────────────────
def test_pipeline_uses_warmed_iv_store(tmp_path):
    store = IVHistoryStore(tmp_path / "iv.jsonl")
    # 25 low historical IVs spanning a real range -> current ATM IV ranks at top
    for i in range(25):
        store.record("NVDA", 0.10 + 0.005 * i)        # 0.10..0.22

    warm_pipe = Pipeline(config=AppConfig(), fixtures_dir=DEFAULT_FIXTURES,
                         iv_history=store)
    cold_pipe = Pipeline(config=AppConfig(), fixtures_dir=DEFAULT_FIXTURES)
    warm_ev = warm_pipe.explain("NVDA")["evaluation"]
    cold_ev = cold_pipe.explain("NVDA")["evaluation"]

    # warmed-up store ranks NVDA's current IV at the top -> flagged very rich
    assert any("very rich" in r for r in warm_ev.reasons_against)
    # and the cold run (realised-vol proxy) should differ
    assert warm_ev.reasons_against != cold_ev.reasons_against


def test_no_store_means_no_rank():
    pipe = Pipeline(config=AppConfig(), fixtures_dir=DEFAULT_FIXTURES)
    assert pipe._iv_rank("NVDA", []) is None
