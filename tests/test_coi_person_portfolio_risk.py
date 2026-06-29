"""Tests for portfolio-level guardrails over the EV-cleared candidates."""
from call_options_intel.person_intel.portfolio_risk import (
    Position, PortfolioLimits, assess_portfolio,
)


def _p(ticker, cluster, delta=0.4, vega=10.0, premium=5.0, contracts=1):
    return Position(ticker=ticker, cluster=cluster, delta=delta, vega=vega,
                    premium=premium, contracts=contracts)


def test_empty_is_ok():
    r = assess_portfolio([])
    assert r["ok"] is True and r["n"] == 0 and r["breaches"] == []


def test_within_limits_is_ok():
    # 3 diversified names → each 33% < 40% single-name, clusters spread → OK
    pos = [_p("NVDA", "compute"), _p("CEG", "power_grid"), _p("CCJ", "nuclear")]
    r = assess_portfolio(pos)
    assert r["ok"] is True, r["breaches"]
    assert r["aggregates"]["net_delta"] == 120.0     # 3 × 0.4 × 100
    assert r["aggregates"]["aggregate_vega"] == 30.0


def test_too_many_positions_breaches():
    pos = [_p(f"T{i}", f"c{i}") for i in range(6)]
    r = assess_portfolio(pos, PortfolioLimits(max_positions=5, max_net_delta=1e9,
                                              max_aggregate_vega=1e9))
    assert r["ok"] is False
    assert any("positions > max" in b for b in r["breaches"])


def test_cluster_count_and_weight_breach():
    # 3 names all in 'compute' → cluster count and weight both exceed defaults
    pos = [_p("NVDA", "compute"), _p("AMD", "compute"), _p("MU", "compute")]
    r = assess_portfolio(pos, PortfolioLimits(max_positions=10, max_net_delta=1e9,
                                              max_aggregate_vega=1e9))
    assert r["ok"] is False
    assert any("cluster «compute»: 3 positions" in b for b in r["breaches"])
    assert any("cluster «compute» weight" in b for b in r["breaches"])


def test_single_name_concentration_breach():
    # one fat name dominates premium notional
    pos = [_p("NVDA", "compute", premium=50.0), _p("CEG", "power_grid", premium=1.0)]
    r = assess_portfolio(pos, PortfolioLimits(max_per_cluster=9, max_cluster_weight=1.1,
                                              max_net_delta=1e9, max_aggregate_vega=1e9))
    assert r["ok"] is False
    assert any("NVDA weight" in b and "single-name" in b for b in r["breaches"])


def test_net_delta_breach():
    pos = [_p(f"T{i}", "compute", delta=0.9, contracts=2) for i in range(3)]
    r = assess_portfolio(pos, PortfolioLimits(max_positions=99, max_per_cluster=99,
                                              max_cluster_weight=1.1,
                                              max_single_name_weight=1.1,
                                              max_net_delta=300, max_aggregate_vega=1e9))
    # 3 × 0.9 × 100 × 2 = 540 > 300
    assert r["ok"] is False
    assert any("net delta" in b for b in r["breaches"])


def test_aggregate_vega_breach():
    pos = [_p(f"T{i}", f"c{i}", vega=200.0) for i in range(3)]
    r = assess_portfolio(pos, PortfolioLimits(max_positions=99, max_net_delta=1e9,
                                              max_aggregate_vega=500))
    assert r["ok"] is False
    assert any("aggregate vega" in b for b in r["breaches"])


def test_limits_from_config():
    lim = PortfolioLimits.from_config({"portfolio": {
        "max_positions": 3, "max_per_cluster": 1, "max_single_name_weight": 0.25,
        "max_cluster_weight": 0.5, "max_net_delta": 100, "max_aggregate_vega": 200}})
    assert lim.max_positions == 3 and lim.max_per_cluster == 1
    assert lim.max_single_name_weight == 0.25 and lim.max_net_delta == 100.0
