#!/usr/bin/env python3
"""
generate_fixtures.py
====================
Deterministically generate the OFFLINE market + options fixtures used by the
default scan. Internally consistent: option premia come from Black-Scholes on
each ticker's spot/IV, so estimated greeks and IV-richness checks behave sanely.

This is a DEV tool. The generated JSON is committed so end users never need to
run it. Re-run after editing the universe:

    python scripts/generate_fixtures.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = ROOT / "config" / "ai_infra_universe.yml"
FIX = ROOT / "call_options_intel" / "fixtures"

import sys
sys.path.insert(0, str(ROOT))
from call_options_intel import blackscholes as bs  # noqa: E402

# Tickers intentionally degraded to exercise graceful-degradation paths.
NO_MARKET_DATA = {"TER"}          # omitted from market fixture -> "no_market_data"
THIN_OPTIONS = {"SMCI"}           # low OI + wide spreads -> rejections
WIDE_SPREAD = {"SMCI"}


def main() -> None:
    rng = random.Random(42)
    universe = yaml.safe_load(UNIVERSE.read_text())
    tickers = universe.get("tickers", [])

    market: dict[str, dict] = {}
    options: dict[str, list] = {}

    for row in tickers:
        t = row["ticker"]
        rel = float(row.get("relevance", 0.5))
        if t in NO_MARKET_DATA:
            continue

        # spot: deterministic per ticker
        spot = round(40 + rng.random() * 260, 2)
        # higher-relevance names skew to healthier (but noisy) trend
        bias = (rel - 0.5)
        mom3 = round(rng.uniform(-0.18, 0.30) + bias * 0.25, 4)
        mom6 = round(mom3 + rng.uniform(-0.10, 0.20) + bias * 0.20, 4)
        above_200 = (mom6 + bias) > 0.02
        rsi = round(min(88, max(22, 50 + mom3 * 120 + rng.uniform(-8, 8))), 2)
        drawdown = round(min(0.0, -abs(rng.uniform(0.02, 0.40)) + bias * 0.1), 4)
        stabilizing = drawdown > -0.18 or rng.random() > 0.4
        hist_vol = round(rng.uniform(0.30, 0.70) - bias * 0.05, 4)
        adv = round(spot * rng.uniform(2e6, 4.0e7), 0)

        market[t] = {
            "spot": spot, "momentum_3m": mom3, "momentum_6m": mom6,
            "above_200dma": above_200, "rsi_14": rsi,
            "drawdown_from_high": drawdown, "avg_dollar_volume": adv,
            "hist_vol_annual": hist_vol, "stabilizing": stabilizing,
        }

        # options: grid of DTE x moneyness, priced via BS
        contracts = []
        thin = t in THIN_OPTIONS
        wide = t in WIDE_SPREAD
        base_iv = hist_vol * rng.uniform(0.95, 1.35)  # IV around realised
        for dte in (45, 90, 135, 180):
            for mny in (0.95, 1.00, 1.05, 1.10, 1.20):
                strike = round(spot * mny, 1)
                tau = dte / 365.0
                iv = round(base_iv * rng.uniform(0.92, 1.12), 4)
                mid = bs.call_price(spot, strike, tau, 0.04, iv)
                if mid <= 0.05:
                    continue
                # liquidity scales with relevance and inverse-OTM
                liq = (0.4 + rel) * (1.3 - abs(mny - 1.0))
                oi = int(max(1, liq * rng.uniform(200, 6000)))
                vol = int(max(0, liq * rng.uniform(20, 1500)))
                spread_frac = rng.uniform(0.02, 0.08)
                if thin:
                    oi = int(oi * 0.02)
                    vol = int(vol * 0.02)
                if wide:
                    spread_frac = rng.uniform(0.20, 0.35)
                half = mid * spread_frac / 2.0
                bid = round(max(0.01, mid - half), 2)
                ask = round(mid + half, 2)
                # leave delta None for most (provider estimates via BS); a few set
                contract = {
                    "dte": dte, "strike": strike, "bid": bid, "ask": ask,
                    "last": round(mid, 2), "iv": iv,
                    "open_interest": oi, "volume": vol,
                }
                if mny == 1.00 and dte == 90:
                    contract["delta"] = round(
                        bs.call_delta(spot, strike, tau, 0.04, iv), 4)
                contracts.append(contract)
        options[t] = contracts

    (FIX / "market").mkdir(parents=True, exist_ok=True)
    (FIX / "options").mkdir(parents=True, exist_ok=True)
    (FIX / "market" / "market_fixture.json").write_text(
        json.dumps(market, indent=2, sort_keys=True))
    (FIX / "options" / "options_fixture.json").write_text(
        json.dumps(options, indent=2, sort_keys=True))
    print(f"Wrote market fixture: {len(market)} tickers")
    print(f"Wrote options fixture: {sum(len(v) for v in options.values())} contracts "
          f"across {len(options)} tickers")


if __name__ == "__main__":
    main()
