"""Tests for the options candidate generator (hard filters + ranking)."""
import pytest

from call_options_intel.config_loader import AppConfig
from call_options_intel.scoring import SignalEngine
from call_options_intel.candidates import CandidateGenerator
from call_options_intel.models import (
    UniverseEntry, ThesisVector, MarketSnapshot, OptionContract,
)


@pytest.fixture
def setup():
    cfg = AppConfig()
    engine = SignalEngine(cfg.scoring, cfg.risk)
    gen = CandidateGenerator(engine, cfg.risk)
    entry = UniverseEntry(ticker="NVDA", name="NVIDIA", category="semiconductors",
                          relevance=0.9, thesis_tags=["compute_scarcity"])
    snap = MarketSnapshot(ticker="NVDA", spot=200, momentum_3m=0.15, momentum_6m=0.25,
                          above_200dma=True, rsi_14=55, drawdown_from_high=-0.05,
                          stabilizing=True, hist_vol_annual=0.45)
    ev = engine.evaluate_ticker(entry, ThesisVector(), snap, None, [], ["catalyst"])
    return cfg, engine, gen, entry, snap, ev


def _c(strike, dte, bid, ask, oi=2000, vol=500, iv=0.45, spot=200):
    return OptionContract(ticker="NVDA", expiry="2026-12-18", strike=strike, dte=dte,
                          bid=bid, ask=ask, iv=iv, open_interest=oi, volume=vol, spot=spot)


def test_rejects_near_expiry(setup):
    cfg, engine, gen, entry, snap, ev = setup
    cands, rej = gen.generate(ev, entry, [_c(200, 10, 5, 5.2)], 0.45)
    assert not cands
    assert any(r.reason == "near_expiry_lottery" for r in rej)


def test_rejects_low_open_interest(setup):
    cfg, engine, gen, entry, snap, ev = setup
    cands, rej = gen.generate(ev, entry, [_c(200, 90, 5, 5.2, oi=5)], 0.45)
    assert not cands
    assert any(r.reason == "low_open_interest" for r in rej)


def test_rejects_wide_spread(setup):
    cfg, engine, gen, entry, snap, ev = setup
    cands, rej = gen.generate(ev, entry, [_c(200, 90, 4, 6)], 0.45)  # ~40% spread
    assert not cands
    assert any(r.reason == "wide_spread" for r in rej)


def test_rejects_strike_out_of_zone(setup):
    cfg, engine, gen, entry, snap, ev = setup
    # strike 320 on spot 200 -> moneyness 1.6, outside all zones
    cands, rej = gen.generate(ev, entry, [_c(320, 90, 1.0, 1.05)], 0.45)
    assert not cands
    assert any(r.reason == "strike_out_of_zone" for r in rej)


def test_accepts_good_contract_and_ranks(setup):
    cfg, engine, gen, entry, snap, ev = setup
    contracts = [
        _c(200, 90, 12, 12.5, oi=8000, vol=2000),   # near money, deep liq
        _c(220, 120, 6, 6.3, oi=3000, vol=800),      # slight OTM
    ]
    cands, rej = gen.generate(ev, entry, contracts, 0.45)
    assert len(cands) == 2
    # sorted descending by score
    assert cands[0].final_score >= cands[1].final_score
    c = cands[0]
    assert c.strike_zone in ("near_the_money", "slightly_otm")
    assert c.paper_recommendation.startswith("PAPER ONLY")
    assert c.suggested_expiry_window
    # delta estimation is the data provider's job; here contracts carry no
    # delta, and the generator must handle that gracefully.
    assert c.final_score > 0


def test_event_trade_flag(setup):
    cfg, engine, gen, entry, snap, ev = setup
    cands, rej = gen.generate(ev, entry, [_c(200, 90, 12, 12.5, oi=8000)], 0.45,
                              earnings_in_days=4)
    assert cands[0].is_event_trade
    assert any("EARNINGS" in n for n in cands[0].risk_notes)
