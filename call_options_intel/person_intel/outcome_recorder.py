"""
outcome_recorder.py
===================
Fill the outcome store **in real operation** — the single most important lever:
without recorded signals there is nothing to learn from.

For EVERY signal a monitor run produces — filing alerts, conviction statements,
vorfeld changes, **including the ones the gate REJECTED** — this records one
append-only row capturing the full decision context at signal time:

  when · source · latency · person-link · ticker/proxy · spot · option chain
  (strike / premium / delta / IV / DTE / spread) · the three score components ·
  the decision (top / watch / reject)

Later, :mod:`outcomes` matures each row at 7/14/30/60/90/180 days against the
underlying + QQQ/SOXX with the walk-forward guard. Recording rejected candidates
is deliberate: we must be able to measure what the system correctly AVOIDED.

The market/option snapshot provider is injectable: a pipeline-backed function in
real runs (offline fixtures / live yfinance), or a fake in tests. A signal with
no tradeable public proxy is still recorded (audit trail) — it simply matures
into no return.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .outcomes import OutcomeStore
from .proxy_map import ProxyMap

logger = logging.getLogger("coi.person.outcome_recorder")

# snapshot_fn(ticker) -> dict(spot, strike, entry_premium, entry_delta, iv, dte,
#                             spread_pct, open_interest, volume, expiry) | None
SnapshotFn = Callable[[str], Optional[dict]]

# under data/person_intel/ so the workflow commits it across runs (reports/* is
# gitignored). This append-only file is the real learning substrate.
DEFAULT_STORE = "data/person_intel/outcomes.jsonl"


def _decision(triple: dict) -> str:
    label = (triple or {}).get("label", "")
    if label == "TRADE-CANDIDATE":
        return "top"
    if label == "WATCH":
        return "watch"
    return "rejected"


def _axes(triple: dict) -> tuple[float, float, float]:
    def v(k):
        c = (triple or {}).get(k, {})
        return float(c.get("value") or 0.0) if isinstance(c, dict) else 0.0
    return v("person_signal"), v("freshness"), v("tradeability")


def _pick_target_call(contracts: list, target_delta: float = 0.40) -> Optional[object]:
    """Pick a representative long call: delta nearest target, else ~5% OTM."""
    if not contracts:
        return None
    with_delta = [c for c in contracts if getattr(c, "delta", None) is not None]
    if with_delta:
        return min(with_delta, key=lambda c: abs((c.delta or 0) - target_delta))
    spot = getattr(contracts[0], "spot", None)
    if spot:
        return min(contracts, key=lambda c: abs((c.strike / spot) - 1.05))
    return contracts[0]


def make_pipeline_snapshot_fn(cfg, mode: str, fixtures) -> Optional[SnapshotFn]:
    """Build a memoised ticker→option-snapshot provider from the pipeline."""
    try:
        from ..pipeline import Pipeline
        pipe = Pipeline(config=cfg, mode=("live" if mode == "live" else "offline"),
                        fixtures_dir=fixtures)
    except Exception as exc:                            # pragma: no cover
        logger.warning("snapshot pipeline unavailable: %s", exc)
        return None
    cache: dict[str, Optional[dict]] = {}

    def fn(ticker: Optional[str]) -> Optional[dict]:
        if not ticker:
            return None
        if ticker in cache:
            return cache[ticker]
        snap: Optional[dict] = None
        try:
            m = pipe.market.get_snapshot(ticker)
            contracts = pipe.options.get_call_contracts(
                ticker, m.spot, m.hist_vol_annual)
            c = _pick_target_call(contracts)
            if m.spot and c is not None:
                snap = {
                    "spot": round(float(m.spot), 4),
                    "strike": float(c.strike),
                    "entry_premium": (float(c.mid) if c.mid is not None else None),
                    "entry_delta": (float(c.delta) if c.delta is not None else None),
                    "iv": (float(c.iv) if c.iv is not None else None),
                    "dte": int(c.dte) if c.dte is not None else None,
                    "spread_pct": c.spread_pct,
                    "open_interest": c.open_interest,
                    "volume": c.volume,
                    "expiry": c.expiry,
                }
        except Exception:
            snap = None
        cache[ticker] = snap
        return snap
    return fn


def _proxy_for_cluster(proxy_map: ProxyMap, cluster: str) -> Optional[str]:
    if not cluster:
        return None
    ranked = proxy_map.best_proxies(cluster, top=1)
    return ranked[0].ticker if ranked else None


def _rows_for_run(result, proxy_map: ProxyMap, snapshot_fn: Optional[SnapshotFn],
                  recorded_at: str) -> list[dict]:
    rows: list[dict] = []

    def base(kind_source: str, principal: str, path_weight: float, ticker,
             cluster: str, triple: dict, age_days, headline: str,
             proxy_of: str = "") -> dict:
        p, f, t = _axes(triple)
        row = {
            "recorded_at": recorded_at,
            "source": kind_source,
            "latency_days": age_days,
            "principal": principal or "",
            "path_weight": round(float(path_weight or 0.0), 3),
            "ticker": ticker,
            "proxy_of_cluster": proxy_of or cluster or "",
            "thesis_cluster": cluster or "",
            "regime": "unknown",
            "person_signal": round(p, 2), "freshness": round(f, 2),
            "tradeability": round(t, 2),
            "final_score": round((p + f + t) / 3.0, 2),
            "gate_score": (triple or {}).get("final_trade_score", 0.0),
            "gate_pass": bool((triple or {}).get("gate_pass")),
            "label": _decision(triple),
            "headline": headline,
        }
        snap = snapshot_fn(ticker) if (snapshot_fn and ticker) else None
        if snap:
            row.update({"entry_spot": snap.get("spot"), **{
                k: snap.get(k) for k in (
                    "strike", "entry_premium", "entry_delta", "iv", "dte",
                    "spread_pct", "open_interest", "volume", "expiry")}})
        return row

    # filing alerts (what they DID) — record EVERY one, incl. rejects
    for a in result.alerts:
        cluster = ""
        cl = proxy_map.clusters_for_ticker(a.subject_ticker) if a.subject_ticker else []
        if cl:
            cluster = max(cl, key=lambda kv: kv[1].quality())[0]
        ticker = a.subject_ticker or _proxy_for_cluster(proxy_map, cluster)
        rows.append(base(
            f"filing:{a.filing_type}" + (":fts" if a.discovered_via == "edgar_fts" else ""),
            a.principal, a.path_weight, ticker, cluster, a.triple, a.age_days,
            a.headline, proxy_of=("" if a.subject_ticker else cluster)))

    # conviction statements (what they SAY)
    for s in result.statements:
        ticker = s.derived_candidates[0] if s.derived_candidates else None
        rows.append(base(
            "statement", s.principal, 0.0, ticker, s.dominant_cluster, s.triple,
            s.age_days, s.headline, proxy_of=s.dominant_cluster))

    # vorfeld change-detection
    for v in result.vorfeld:
        ticker = _proxy_for_cluster(proxy_map, v.cluster)
        rows.append(base(
            f"vorfeld:{v.source}", v.principal, 0.0, ticker, v.cluster, v.triple,
            v.age_days, v.headline, proxy_of=v.cluster))
    return rows


def record_run(result, proxy_map: ProxyMap, *,
               store_path: str | Path = DEFAULT_STORE,
               snapshot_fn: Optional[SnapshotFn] = None,
               as_of: Optional[date] = None) -> int:
    """Append one outcome row per NEW signal in this run. Returns rows written."""
    recorded_at = ((as_of.isoformat() + "T12:00:00+00:00") if as_of else
                   datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rows = _rows_for_run(result, proxy_map, snapshot_fn, recorded_at)
    if not rows:
        return 0
    OutcomeStore(store_path).record_many(rows)
    logger.info("recorded %d outcome rows -> %s", len(rows), store_path)
    return len(rows)
