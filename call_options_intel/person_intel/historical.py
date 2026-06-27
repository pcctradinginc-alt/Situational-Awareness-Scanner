"""
historical.py
=============
Phase 4 — a FREE, date-aware historical-close provider so outcome learning uses
the price AT each horizon (recorded_date + N days), not a single current spot.

  * offline: bundled per-ticker CSV fixtures (``date,close``) — deterministic;
  * live: Stooq free daily CSV (stdlib urllib, no key) — used only on opt-in.

``price_on(ticker, target)`` returns the close on/before ``target`` (so weekends
and holidays resolve to the prior trading day). Beyond the available series it
returns None — we never fabricate a future price.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger("coi.person.historical")


class Fetcher(Protocol):
    def get(self, url: str) -> Optional[str]: ...


class StooqFetcher:
    """Live daily history from Stooq (free, no key). Live mode only."""
    timeout_s: int = 10

    def get(self, url: str) -> Optional[str]:  # pragma: no cover - network
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:
            logger.warning("Stooq fetch failed (%s): %s", url, exc)
            return None


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_csv(text: str) -> dict[date, float]:
    """Parse a Date,…,Close CSV into {date: close}. Tolerant of column order."""
    out: dict[date, float] = {}
    if not text:
        return out
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        # case-insensitive Date / Close lookup
        d = c = None
        for k, v in row.items():
            kl = (k or "").strip().lower()
            if kl == "date":
                d = _parse_date(v)
            elif kl == "close":
                try:
                    c = float(v)
                except (TypeError, ValueError):
                    c = None
        if d is not None and c is not None and c > 0:
            out[d] = c
    return out


class HistoricalPriceProvider:
    def __init__(self, mode: str = "offline", fixtures_dir: str | Path | None = None,
                 fetcher: Optional[Fetcher] = None):
        self.mode = mode
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        self.fetcher = fetcher or StooqFetcher()
        self._cache: dict[str, dict[date, float]] = {}

    def series(self, ticker: str) -> dict[date, float]:
        t = ticker.upper()
        if t in self._cache:
            return self._cache[t]
        series: dict[date, float] = {}
        if self.mode == "live":
            url = f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d"
            series = _parse_csv(self.fetcher.get(url) or "")
        elif self.fixtures_dir:
            path = self.fixtures_dir / "historical" / f"{t}.csv"
            if path.exists():
                series = _parse_csv(path.read_text(encoding="utf-8"))
            else:
                logger.info("no historical fixture for %s", t)
        self._cache[t] = series
        return series

    def price_on(self, ticker: str, target: date) -> Optional[float]:
        """Close on/before ``target``; None if the series doesn't cover it.

        Returns None both when ``target`` precedes the series AND when it lies
        beyond the last observation — we never carry a stale close forward into a
        date we have no data for (that would silently fabricate an outcome).
        """
        series = self.series(ticker)
        if not series:
            return None
        if target > max(series):
            return None                      # beyond available history
        on_or_before = [d for d in series if d <= target]
        if not on_or_before:
            return None                      # target precedes the series
        return series[max(on_or_before)]

    def make_price_at(self):
        """Return a (ticker, date) -> price callable for OutcomeStore.evaluate."""
        return lambda ticker, d: self.price_on(ticker, d)
