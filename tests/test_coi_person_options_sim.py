"""Tests for the realistic path-based options backtest."""
from datetime import date, timedelta

from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.historical import HistoricalPriceProvider
from call_options_intel.person_intel.options_sim import (
    ExitRules, backtest_signals, simulate_option_trade,
)

E = date(2026, 1, 5)


def _path(slope, bench=0.001):
    def f(t, d):
        n = (d - E).days
        if t in ("QQQ", "SOXX"):
            return 100 * (1 + bench * n)
        return 100 * (1 + slope * n)
    return f


def _sim(slope, iv_on=None, **kw):
    base = dict(ticker="X", entry_date=E, strike=105, dte=60, entry_iv=0.5,
                entry_mid=5.0, price_on=_path(slope), iv_on=iv_on)
    base.update(kw)
    return simulate_option_trade(**base)


# ── exit rules ──────────────────────────────────────────────────────────────
def test_rally_hits_take_profit():
    s = _sim(0.015)
    assert s.exit_reason == "take_profit"
    assert s.option_pnl_pct >= 1.0


def test_crash_hits_stop_loss():
    s = _sim(-0.02)
    assert s.exit_reason == "stop_loss"
    assert -1.0 <= s.option_pnl_pct <= -0.5 + 0.2


def test_flat_decays_to_time_stop_negative():
    s = _sim(0.0)
    assert s.exit_reason in ("time_stop", "dte_stop")
    assert s.option_pnl_pct < 0          # pure theta drag — the proxy missed this


def test_iv_crush_exits_early_with_loss():
    def ivp(t, d):
        return 0.25 if (d - E).days >= 3 else 0.5
    s = _sim(0.005, iv_on=ivp)
    assert s.exit_reason == "iv_crush"
    assert s.days_held == 3
    assert s.option_pnl_pct < 0          # vega hit captured


# ── frictions ───────────────────────────────────────────────────────────────
def test_conservative_fills_buy_high_sell_low():
    s = _sim(0.0)
    assert s.entry_fill > 5.0            # crossed toward the ask on entry
    # exit fill reflects crossing toward the bid (below the repriced mid)
    assert s.exit_fill < s.entry_fill or s.option_pnl_pct < 0


def test_benchmarks_and_no_trade_present():
    s = _sim(0.01)
    assert s.qqq_return is not None and s.soxx_return is not None
    assert s.stock_return is not None
    assert s.no_trade == 0.0


def test_no_entry_price_returns_none():
    assert simulate_option_trade(
        ticker="X", entry_date=E, strike=105, dte=60, entry_iv=0.5,
        entry_mid=5.0, price_on=lambda t, d: None) is None


def test_exit_rules_from_config():
    r = ExitRules.from_config({"options_backtest": {"take_profit": 0.5,
                                                    "stop_loss": 0.25}})
    assert r.take_profit == 0.5 and r.stop_loss == 0.25
    # a stricter take-profit fires sooner
    s = simulate_option_trade(ticker="X", entry_date=E, strike=105, dte=60,
                              entry_iv=0.5, entry_mid=5.0, price_on=_path(0.015),
                              rules=r)
    assert s.exit_reason == "take_profit"


# ── aggregation + benchmark comparison ──────────────────────────────────────
def test_backtest_signals_aggregates_and_skips_rejects():
    rows = [
        {"ticker": "X", "recorded_at": "2026-01-05T12:00:00+00:00", "strike": 105,
         "entry_premium": 5.0, "iv": 0.5, "dte": 60, "label": "top"},
        {"ticker": "X", "recorded_at": "2026-01-05T12:00:00+00:00", "strike": 105,
         "entry_premium": 5.0, "iv": 0.5, "dte": 60, "label": "rejected"},
    ]
    out = backtest_signals(rows, _path(0.015))
    assert out["n"] == 1                 # the rejected row is excluded
    assert "option_pnl" in out and "vs_stock" in out and "vs_qqq" in out
    assert "exit_reasons" in out


def test_realistic_backtest_over_fixture_history():
    # use the bundled PLTR daily fixture as the real path
    prov = HistoricalPriceProvider(mode="offline", fixtures_dir=DEFAULT_FIXTURES)
    rows = [{"ticker": "PLTR", "recorded_at": "2026-01-15T12:00:00+00:00",
             "strike": 210, "entry_premium": 8.0, "iv": 0.55, "dte": 60,
             "label": "top"}]
    out = backtest_signals(rows, prov.price_on, rules=ExitRules())
    assert out["n"] == 1
    assert out["option_pnl"]["n"] == 1
    assert out["vs_stock"]["n"] == 1     # PLTR fixture covers the window
