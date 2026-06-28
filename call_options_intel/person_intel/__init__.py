"""
person_intel
============
The **Person-Intelligence-Layer** of the CALL-options subsystem.

Where the rest of `call_options_intel/` scores the *AI-infrastructure thesis*,
this sub-package tracks the *people* the thesis follows (Aschenbrenner /
Situational Awareness LP, Thiel / Founders Fund / Thiel Capital / Mithril) and
enforces a strict, auditable dataflow:

    Entity ─▶ Signal (filing/statement) ─▶ Evidence (typed, verified)
           ─▶ Thesis cluster ─▶ private→public Proxy
           ─▶ Research score  ─▶ (separate) Trade-candidate score

Hard rules baked into the types here:
  * facts and hypotheses are typed, never blurred (every claim has a confidence);
  * filings are differentiated (13F vs 13D/G vs Form D vs ADV) and 13F options
    are NOT read as naive direction (``direction_unknown``);
  * no CUSIP→ticker match without a confidence + source;
  * statements are discovery-only (URL + hash + excerpt, never full text);
  * thesis ≠ trade — research and trade-candidate scores are separate;
  * no profitability claim without out-of-sample evidence + a minimum sample.

Everything here is pure-Python, deterministic and offline. It never trades and
contains no broker/order code (enforced by the ``doctor`` guard).
"""

from __future__ import annotations

__all__ = [
    "entities",
    "filings",
    "cusip_map",
    "statements",
    "proxy_map",
    "person_scoring",
    "fills",
    "outcomes",
    "monitor",
    "monitor_report",
    "statement_feed",
    "holdings_diff",
    "network",
    "triple_score",
    "edgar_fts",
]
