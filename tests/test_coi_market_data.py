"""Tests for market-data metric computation + fixture provider."""
from call_options_intel.market_data import (
    MarketDataProvider, compute_snapshot_from_closes,
)
from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import DEFAULT_FIXTURES


def test_compute_snapshot_uptrend():
    closes = [100 + i * 0.5 for i in range(220)]  # steady uptrend
    vols = [1_000_000] * 220
    snap = compute_snapshot_from_closes("X", closes, vols)
    assert snap.spot == closes[-1]
    assert snap.momentum_3m > 0
    assert snap.above_200dma is True
    assert snap.rsi_14 is not None
    assert snap.drawdown_from_high == 0.0  # always at highs
    assert snap.avg_dollar_volume > 0
    assert snap.complete


def test_compute_snapshot_short_history_flagged():
    snap = compute_snapshot_from_closes("X", [100, 101, 102])
    assert "lt_200d_history" in snap.data_flags
    assert snap.above_200dma is None


def test_compute_snapshot_drawdown():
    closes = [100] * 50 + [80] * 50  # 20% drop then flat
    snap = compute_snapshot_from_closes("X", closes)
    assert snap.drawdown_from_high < -0.15


def test_single_point_safe():
    snap = compute_snapshot_from_closes("X", [100])
    assert "insufficient_price_history" in snap.data_flags
    assert not snap.complete


def test_fixture_provider_known_ticker():
    prov = MarketDataProvider(AppConfig().data_sources, mode="offline",
                              fixtures_dir=DEFAULT_FIXTURES)
    snap = prov.get_snapshot("NVDA")
    assert snap.spot is not None
    assert snap.complete


def test_fixture_provider_unknown_ticker_flagged():
    prov = MarketDataProvider(AppConfig().data_sources, mode="offline",
                              fixtures_dir=DEFAULT_FIXTURES)
    snap = prov.get_snapshot("ZZZZ")
    assert "no_market_data" in snap.data_flags
    assert snap.spot is None


def test_vix_fixture():
    prov = MarketDataProvider(AppConfig().data_sources, mode="offline",
                              fixtures_dir=DEFAULT_FIXTURES)
    assert prov.get_vix() == 17.5
