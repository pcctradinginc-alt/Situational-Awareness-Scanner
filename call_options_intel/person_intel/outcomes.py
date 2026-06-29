"""
outcomes.py
===========
Goal I — outcome learning that can be trusted, not a backtest that flatters.

  * **append-only** signal store that records REJECTED candidates too — so we can
    later measure what the system correctly avoided, not just its picks;
  * outcomes at **7 / 14 / 30 / 60 / 90 / 180** days;
  * buckets by **score / source / thesis-cluster / regime**;
  * benchmark every signal against the **underlying, QQQ and SOXX**;
  * a **walk-forward + minimum-sample guard**: ``edge_claim`` stays
    ``insufficient_*`` until there is an out-of-sample fold with enough matured
    signals. No profitability is asserted before that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("coi.person.outcomes")

HORIZONS = (7, 14, 30, 60, 90, 180)
BENCHMARKS = ("QQQ", "SOXX")

# price_fn(ticker, horizon_days) -> price at that horizon after the signal, or None
PriceFn = Callable[[str, int], Optional[float]]
# benchmark_ret_fn(symbol, horizon_days) -> benchmark RETURN over that horizon
BenchmarkFn = Callable[[str, int], Optional[float]]
# price_at(ticker, target_date) -> close on/before that date (date-aware path)
PriceAtFn = Callable[[str, date], Optional[float]]


@dataclass
class OutcomeStore:
    """Append-only JSONL store. Records accepted AND rejected signals."""
    path: Path

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("recorded_at",
                       datetime.now(timezone.utc).isoformat(timespec="seconds"))
        row.setdefault("label", "candidate")        # candidate | top | rejected
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def record_many(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            self.record(r)
            n += 1
        return n

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed outcome row")
        return out

    # ── evaluate across horizons ────────────────────────────────────────
    def evaluate(
        self, price_fn: Optional[PriceFn] = None,
        benchmark_fn: Optional[BenchmarkFn] = None,
        as_of: Optional[date] = None, price_at: Optional[PriceAtFn] = None,
    ) -> list[dict]:
        """Annotate matured signals with realised returns.

        Two pricing modes:
          * ``price_at(ticker, date)`` — DATE-AWARE: uses the close at
            ``recorded_date + horizon`` and computes benchmark returns from the
            same provider. This is the realistic path (Phase 4).
          * ``price_fn(ticker, horizon)`` — the simpler injectable path (tests /
            current-spot approximation), with an optional ``benchmark_fn``.
        """
        as_of = as_of or date.today()
        results: list[dict] = []
        for rec in self.load():
            try:
                rec_date = datetime.fromisoformat(rec["recorded_at"]).date()
            except Exception:
                continue
            age = (as_of - rec_date).days
            entry_spot = rec.get("entry_spot")
            for h in HORIZONS:
                if age < h:
                    continue                         # not matured at this horizon
                target = rec_date + timedelta(days=h)
                if price_at is not None:
                    px = price_at(rec["ticker"], target)
                elif price_fn is not None:
                    px = price_fn(rec["ticker"], h)
                else:
                    px = None
                if px is None or not entry_spot:
                    continue
                und_ret = (px - entry_spot) / entry_spot
                benches: dict[str, Optional[float]] = {}
                excess: dict[str, Optional[float]] = {}
                if price_at is not None:
                    for sym in BENCHMARKS:
                        b_entry = price_at(sym, rec_date)
                        b_now = price_at(sym, target)
                        b = ((b_now - b_entry) / b_entry
                             if (b_entry and b_now) else None)
                        benches[sym] = (round(b, 4) if b is not None else None)
                        excess[sym] = (round(und_ret - b, 4) if b is not None else None)
                elif benchmark_fn:
                    for sym in BENCHMARKS:
                        b = benchmark_fn(sym, h)
                        benches[sym] = (round(b, 4) if b is not None else None)
                        excess[sym] = (round(und_ret - b, 4) if b is not None else None)
                results.append({
                    **rec, "horizon": h, "age_days": age, "eval_price": px,
                    "underlying_return": round(und_ret, 4),
                    "benchmark_returns": benches,
                    "excess_vs_benchmark": excess,
                })
        return results


# ── bucketed summary ────────────────────────────────────────────────────────
def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    wins = [x for x in xs if x > 0]
    return {
        "n": len(xs),
        "hit_rate": round(len(wins) / len(xs), 3),
        "avg_return": round(sum(xs) / len(xs), 4),
        "best": round(max(xs), 4),
        "worst": round(min(xs), 4),
    }


def _score_bucket(s: float) -> str:
    return "high>=7" if s >= 7 else ("mid5-7" if s >= 5 else "low<5")


def summarize(evaluated: list[dict]) -> dict:
    """Per-horizon and per-bucket stats. Includes a rejected-vs-accepted view."""
    if not evaluated:
        return {"n": 0, "note": "no matured signals"}

    by_horizon: dict[int, dict] = {}
    for h in HORIZONS:
        rows = [e for e in evaluated if e["horizon"] == h
                and e.get("label") != "rejected"]
        if not rows:
            continue
        und = [e["underlying_return"] for e in rows
               if e.get("underlying_return") is not None]
        excess_qqq = [e["excess_vs_benchmark"].get("QQQ") for e in rows
                      if e.get("excess_vs_benchmark", {}).get("QQQ") is not None]
        by_horizon[h] = {
            "underlying": _stats(und),
            "excess_vs_QQQ": _stats(excess_qqq),
        }

    def _bucketed(key_fn) -> dict:
        out: dict[str, list[float]] = {}
        for e in evaluated:
            if e.get("label") == "rejected":
                continue
            r = e.get("underlying_return")
            if r is None:
                continue
            out.setdefault(key_fn(e), []).append(r)
        return {k: _stats(v) for k, v in out.items()}

    # did we correctly avoid the rejected ones? compare underlying returns.
    acc = [e["underlying_return"] for e in evaluated
           if e.get("label") != "rejected" and e.get("underlying_return") is not None]
    rej = [e["underlying_return"] for e in evaluated
           if e.get("label") == "rejected" and e.get("underlying_return") is not None]

    return {
        "n": len(evaluated),
        "by_horizon": by_horizon,
        "score_buckets": _bucketed(lambda e: _score_bucket(e.get("final_score", 0))),
        "source_buckets": _bucketed(lambda e: e.get("source", "unknown")),
        "thesis_buckets": _bucketed(lambda e: e.get("thesis_cluster", "unknown")),
        "regime_buckets": _bucketed(lambda e: e.get("regime", "unknown")),
        "rejected_vs_accepted": {
            "accepted_underlying": _stats(acc),
            "rejected_underlying": _stats(rej),
        },
        "caveat": ("These are UNDERLYING returns (real prices) + benchmarks, NOT "
                   "option P&L — the delta/intrinsic option proxy has been removed. "
                   "Option performance is reported ONLY by the realistic path sim "
                   "(`outcomes-report --realistic`). No profitability is claimed "
                   "without the walk-forward guard."),
    }


# ── walk-forward + minimum-sample guard ─────────────────────────────────────
def walk_forward_guard(
    evaluated: list[dict], split_date: str, horizon: int = 30,
    min_sample: int = 30, return_key: str = "underlying_return",
) -> dict:
    """Refuse to assert an edge without an out-of-sample fold of sufficient size.

    Signals recorded before ``split_date`` are IN-sample; those on/after are
    OUT-of-sample. We only report an ``edge_claim`` of ``positive_oos`` when the
    OOS fold has at least ``min_sample`` matured signals AND a positive average.

    ``return_key`` selects which return to fold on: ``underlying_return`` (real
    prices, the default for the non-realistic report) or ``option_pnl_pct`` (the
    realistic path sim — the ONLY option-performance edge claim). The removed
    delta/intrinsic proxy is intentionally not an option here.
    """
    try:
        split = datetime.fromisoformat(split_date).date()
    except Exception:
        return {"edge_claim": "invalid_split_date"}

    def _fold(predicate) -> list[float]:
        xs: list[float] = []
        for e in evaluated:
            if e.get("horizon") != horizon or e.get("label") == "rejected":
                continue
            r = e.get(return_key)
            if r is None:
                continue
            try:
                d = datetime.fromisoformat(e["recorded_at"]).date()
            except Exception:
                continue
            if predicate(d):
                xs.append(r)
        return xs

    is_xs = _fold(lambda d: d < split)
    oos_xs = _fold(lambda d: d >= split)

    is_stats, oos_stats = _stats(is_xs), _stats(oos_xs)
    if oos_stats["n"] < min_sample:
        claim = "insufficient_oos_sample"
    elif oos_stats["avg_return"] > 0 and oos_stats["hit_rate"] >= 0.5:
        claim = "positive_oos"
    else:
        claim = "no_oos_edge"

    return {
        "horizon": horizon, "split_date": split_date, "min_sample": min_sample,
        "in_sample": is_stats, "out_of_sample": oos_stats, "edge_claim": claim,
        "note": ("No profitability is claimed unless edge_claim == 'positive_oos' "
                 "with n >= min_sample out-of-sample."),
    }
