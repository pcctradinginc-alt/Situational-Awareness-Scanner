"""
backtest.py
===========
Minimal paper-evaluation framework. It does NOT claim profitability — it exists
so that any future edge claim can be checked against evidence.

Workflow:
  1. record(scan_result)   -> append each ranked candidate to a JSONL signal
                              store with a timestamp and entry snapshot.
  2. evaluate(price_lookup)-> for matured signals (now >= horizon), compute the
                              underlying return and a first-order option-proxy
                              return (delta-leverage, capped at -100%).
  3. summarize()           -> hit-rate, average return, drawdown proxy, score-
                              bucket performance, and a buy-the-underlying
                              benchmark for comparison.

Everything is deterministic and injectable, so it runs offline in tests.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import ScanResult

logger = logging.getLogger("coi.backtest")


class SignalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── record ─────────────────────────────────────────────────────────
    def record(self, result: ScanResult, horizon_days: int = 30,
               only_top: bool = False) -> int:
        rows = result.top if only_top else result.candidates
        n = 0
        with self.path.open("a", encoding="utf-8") as fh:
            for c in rows:
                ct = c.contract
                rec = {
                    "recorded_at": result.generated_at or datetime.now(timezone.utc).isoformat(),
                    "ticker": c.ticker,
                    "expiry": ct.expiry,
                    "strike": ct.strike,
                    "dte": ct.dte,
                    "entry_spot": ct.spot,
                    "entry_premium": ct.mid,
                    "entry_delta": ct.delta,
                    "iv": ct.iv,
                    "final_score": c.final_score,
                    "confidence_label": c.confidence_label,
                    "is_event_trade": c.is_event_trade,
                    "horizon_days": horizon_days,
                    "label": "top" if c in result.top else "candidate",
                }
                fh.write(json.dumps(rec) + "\n")
                n += 1
        logger.info("Recorded %d signals -> %s", n, self.path)
        return n

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed signal row")
        return out

    # ── evaluate ────────────────────────────────────────────────────────
    def evaluate(self, price_lookup: Callable[[str], Optional[float]],
                 as_of: Optional[date] = None) -> list[dict]:
        """Return matured signals annotated with realised returns."""
        as_of = as_of or date.today()
        results = []
        for rec in self.load():
            try:
                rec_date = datetime.fromisoformat(rec["recorded_at"]).date()
            except Exception:
                continue
            age = (as_of - rec_date).days
            if age < rec.get("horizon_days", 30):
                continue  # not matured yet
            now_price = price_lookup(rec["ticker"])
            entry_spot = rec.get("entry_spot")
            if now_price is None or not entry_spot:
                continue
            underlying_ret = (now_price - entry_spot) / entry_spot
            opt_ret = self._option_proxy_return(rec, now_price)
            results.append({
                **rec, "eval_price": now_price, "age_days": age,
                "underlying_return": round(underlying_ret, 4),
                "option_proxy_return": (round(opt_ret, 4) if opt_ret is not None else None),
            })
        return results

    @staticmethod
    def _option_proxy_return(rec: dict, now_price: float) -> Optional[float]:
        """First-order delta-leverage proxy, capped at -1 (premium is max loss).

        If a premium is known we also blend an intrinsic-value floor so deep
        moves are not understated. This is a PROXY only — real option P&L depends
        on theta/vega/IV path which free historical data rarely provides.
        """
        entry_spot = rec.get("entry_spot")
        premium = rec.get("entry_premium")
        delta = rec.get("entry_delta")
        strike = rec.get("strike")
        if not entry_spot or not premium or premium <= 0:
            return None
        move = now_price - entry_spot
        if delta is not None:
            linear = (delta * move) / premium
        else:
            linear = move / premium  # crude
        # intrinsic floor at expiry-style payoff
        if strike is not None:
            intrinsic = max(0.0, now_price - strike)
            intrinsic_ret = (intrinsic - premium) / premium
            est = max(linear, intrinsic_ret)
        else:
            est = linear
        return max(-1.0, est)

    # ── summarize ────────────────────────────────────────────────────────
    def summarize(self, evaluated: list[dict]) -> dict:
        if not evaluated:
            return {"n": 0, "note": "no matured signals to evaluate"}
        opt_rets = [e["option_proxy_return"] for e in evaluated
                    if e.get("option_proxy_return") is not None]
        und_rets = [e["underlying_return"] for e in evaluated
                    if e.get("underlying_return") is not None]

        def _stats(xs):
            if not xs:
                return {"n": 0}
            wins = [x for x in xs if x > 0]
            return {
                "n": len(xs),
                "hit_rate": round(len(wins) / len(xs), 3),
                "avg_return": round(sum(xs) / len(xs), 4),
                "best": round(max(xs), 4),
                "worst": round(min(xs), 4),
                "drawdown_proxy": round(min(xs), 4),
            }

        buckets = {"high>=7": [], "mid5-7": [], "low<5": []}
        for e in evaluated:
            r = e.get("option_proxy_return")
            if r is None:
                continue
            s = e.get("final_score", 0)
            key = "high>=7" if s >= 7 else ("mid5-7" if s >= 5 else "low<5")
            buckets[key].append(r)
        bucket_stats = {k: _stats(v) for k, v in buckets.items()}

        return {
            "n": len(evaluated),
            "option_proxy": _stats(opt_rets),
            "underlying_benchmark": _stats(und_rets),
            "score_buckets": bucket_stats,
            "caveat": ("Option-proxy returns are first-order delta/intrinsic "
                       "approximations, not realised option P&L. No profitability "
                       "is claimed."),
        }
