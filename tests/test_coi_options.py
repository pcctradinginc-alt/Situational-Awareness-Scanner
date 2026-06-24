"""Tests for option contract model + options provider (incl. greek estimation)."""
from call_options_intel.models import OptionContract
from call_options_intel.options_data import OptionsDataProvider
from call_options_intel.config_loader import AppConfig
from call_options_intel.pipeline import DEFAULT_FIXTURES


def test_contract_derived_properties():
    c = OptionContract(ticker="X", expiry="2026-12-18", strike=110, dte=90,
                       bid=4.0, ask=4.4, spot=100)
    assert c.mid == 4.2
    # spread_pct is rounded to 4 dp by the model
    assert abs(c.spread_pct - round(0.4 / 4.2, 4)) < 1e-9
    assert c.moneyness == 1.1


def test_mid_falls_back_to_last():
    c = OptionContract(ticker="X", expiry="2026-12-18", strike=110, dte=90, last=3.3)
    assert c.mid == 3.3
    assert c.spread_pct is None


def test_provider_estimates_missing_delta():
    prov = OptionsDataProvider(AppConfig().data_sources, mode="offline",
                               fixtures_dir=DEFAULT_FIXTURES)
    contracts = prov.get_call_contracts("NVDA", spot=200.0, hist_vol=0.5)
    assert contracts
    est = [c for c in contracts if c.delta_estimated]
    assert est, "expected some estimated deltas"
    for c in est:
        assert 0.0 <= c.delta <= 1.0
        assert c.spot == 200.0


def test_provider_unknown_ticker_empty():
    prov = OptionsDataProvider(AppConfig().data_sources, mode="offline",
                               fixtures_dir=DEFAULT_FIXTURES)
    assert prov.get_call_contracts("ZZZZ", spot=100.0) == []


def test_expiry_dates_in_future():
    from datetime import date
    prov = OptionsDataProvider(AppConfig().data_sources, mode="offline",
                               fixtures_dir=DEFAULT_FIXTURES, today=date(2026, 1, 1))
    contracts = prov.get_call_contracts("NVDA", spot=200.0, hist_vol=0.5)
    for c in contracts:
        assert c.expiry > "2026-01-01"
        assert c.dte > 0
