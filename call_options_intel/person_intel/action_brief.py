"""
action_brief.py
===============
Turn a scored signal into an **extremely action-oriented** five-part brief, so the
email is a decision aid, not just "ticker + score". Every alert answers:

  1. **Was ist neu?**          new filing / position / statement / entity / proxy
  2. **Warum Leo/Thiel-relevant?**  direct ownership · indirect entity · media hint · only theme-fit
  3. **Warum ist es früh?**     before 13F · direct EDGAR event · not yet mainstream · price reaction small
  4. **Bester öffentlicher Proxy?**  stock · liquid option · spread · ETF · or only watchlist
  5. **Warum jetzt NICHT handeln?**  IV too rich · spread too wide · earnings risk · already ran · data uncertain

Everything is derived from data the radar already has — the three-axis score, the
option snapshot (IV / spread / DTE / strike), the ranked proxy map and the
fact/hypothesis flags — so the brief stays honest and auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

IV_RICH = 0.60          # ATM IV above this → long options expensive
SPREAD_WIDE = 0.10      # bid/ask spread above this → poor fill

# broad ETF fallback per cluster (a diversified proxy when single-name is unclear)
_CLUSTER_ETF = {
    "compute": "SOXX (semis)", "networking": "SOXX (semis)",
    "export_controls": "SOXX (semis)", "cooling": "XLK (broad tech)",
    "defense_ai": "ITA (defense)", "power_grid": "XLU / VPU (utilities)",
    "nuclear": "URA / NLR (nuclear)", "automation": "BOTZ / ROBO (robotics)",
}


@dataclass
class ActionBrief:
    whats_new: str
    why_relevant: str
    why_early: str
    best_proxy: str
    why_not_now: str

    def to_dict(self) -> dict:
        return asdict(self)


def _principal_name(p: str) -> str:
    return {"thiel": "Peter Thiel", "aschenbrenner": "Leopold Aschenbrenner"}.get(
        p, "the tracked network")


# ── shared building blocks ──────────────────────────────────────────────────
def _proxy_text(*, ticker: Optional[str], snapshot: Optional[dict],
                ranked: Optional[list], cluster: str, tradeable: float) -> str:
    """Best public proxy: liquid option / stock / spread / ETF / watchlist."""
    etf = _CLUSTER_ETF.get(cluster, "")
    # a directly resolved, liquid name → propose the concrete option
    if ticker and snapshot and snapshot.get("entry_premium"):
        iv = snapshot.get("iv")
        spread = snapshot.get("spread_pct")
        strike = snapshot.get("strike")
        dte = snapshot.get("dte")
        prem = snapshot.get("entry_premium")
        wide = spread is not None and spread > SPREAD_WIDE
        rich = iv is not None and iv > IV_RICH
        instrument = "stock or a debit call-SPREAD" if (rich or wide) else "a liquid long CALL"
        bits = [f"{ticker}: {instrument}"]
        if strike and dte:
            bits.append(f"~{strike:g} strike · {dte}d · prem ~{prem:g}")
        if iv is not None:
            bits.append(f"IV {iv:.0%}")
        out = " · ".join(bits)
        if etf:
            out += f" · ETF alt: {etf}"
        return out
    # no liquid option, but the ranked proxy map has a public name
    if ranked:
        top = ranked[0]
        alts = ", ".join(r.get("ticker", "") for r in ranked[1:3])
        live = "live" if top.get("options_live") else "prior"
        out = (f"best public proxy «{cluster}»: {top['ticker']} "
               f"(edge {top.get('edge_score', 0):.1f}, opt/{live})")
        if alts:
            out += f" · alts {alts}"
        if etf:
            out += f" · ETF: {etf}"
        return out
    if ticker:
        return f"{ticker} — stock only (options thin/unverified)" + (
            f" · ETF: {etf}" if etf else "")
    return ("WATCHLIST only — no sourced, liquid public proxy yet"
            + (f" (broad ETF: {etf})" if etf else ""))


def _not_now_text(*, triple: dict, snapshot: Optional[dict],
                  needs_review: bool, is_private: bool, role_early: bool,
                  has_ticker: bool) -> str:
    reasons: list[str] = []
    iv = (snapshot or {}).get("iv")
    spread = (snapshot or {}).get("spread_pct")
    if iv is not None and iv > IV_RICH:
        reasons.append(f"IV rich (~{iv:.0%}) — long premium vulnerable to vega/crush")
    if spread is not None and spread > SPREAD_WIDE:
        reasons.append(f"bid/ask spread wide ({spread:.0%}) — poor fill")
    if is_private:
        reasons.append("private placement — no public ticker; nothing to trade yet")
    if needs_review:
        reasons.append("data uncertain — subject/entity needs_human_review (verify first)")
    if not has_ticker:
        reasons.append("no confirmed tradeable proxy — watch, do not size")
    fa = set((triple or {}).get("failing_axes", []))
    if "freshness" in fa and not role_early:
        reasons.append("already lagged/priced (e.g. quarterly 13F) — no early edge left")
    if "tradeability" in fa and has_ticker and not reasons:
        reasons.append("timing/liquidity not confirmed — verify live before sizing")
    reasons.append("always check the earnings calendar before buying premium")
    if not (triple or {}).get("gate_pass"):
        reasons.insert(0, "GATE NOT PASSED")
    return "; ".join(reasons)


# ── per signal-type builders ────────────────────────────────────────────────
def filing_brief(alert, *, snapshot: Optional[dict], ranked: Optional[list],
                 cluster: str) -> dict:
    pname = _principal_name(alert.principal)
    pw = alert.path_weight
    # 1. what's new
    if alert.is_new_entity:
        whats_new = (f"NEW ENTITY discovered — {alert.entity} (CIK {alert.cik}) "
                     f"files {alert.filing_type}; not yet in the tracked graph")
    elif getattr(alert, "position_changes", None):
        from .holdings_diff import summarize_changes  # reuse the labels
        whats_new = f"New {alert.filing_type}; position changes present"
    else:
        subj = alert.subject_ticker or alert.subject_issuer or "subject pending"
        whats_new = f"{alert.category} → {subj}"
    # 2. relevance
    if pw >= 0.95:
        why_relevant = f"DIRECT: {pname} (or a wholly-controlled vehicle) is the filer (path {pw:.2f})"
    elif pw >= 0.75:
        why_relevant = f"confirmed vehicle of {pname} (path {pw:.2f})"
    elif alert.is_new_entity:
        why_relevant = (f"indirect: filing NAMES {pname} but the control link is "
                        f"UNCONFIRMED (path {pw:.2f})")
    elif pw >= 0.45:
        why_relevant = f"affiliated entity of {pname} (path {pw:.2f}) — likely, not confirmed"
    else:
        why_relevant = f"adjacent network / theme-fit only (path {pw:.2f})"
    # 3. early
    if alert.role == "early":
        why_early = (f"EARLY: direct EDGAR event ({alert.filing_type}), days-fast — "
                     f"ahead of the quarterly 13F")
        if alert.discovered_via == "edgar_fts":
            why_early += "; surfaced via full-text search (before broad coverage)"
    else:
        why_early = (f"CONFIRMATION ({alert.filing_type}, lagged ~45d) — not early; "
                     f"confirms an existing thesis")
    tr = alert.triple or {}
    f = tr.get("freshness", {}).get("value")
    if f is not None:
        why_early += f" · freshness {f:.1f}/10" + (
            f", {alert.age_days}d old" if alert.age_days is not None else "")
    # 4 + 5
    tradeable = (tr.get("tradeability", {}) or {}).get("value") or 0.0
    best_proxy = _proxy_text(ticker=alert.subject_ticker, snapshot=snapshot,
                             ranked=ranked, cluster=cluster, tradeable=tradeable)
    why_not_now = _not_now_text(
        triple=tr, snapshot=snapshot, needs_review=alert.needs_human_review,
        is_private=alert.is_private, role_early=(alert.role == "early"),
        has_ticker=bool(alert.subject_ticker and not alert.needs_human_review))
    return ActionBrief(whats_new, why_relevant, why_early, best_proxy,
                       why_not_now).to_dict()


def statement_brief(s, *, snapshot: Optional[dict], ranked: Optional[list]) -> dict:
    pname = _principal_name(s.principal)
    tier = s.tier
    whats_new = f"New {tier}-tier statement by {s.speaker} → theme «{s.dominant_cluster}»"
    if tier in ("official", "primary"):
        why_relevant = (f"FIRST-PARTY: {s.speaker}'s own words (a direct conviction/"
                        f"Denkbewegung) — but it is a SAY, not a capital move")
    else:
        why_relevant = (f"MEDIA hint (secondary): names {s.speaker}, not a primary "
                        f"source — theme-fit, low directness")
    why_early = ("fresh first-party statement (often before broad media pickup)"
                 if tier in ("official", "primary")
                 else "already reported in media — less early")
    if s.age_days is not None:
        why_early += f" · {s.age_days}d old"
    tr = s.triple or {}
    tradeable = (tr.get("tradeability", {}) or {}).get("value") or 0.0
    best_proxy = _proxy_text(
        ticker=(s.derived_candidates[0] if s.derived_candidates else None),
        snapshot=snapshot, ranked=ranked, cluster=s.dominant_cluster,
        tradeable=tradeable)
    why_not_now = _not_now_text(
        triple=tr, snapshot=snapshot, needs_review=True, is_private=False,
        role_early=True, has_ticker=bool(s.derived_candidates))
    # a statement is a derived/second-order idea by construction
    why_not_now = "second-order/derived (corroborate with a filing); " + why_not_now
    return ActionBrief(whats_new, why_relevant, why_early, best_proxy,
                       why_not_now).to_dict()


def vorfeld_brief(v, *, snapshot: Optional[dict], ranked: Optional[list]) -> dict:
    pname = _principal_name(v.principal)
    kind_label = {
        "aum_change": "ADV AUM change", "new_control_person": "new control person",
        "new_private_fund": "new private fund", "office_move": "office move",
        "new_role": "new job posting", "site_change": "official website changed",
        "new_domain": "brand-new domain (CT log)",
    }.get(v.kind, v.kind)
    whats_new = f"{kind_label}: {v.detail or v.headline}"
    why_relevant = (f"VORFELD change at {v.entity}, associated with {pname} "
                    f"(non-filing context — control/intent UNCONFIRMED)")
    early_map = {
        "cert_transparency": "very early: new domain in CT logs — often weeks before any filing",
        "job_postings": "early hiring proxy — a theme shift before it shows up in results",
        "adv_iapd": "ADV change — slower (periodic), context not a market event",
        "domain_watch": "official-page change — an early product/fund footprint",
    }
    why_early = early_map.get(v.source, "non-filing change-detection signal")
    if v.age_days is not None:
        why_early += f" · {v.age_days}d old"
    tr = v.triple or {}
    tradeable = (tr.get("tradeability", {}) or {}).get("value") or 0.0
    best_proxy = _proxy_text(ticker=None, snapshot=snapshot, ranked=ranked,
                             cluster=v.cluster, tradeable=tradeable)
    why_not_now = ("context-grade, not a confirmed capital move; "
                   + _not_now_text(triple=tr, snapshot=snapshot, needs_review=True,
                                   is_private=False, role_early=True,
                                   has_ticker=False))
    return ActionBrief(whats_new, why_relevant, why_early, best_proxy,
                       why_not_now).to_dict()
