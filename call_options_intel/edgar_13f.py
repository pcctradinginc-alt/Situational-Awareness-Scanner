"""
edgar_13f.py
============
SEC 13F monitoring. FREE EDGAR only. Fully mockable: the default path parses
local fixtures (JSON or the standard 13F information-table XML). Live fetching
is optional, off by default, and uses SEC's free data API with a descriptive
User-Agent per SEC fair-access policy. No API key, ever.

Conviction is scored from: new position / add / reduce / exit, q/q share change,
portfolio concentration, and persistence across filings.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Holding13F, Holding13FChange

logger = logging.getLogger("coi.edgar13f")


def _localname(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


class ThirteenFParser:
    """Pure parsing/scoring. No network."""

    # ── parsing ────────────────────────────────────────────────────────
    def parse_information_table_xml(
        self, xml_text: str, manager: str, cusip_map: dict[str, str] | None = None
    ) -> list[Holding13F]:
        """Parse a standard SEC 13F information table XML string."""
        cusip_map = cusip_map or {}
        holdings: list[Holding13F] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("13F XML parse error for %s: %s", manager, exc)
            return holdings

        total_value = 0.0
        rows: list[dict] = []
        for el in root.iter():
            if _localname(el.tag) != "infoTable":
                continue
            row: dict = {}
            for child in el.iter():
                name = _localname(child.tag)
                if name == "nameOfIssuer":
                    row["name"] = (child.text or "").strip()
                elif name == "cusip":
                    row["cusip"] = (child.text or "").strip()
                elif name == "value":
                    row["value"] = _to_float(child.text)
                elif name == "sshPrnamt":
                    row["shares"] = _to_float(child.text)
            if row:
                rows.append(row)
                if row.get("value"):
                    total_value += row["value"]

        for row in rows:
            cusip = row.get("cusip", "")
            ticker = cusip_map.get(cusip) or _ticker_from_name(row.get("name", ""))
            if not ticker:
                continue
            # SEC 13F `value` historically in $1000s; we keep it as reported and
            # only use it for relative portfolio_pct, so units cancel out.
            value = row.get("value")
            pct = (value / total_value) if (value and total_value) else None
            holdings.append(Holding13F(
                ticker=ticker.upper(), manager=manager,
                shares=row.get("shares"), value_usd=value, portfolio_pct=pct,
            ))
        return holdings

    def parse_json_holdings(self, data: dict, manager: str | None = None) -> list[Holding13F]:
        """Parse a simplified fixture: {manager, period, holdings:[...]}."""
        mgr = manager or data.get("manager", "unknown")
        rows = data.get("holdings", []) or []
        total_value = sum(
            (r.get("value_usd") or 0.0) for r in rows if isinstance(r, dict)
        )
        holdings: list[Holding13F] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ticker = str(r.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            value = r.get("value_usd")
            pct = r.get("portfolio_pct")
            if pct is None and value and total_value:
                pct = value / total_value
            holdings.append(Holding13F(
                ticker=ticker, manager=mgr, shares=r.get("shares"),
                value_usd=value, portfolio_pct=pct,
            ))
        return holdings

    # ── change detection ───────────────────────────────────────────────
    def detect_changes(
        self, current: list[Holding13F], prior: list[Holding13F]
    ) -> dict[str, dict]:
        """Per-ticker action + q/q share change for ONE manager."""
        cur = {h.ticker: h for h in current}
        pre = {h.ticker: h for h in prior}
        out: dict[str, dict] = {}
        for ticker, h in cur.items():
            if ticker not in pre:
                out[ticker] = {"action": "new", "pct_change": None,
                               "portfolio_pct": h.portfolio_pct, "manager": h.manager}
            else:
                p = pre[ticker]
                pct_change = None
                if p.shares and h.shares is not None:
                    pct_change = (h.shares - p.shares) / p.shares
                if pct_change is None:
                    action = "hold"
                elif pct_change > 0.02:
                    action = "add"
                elif pct_change < -0.02:
                    action = "reduce"
                else:
                    action = "hold"
                out[ticker] = {"action": action, "pct_change": pct_change,
                               "portfolio_pct": h.portfolio_pct, "manager": h.manager}
        for ticker, p in pre.items():
            if ticker not in cur:
                out[ticker] = {"action": "exit", "pct_change": -1.0,
                               "portfolio_pct": 0.0, "manager": p.manager}
        return out

    # ── scoring ────────────────────────────────────────────────────────
    def score_changes(
        self,
        per_manager_changes: list[dict[str, dict]],
        scoring_cfg: dict,
        manager_weights: dict[str, float] | None = None,
        persistence: dict[str, int] | None = None,
    ) -> dict[str, Holding13FChange]:
        """Aggregate per-manager changes into one conviction score per ticker."""
        manager_weights = manager_weights or {}
        persistence = persistence or {}
        cfg = scoring_cfg or {}

        new_bonus = cfg.get("new_position_bonus", 2.0)
        add_per = cfg.get("add_bonus_per_pct_change", 0.04)
        add_cap = cfg.get("add_bonus_cap", 3.0)
        reduce_per = cfg.get("reduce_penalty_per_pct_change", 0.03)
        exit_pen = cfg.get("exit_penalty", 2.5)
        conc_w = cfg.get("concentration_weight", 2.0)
        persist_per = cfg.get("persistence_bonus_per_quarter", 0.3)
        persist_cap = cfg.get("persistence_cap", 1.5)

        agg: dict[str, Holding13FChange] = {}
        for changes in per_manager_changes:
            for ticker, info in changes.items():
                mgr = info.get("manager", "unknown")
                w = manager_weights.get(mgr, 1.0)
                action = info["action"]
                raw = 0.0
                if action == "new":
                    raw += new_bonus
                elif action == "add":
                    pc = (info.get("pct_change") or 0.0) * 100.0
                    raw += min(add_cap, add_per * pc)
                elif action == "reduce":
                    pc = abs((info.get("pct_change") or 0.0)) * 100.0
                    raw -= reduce_per * pc
                elif action == "exit":
                    raw -= exit_pen
                # concentration
                ppct = info.get("portfolio_pct") or 0.0
                raw += conc_w * ppct

                entry = agg.get(ticker)
                if entry is None:
                    entry = Holding13FChange(ticker=ticker, action=action,
                                             pct_change=info.get("pct_change"),
                                             portfolio_pct=ppct, managers=[])
                    agg[ticker] = entry
                entry.conviction_score += raw * w
                if mgr not in entry.managers:
                    entry.managers.append(mgr)
                # Keep the strongest directional action label for readability.
                entry.action = _stronger_action(entry.action, action)
                if ppct and (entry.portfolio_pct is None or ppct > entry.portfolio_pct):
                    entry.portfolio_pct = ppct

        # persistence + clamp to 0..10
        for ticker, entry in agg.items():
            q = persistence.get(ticker, 0)
            entry.quarters_held = q
            entry.conviction_score += min(persist_cap, persist_per * q)
            entry.conviction_score = round(max(-5.0, min(10.0, entry.conviction_score)), 2)
        return agg


_ACTION_RANK = {"exit": 4, "new": 3, "add": 2, "reduce": 1, "hold": 0}


def _stronger_action(a: str, b: str) -> str:
    # Prefer the more "signal-bearing" action for the aggregate label.
    return a if _ACTION_RANK.get(a, 0) >= _ACTION_RANK.get(b, 0) else b


# Minimal name->ticker heuristic for XML fixtures lacking a cusip map.
_NAME_TICKER_HINTS = {
    "nvidia": "NVDA", "advanced micro": "AMD", "taiwan semiconductor": "TSM",
    "broadcom": "AVGO", "micron": "MU", "vistra": "VST", "constellation": "CEG",
    "palantir": "PLTR", "microsoft": "MSFT", "arista": "ANET", "vertiv": "VRT",
}


def _ticker_from_name(name: str) -> str | None:
    low = name.lower()
    for hint, tick in _NAME_TICKER_HINTS.items():
        if hint in low:
            return tick
    return None


def _to_float(text) -> float | None:
    if text is None:
        return None
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── high-level monitor over fixtures ───────────────────────────────────────
class ThirteenFMonitor:
    """Loads two-quarter fixtures per manager and produces aggregated changes.

    Fixture layout (offline default):
        <fixtures_dir>/13f/<manager_slug>_<period>.json
    where period is e.g. "2025Q4" (current) and "2025Q3" (prior). The newest
    two periods found per manager are diffed.
    """

    def __init__(self, parser: ThirteenFParser | None = None):
        self.parser = parser or ThirteenFParser()

    def run_from_fixtures(
        self, fixtures_dir: str | Path, investors_cfg: dict
    ) -> dict[str, Holding13FChange]:
        fdir = Path(fixtures_dir) / "13f"
        scoring_cfg = (investors_cfg or {}).get("scoring", {})
        managers_cfg = (investors_cfg or {}).get("managers", []) or []
        manager_weights = {m.get("name", ""): m.get("weight", 1.0) for m in managers_cfg}

        if not fdir.exists():
            logger.warning("No 13F fixtures dir at %s — empty 13F signal", fdir)
            return {}

        # group files by manager slug
        by_manager: dict[str, list[Path]] = {}
        for f in sorted(fdir.glob("*.json")):
            slug = f.stem.rsplit("_", 1)[0]
            by_manager.setdefault(slug, []).append(f)

        per_manager_changes: list[dict[str, dict]] = []
        persistence: dict[str, int] = {}
        for slug, files in by_manager.items():
            files = sorted(files, key=lambda p: p.stem)  # period sorts lexically
            if len(files) < 1:
                continue
            current = self._load(files[-1])
            prior = self._load(files[-2]) if len(files) >= 2 else []
            changes = self.parser.detect_changes(current, prior)
            per_manager_changes.append(changes)
            # crude persistence: present in both quarters -> +1
            cur_t = {h.ticker for h in current}
            pre_t = {h.ticker for h in prior}
            for t in cur_t:
                persistence[t] = persistence.get(t, 0) + (1 if t in pre_t else 0)

        return self.parser.score_changes(
            per_manager_changes, scoring_cfg, manager_weights, persistence
        )

    def _load(self, path: Path) -> list[Holding13F]:
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            logger.warning("Could not load 13F fixture %s: %s", path, exc)
            return []
        return self.parser.parse_json_holdings(data)
