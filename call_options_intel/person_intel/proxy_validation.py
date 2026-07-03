"""
proxy_validation.py
===================
Empirically validate the cluster→public-proxy map. A thesis cluster ("power_grid")
maps to listed proxies (CEG, VST, …); but a proxy only earns its place if it has
**historically beaten the benchmark** (QQQ / SOXX) over a real window. Otherwise
the map is storytelling.

For each configured proxy this computes its total return vs the benchmark over a
lookback window and renders a verdict: ``keep`` (beat the benchmark by at least
``min_edge``), ``drop`` (lagged), or ``insufficient`` (no price history yet — no
keep/drop is asserted, exactly like the walk-forward and calibration guards).
Deterministic + injectable (``price_on(ticker, date)``): offline fixtures or the
free Stooq history live.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Optional

PriceOn = Callable[[str, date], Optional[float]]


def _ret(price_on: PriceOn, sym: str, d0: date, d1: date) -> Optional[float]:
    p0 = price_on(sym, d0)
    p1 = price_on(sym, d1)
    if p0 and p1 and p0 > 0:
        return round((p1 - p0) / p0, 4)
    return None


def validate_proxies(proxy_map, price_on: PriceOn, *,
                     as_of: Optional[date] = None, lookback_days: int = 180,
                     benchmark: str = "QQQ", min_edge: float = 0.0) -> dict:
    """Per cluster→proxy: total return vs ``benchmark`` over the window, with a
    keep / drop / insufficient verdict. Asserts nothing without price data."""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)
    bench_ret = _ret(price_on, benchmark, start, as_of)

    clusters: dict[str, list[dict]] = {}
    counts = {"keep": 0, "drop": 0, "insufficient": 0}
    for name in sorted(getattr(proxy_map, "clusters", {})):
        cluster = proxy_map.clusters[name]
        rows: list[dict] = []
        for p in getattr(cluster, "proxies", []) or []:
            pr = _ret(price_on, p.ticker, start, as_of)
            if pr is None or bench_ret is None:
                verdict, edge = "insufficient", None
            else:
                edge = round(pr - bench_ret, 4)
                verdict = "keep" if edge >= min_edge else "drop"
            counts[verdict] += 1
            rows.append({
                "ticker": p.ticker,
                "proxy_return": pr,
                "benchmark_return": bench_ret,
                "edge_vs_benchmark": edge,
                "verdict": verdict,
            })
        if rows:
            clusters[name] = rows

    return {
        "as_of": as_of.isoformat(), "lookback_days": lookback_days,
        "benchmark": benchmark, "min_edge": min_edge,
        "summary": counts, "clusters": clusters,
        "caveat": ("Keep a cluster→proxy only if it historically beat the benchmark "
                   "over the window; 'insufficient' = no price history yet, so no "
                   "keep/drop is asserted. Sample is small offline — run --live for "
                   "a real window before pruning the map."),
    }
