"""Tests for the empirical cluster→proxy validator (data-gated verdict)."""
from datetime import date
from types import SimpleNamespace

from call_options_intel.person_intel.proxy_validation import validate_proxies


def _proxy_map():
    # two clusters, each with one proxy ticker
    return SimpleNamespace(clusters={
        "power_grid": SimpleNamespace(proxies=[SimpleNamespace(ticker="CEG")]),
        "compute": SimpleNamespace(proxies=[SimpleNamespace(ticker="NVDA")]),
    })


def _price_on(table):
    def fn(t, d):
        return table.get((t, d.isoformat()))
    return fn


AS_OF = date(2026, 6, 27)
START = date(2026, 6, 27).replace(day=29) if False else None  # placeholder


def test_keep_when_proxy_beats_benchmark():
    # window = 180d before AS_OF
    from datetime import timedelta
    start = AS_OF - timedelta(days=180)
    table = {
        ("CEG", start.isoformat()): 100.0, ("CEG", AS_OF.isoformat()): 140.0,   # +40%
        ("NVDA", start.isoformat()): 100.0, ("NVDA", AS_OF.isoformat()): 110.0,  # +10%
        ("QQQ", start.isoformat()): 100.0, ("QQQ", AS_OF.isoformat()): 120.0,    # +20%
    }
    r = validate_proxies(_proxy_map(), _price_on(table), as_of=AS_OF,
                         lookback_days=180, benchmark="QQQ")
    ceg = r["clusters"]["power_grid"][0]
    nvda = r["clusters"]["compute"][0]
    assert ceg["verdict"] == "keep"      # +40% > +20%
    assert nvda["verdict"] == "drop"     # +10% < +20%
    assert r["summary"]["keep"] == 1 and r["summary"]["drop"] == 1


def test_insufficient_without_price_history():
    r = validate_proxies(_proxy_map(), _price_on({}), as_of=AS_OF)
    assert r["summary"]["insufficient"] == 2
    assert all(row["verdict"] == "insufficient"
               for rows in r["clusters"].values() for row in rows)


def test_min_edge_threshold():
    from datetime import timedelta
    start = AS_OF - timedelta(days=180)
    table = {
        ("CEG", start.isoformat()): 100.0, ("CEG", AS_OF.isoformat()): 122.0,   # +22%
        ("NVDA", start.isoformat()): 100.0, ("NVDA", AS_OF.isoformat()): 130.0,  # +30%
        ("QQQ", start.isoformat()): 100.0, ("QQQ", AS_OF.isoformat()): 120.0,    # +20%
    }
    # require a 5-point edge over the benchmark to keep
    r = validate_proxies(_proxy_map(), _price_on(table), as_of=AS_OF,
                         lookback_days=180, benchmark="QQQ", min_edge=0.05)
    assert r["clusters"]["power_grid"][0]["verdict"] == "drop"   # +2% edge < 5%
    assert r["clusters"]["compute"][0]["verdict"] == "keep"      # +10% edge >= 5%
