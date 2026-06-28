"""
monitor.py
==========
The **live, dedup'd person-signal monitor** — the piece that turns the offline
person-intelligence layer into a twice-daily *radar* for Leopold Aschenbrenner
and Peter Thiel.

What it does (and, just as importantly, what it refuses to do):

  * Pulls the *recent filing feed* for every tracked Thiel / Aschenbrenner
    vehicle (CIKs in ``config/investors_13f.yml`` / ``config/entity_graph.yml``)
    from SEC EDGAR's free ``submissions`` API — reusing the injectable
    :class:`~.edgar_fast.EdgarFastClient` (offline fixtures by default, live SEC
    only on explicit opt-in).
  * Classifies each filing into an honest, typed *signal category*
    (insider trade / new >5% stake / passive stake / private placement /
    quarterly 13F) with its lead-vs-confirmation role.
  * Resolves the PUBLIC subject ticker via a check-digit-valid CUSIP **only when
    it can** — otherwise the subject stays ``needs_human_review``. A Form D is a
    *private* footprint with no public ticker, and is labelled as such.
  * Grades each filing by the *confidence of the controlled-entity path* from a
    tracked principal (a confirmed Thiel vehicle outranks merely adjacent smart
    money).
  * **De-duplicates against a persistent store** keyed by SEC accession number,
    so a given filing is reported exactly once — the basis for *"email only on a
    NEW signal"*.

Hard rules (mirrors the layer's design principles):
  * A filing is a **fact** (it exists, with an accession + URL). The *subject*
    of that filing is only asserted as a ticker when a CUSIP maps with
    confidence; everything else is ``needs_human_review`` — never a guess.
  * 13F is CONFIRMATION (quarterly, lagged); 13D/G and Form 4 are EARLY.
  * Nothing here trades. It emits research signals only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from ..config_loader import AppConfig
from .cusip_map import CusipMapper, load_mapper
from .edgar_fast import (
    EARLY_FORMS, EdgarFastClient, FastFilingRef, load_fast_client,
)
from .entities import EntityGraph, load_graph
from .filings import FilingType, SignalRole, filing_meta
from .proxy_map import ProxyMap, load_proxy_map
from .statements import SourceTier
from .triple_score import GateThresholds, TripleInput, TripleScorer

logger = logging.getLogger("coi.person.monitor")

# Tracked principals; proximity to one of these defines the signal strength.
PRINCIPALS = ("thiel", "aschenbrenner")

# Where the persistent dedup / history store lives (git-versioned, free).
DEFAULT_STATE_DIR = Path("data/person_intel")
DEFAULT_STATE_PATH = DEFAULT_STATE_DIR / "seen_filings.json"

# Forms we treat as actionable person-signals. Everything else (8-K, S-1, …) is
# CONTEXT noise for this radar and is dropped.
RELEVANT_FORMS = EARLY_FORMS | {
    FilingType.FORM_13F_HR, FilingType.FORM_13F_HR_A,
}

# A short, honest human label + signal kind per filing type.
_CATEGORY: dict[FilingType, tuple[str, str]] = {
    FilingType.FORM_4: ("Insider transaction (Form 4)", "insider_trade"),
    FilingType.SC_13D: ("New activist >5% stake (SC 13D)", "new_stake"),
    FilingType.SC_13D_A: ("Change to >5% activist stake (SC 13D/A)", "stake_change"),
    FilingType.SC_13G: ("New passive >5% stake (SC 13G)", "passive_stake"),
    FilingType.SC_13G_A: ("Change to passive >5% stake (SC 13G/A)", "passive_change"),
    FilingType.FORM_D: ("Private placement (Form D)", "private_round"),
    FilingType.FORM_D_A: ("Amended private placement (Form D/A)", "private_round"),
    FilingType.FORM_13F_HR: ("Quarterly 13F holdings filed (13F-HR)", "quarterly_13f"),
    FilingType.FORM_13F_HR_A: ("Amended quarterly 13F (13F-HR/A)", "quarterly_13f"),
}


def _confidence_label(weight: float) -> str:
    """Bucket a 0..1 controlled-path weight into an honest tier label."""
    if weight >= 0.95:
        return "principal"        # the person, or a confirmed wholly-owned vehicle
    if weight >= 0.75:
        return "confirmed_vehicle"
    if weight >= 0.45:
        return "affiliated"
    return "network"              # adjacent smart money, not the principal's own


# ── persistent dedup / history store ────────────────────────────────────────
@dataclass
class SeenStore:
    """Append-only record of accession numbers already reported.

    Keyed by SEC accession so each filing fires exactly once across runs. Stored
    as plain JSON so it is git-versioned (the *historical signal archive*) and
    needs no database.
    """
    path: Path
    seen: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SeenStore":
        p = Path(path) if path else DEFAULT_STATE_PATH
        data: dict[str, dict] = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                data = raw.get("seen", raw) if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("seen-store unreadable (%s); starting fresh", exc)
        return cls(path=p, seen=data)

    def is_new(self, accession: str) -> bool:
        return bool(accession) and accession not in self.seen

    def mark(self, ref: "FastFilingRef", as_of: Optional[date] = None) -> None:
        if not ref.accession:
            return
        self.seen[ref.accession] = {
            "first_seen": (as_of or date.today()).isoformat(),
            "entity": ref.entity,
            "cik": ref.cik,
            "form": ref.form,
            "filing_date": ref.filing_date,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self.seen),
            "seen": self.seen,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                             encoding="utf-8")


# ── the alert object ────────────────────────────────────────────────────────
@dataclass
class PersonAlert:
    # provenance — this part is a FACT (the filing exists)
    entity: str
    entity_id: str
    cik: str
    filing_type: str
    form_raw: str
    role: str                       # early | confirmation
    category: str                   # human label
    signal_kind: str                # machine label
    filing_date: str
    age_days: Optional[int]
    accession: str
    url: str

    # principal linkage — how close to Thiel / Aschenbrenner this filer is
    principal: str                  # thiel | aschenbrenner | network
    path_weight: float
    link_confidence: str            # principal | confirmed_vehicle | affiliated | network

    # the SUBJECT of the filing — asserted only when sourced; never guessed
    subject_ticker: Optional[str] = None
    subject_issuer: Optional[str] = None
    subject_confidence: float = 0.0
    needs_human_review: bool = True
    is_private: bool = False
    is_fact: bool = True            # the filing itself; subject ticker may not be

    headline: str = ""
    why_it_matters: str = ""
    falsification: str = ""
    position_changes: list = field(default_factory=list)  # 13F new/add/trim/exit
    triple: dict = field(default_factory=dict)            # 3-axis score + gate
    discovered_via: str = "cik_feed"      # cik_feed | edgar_fts
    is_new_entity: bool = False           # filer not yet in the tracked graph
    matched_term: str = ""                # FTS term that surfaced it

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def sort_key(self) -> tuple:
        # most-leading (early) + closest-to-principal + most-recent first
        role_rank = 0 if self.role == SignalRole.EARLY.value else 1
        age = self.age_days if self.age_days is not None else 999
        return (role_rank, -self.path_weight, age)


# ── the monitor ─────────────────────────────────────────────────────────────
class PersonMonitor:
    """Builds person-alerts from the tracked-manager filing feed."""

    def __init__(self, client: EdgarFastClient, graph: EntityGraph,
                 mapper: CusipMapper, proxy_map: ProxyMap,
                 managers: list[dict], fts_client=None,
                 fts_terms: Optional[list[dict]] = None):
        self.client = client
        self.graph = graph
        self.mapper = mapper
        self.proxy_map = proxy_map
        self.managers = managers or []
        self.fts_client = fts_client          # EdgarFTSClient | None (vorfeld)
        self.fts_terms = fts_terms or []

    # principal linkage ------------------------------------------------------
    def _principal_link(self, cik: str, name: str) -> tuple[str, float, str]:
        """Best (principal, path_weight, entity_id) for a filer.

        A filing by a confirmed Thiel/Aschenbrenner vehicle carries the full
        controlled-path weight; merely adjacent smart money scores a weak 0.3.
        """
        ent = self.graph.by_cik(cik) if cik else None
        if ent is None and name:
            low = name.lower()
            for e in self.graph.entities.values():
                names = [e.name.lower()] + [a.lower() for a in e.aliases]
                if any(low == n or low in n or n in low for n in names if n):
                    ent = e
                    break
        if ent is None:
            return ("network", 0.3, "")
        if ent.id in PRINCIPALS:
            return (ent.id, 1.0, ent.id)
        best_p, best_w = "network", 0.0
        for p in PRINCIPALS:
            if p not in self.graph.entities:
                continue
            w = self.graph.path_confidence(p, ent.id) or 0.0
            if w > best_w:
                best_w, best_p = w, p
        if best_w <= 0:
            return ("network", 0.3, ent.id)
        return (best_p, round(best_w, 3), ent.id)

    # subject resolution -----------------------------------------------------
    def _resolve_subject(self, ref: FastFilingRef) -> dict:
        """Resolve the public subject ticker (13D/G/Form 4) or flag a private
        footprint (Form D). Never guesses — unmapped stays needs_human_review."""
        if ref.filing_type in (FilingType.FORM_D, FilingType.FORM_D_A):
            res = self.client.resolve_subject(ref, self.mapper)
            return {
                "subject_ticker": None,
                "subject_issuer": res.subject_issuer,
                "subject_confidence": 0.0,
                "needs_human_review": True,
                "is_private": True,
            }
        if ref.filing_type in (FilingType.FORM_13F_HR, FilingType.FORM_13F_HR_A):
            # event-level: a new 13F is a portfolio snapshot, not a single name
            return {
                "subject_ticker": None, "subject_issuer": None,
                "subject_confidence": 0.0, "needs_human_review": True,
                "is_private": False,
            }
        res = self.client.resolve_subject(ref, self.mapper)
        return {
            "subject_ticker": res.mapped_ticker,
            "subject_issuer": res.subject_issuer,
            "subject_confidence": round(res.mapping_confidence, 3),
            "needs_human_review": res.needs_human_review,
            "is_private": False,
        }

    def _falsification_for(self, ticker: Optional[str]) -> str:
        if not ticker:
            return ""
        clusters = self.proxy_map.clusters_for_ticker(ticker)
        if not clusters:
            return ""
        dominant = max(clusters, key=lambda kv: kv[1].quality())[0]
        return self.proxy_map.falsification_for(dominant) or ""

    # alert construction -----------------------------------------------------
    def build_alert(self, ref: FastFilingRef, *, resolve: bool,
                    as_of: Optional[date] = None, discovered_via: str = "cik_feed",
                    matched_term: str = "",
                    discovery_principal: str = "") -> PersonAlert:
        principal, weight, ent_id = self._principal_link(ref.cik, ref.entity)
        # FTS discovery: the filer is not in the tracked graph, but the filing
        # TEXT names a tracked principal. That is real (if unconfirmed) person-
        # directness — floor the path weight and ALWAYS flag it for review.
        is_new_entity = False
        if discovered_via == "edgar_fts" and not ent_id:
            is_new_entity = True
            if discovery_principal:
                principal = discovery_principal
                weight = max(weight, 0.5)        # named-in-filing, control unconfirmed
        category, kind = _CATEGORY.get(
            ref.filing_type, (ref.filing_type.value, "filing"))
        if is_new_entity:
            category = f"NEW ENTITY · {category}"
        meta = filing_meta(ref.filing_type)

        subj = {"subject_ticker": None, "subject_issuer": None,
                "subject_confidence": 0.0, "needs_human_review": True,
                "is_private": ref.filing_type in (FilingType.FORM_D,
                                                  FilingType.FORM_D_A)}
        if resolve and ref.filing_type in EARLY_FORMS:
            try:
                subj = self._resolve_subject(ref)
            except Exception as exc:                    # pragma: no cover
                logger.warning("subject resolve failed (%s): %s",
                               ref.accession, exc)

        subject_tag = (subj["subject_ticker"] or subj["subject_issuer"]
                       or ("private issuer" if subj["is_private"]
                           else "subject pending review"))
        principal_name = {"thiel": "Peter Thiel",
                          "aschenbrenner": "Leopold Aschenbrenner"}.get(
                              principal, "tracked network")
        headline = f"{ref.entity}: {category} → {subject_tag}"
        why = self._why(meta.role, kind, principal_name, weight,
                        subj["is_private"])
        needs_review = subj["needs_human_review"] or is_new_entity
        if is_new_entity:
            why = (f"NEW ENTITY discovered via EDGAR full-text search for "
                   f"“{matched_term}” — filer {ref.entity} (CIK {ref.cik}) is NOT "
                   f"yet tracked. Names {principal_name} in the filing; control "
                   f"link UNCONFIRMED → verify and add to the entity graph. ") + why

        return PersonAlert(
            entity=ref.entity, entity_id=ent_id, cik=ref.cik,
            filing_type=ref.filing_type.value, form_raw=ref.form,
            role=meta.role.value, category=category, signal_kind=kind,
            filing_date=ref.filing_date, age_days=ref.age_days(as_of),
            accession=ref.accession, url=ref.url,
            principal=principal, path_weight=weight,
            link_confidence=_confidence_label(weight),
            subject_ticker=subj["subject_ticker"],
            subject_issuer=subj["subject_issuer"],
            subject_confidence=subj["subject_confidence"],
            needs_human_review=needs_review,
            is_private=subj["is_private"], is_fact=True,
            headline=headline, why_it_matters=why,
            falsification=self._falsification_for(subj["subject_ticker"]),
            discovered_via=discovered_via, is_new_entity=is_new_entity,
            matched_term=matched_term,
        )

    @staticmethod
    def _why(role: SignalRole, kind: str, principal_name: str,
             weight: float, is_private: bool) -> str:
        lead = ("EARLY signal (days-fast)" if role is SignalRole.EARLY
                else "CONFIRMATION (quarterly, lagged)")
        tier = ("a confirmed vehicle" if weight >= 0.75
                else "an affiliated entity" if weight >= 0.45
                else "adjacent network smart money")
        base = f"{lead}; filer is {tier} of {principal_name} (path {weight:.2f})."
        extra = {
            "insider_trade": " Insider transactions are the fastest, highest-conviction person-signal.",
            "new_stake": " A fresh >5% activist stake is a deliberate, disclosed conviction position.",
            "private_round": " A private-market footprint that NEVER appears in 13F — verify the public proxy separately.",
            "quarterly_13f": " Triggers a position-level diff (new / add / trim / exit) against the prior 13F.",
        }.get(kind, "")
        if is_private:
            extra += " No public ticker — treat as a derived/second-order lead, not a direct investment."
        return base + extra

    # feed + dedup -----------------------------------------------------------
    def _recent_relevant(self, since_days: int,
                         as_of: Optional[date]) -> list[FastFilingRef]:
        as_of = as_of or date.today()
        rows: list[FastFilingRef] = []
        seen_acc: set[str] = set()
        for m in self.managers:
            cik = m.get("cik")
            if not cik:
                continue
            for r in self.client.recent_filings(cik, m.get("name", "")):
                if r.filing_type not in RELEVANT_FORMS:
                    continue
                if r.accession in seen_acc:
                    continue
                age = r.age_days(as_of)
                if age is None or age < 0 or age > since_days:
                    continue
                seen_acc.add(r.accession)
                rows.append(r)
        return rows

    def new_alerts(self, store: SeenStore, *, since_days: int = 45,
                   as_of: Optional[date] = None, resolve: bool = True,
                   min_path_weight: float = 0.0) -> list[PersonAlert]:
        """Return alerts for filings not yet in the dedup store.

        Subjects are resolved ONLY for genuinely new early filings, to keep the
        live SEC request count minimal.
        """
        as_of = as_of or date.today()
        fresh = [r for r in self._recent_relevant(since_days, as_of)
                 if store.is_new(r.accession)]
        seen_acc = {r.accession for r in fresh}
        alerts: list[PersonAlert] = []
        for ref in fresh:
            alert = self.build_alert(ref, resolve=resolve, as_of=as_of)
            if alert.path_weight < min_path_weight:
                continue
            if resolve and ref.filing_type in (FilingType.FORM_13F_HR,
                                               FilingType.FORM_13F_HR_A):
                self._enrich_13f_changes(alert, ref)
            alerts.append(alert)

        # VORFELD: EDGAR full-text-search discoveries (incl. NEW entities)
        for hit in self._fts_discoveries(since_days, as_of):
            ref = hit.ref
            if not ref.accession or ref.accession in seen_acc:
                continue
            if not store.is_new(ref.accession):
                continue
            seen_acc.add(ref.accession)
            alert = self.build_alert(
                ref, resolve=resolve, as_of=as_of, discovered_via="edgar_fts",
                matched_term=hit.matched_term,
                discovery_principal=hit.principal)
            if alert.path_weight < min_path_weight:
                continue
            alerts.append(alert)

        alerts.sort(key=lambda a: a.sort_key)
        return alerts

    def _fts_discoveries(self, since_days: int, as_of: date) -> list:
        """Run the configured full-text queries (no-op without an FTS client)."""
        if not self.fts_client or not self.fts_terms:
            return []
        try:
            return self.fts_client.discover(
                self.fts_terms, since_days=since_days, as_of=as_of)
        except Exception as exc:                        # pragma: no cover
            logger.warning("FTS discovery failed: %s", exc)
            return []

    def _enrich_13f_changes(self, alert: PersonAlert, ref: FastFilingRef) -> None:
        """Best-effort: turn a new 13F-HR into a position-level diff vs the prior
        quarter. Degrades silently to the event-level alert on any failure."""
        try:
            from .holdings_diff import (
                diff_infotables, fetch_infotable_xml, summarize_changes,
            )
            prior = self._prior_13f(ref)
            cur_xml = fetch_infotable_xml(self.client.fetcher, ref.cik,
                                          ref.accession)
            if not cur_xml or prior is None:
                return
            prior_xml = fetch_infotable_xml(self.client.fetcher, prior.cik,
                                            prior.accession) or ""
            changes = diff_infotables(cur_xml, prior_xml, self.mapper)
            if not changes:
                return
            alert.position_changes = [c.__dict__ for c in changes]
            summary = summarize_changes(changes)
            if summary:
                alert.why_it_matters += f" Position changes: {summary}."
        except Exception as exc:                        # pragma: no cover
            logger.warning("13F diff enrichment failed (%s): %s",
                           ref.accession, exc)

    def _prior_13f(self, ref: FastFilingRef) -> Optional[FastFilingRef]:
        """The 13F-HR immediately preceding `ref` for the same filer (CIK)."""
        same = [r for r in self.client.recent_filings(ref.cik, ref.entity)
                if r.filing_type in (FilingType.FORM_13F_HR,
                                     FilingType.FORM_13F_HR_A)]
        same.sort(key=lambda r: r.filing_date, reverse=True)
        seen_current = False
        for r in same:
            if seen_current:
                return r
            if r.accession == ref.accession:
                seen_current = True
        # current not found in list (defensive): take the newest that isn't ref
        for r in same:
            if r.accession != ref.accession:
                return r
        return None


# ── three-axis scoring (person · freshness · tradeability) ──────────────────
# first-party statements carry real person-directness; reported media less so.
_STMT_TIER_PATH = {
    SourceTier.OFFICIAL: 0.9, SourceTier.PRIMARY: 0.75,
    SourceTier.MEDIA: 0.35, SourceTier.REPOST: 0.2,
}


def _make_tradeability_fn(cfg: AppConfig, mode: str, fixtures):
    """A memoised ticker→(market_timing, options_quality) provider backed by the
    existing pipeline engine (offline fixtures / live yfinance). Returns None if
    the pipeline cannot be built; per-ticker failures return None (→ tradeability
    stays conservatively low)."""
    try:
        from ..pipeline import Pipeline
        pipe = Pipeline(config=cfg, mode=("live" if mode == "live" else "offline"),
                        fixtures_dir=fixtures)
    except Exception as exc:                            # pragma: no cover
        logger.warning("tradeability pipeline unavailable: %s", exc)
        return None
    cache: dict[str, Optional[tuple[float, float]]] = {}

    def fn(ticker: Optional[str]) -> Optional[tuple[float, float]]:
        if not ticker:
            return None
        if ticker in cache:
            return cache[ticker]
        res: Optional[tuple[float, float]] = None
        try:
            snap = pipe.market.get_snapshot(ticker)
            contracts = pipe.options.get_call_contracts(
                ticker, snap.spot, snap.hist_vol_annual)
            mt, *_ = pipe.engine.score_market(snap)
            oq, *_ = pipe.engine.score_options_env(contracts, snap.hist_vol_annual)
            res = (float(mt), float(oq))
        except Exception:
            res = None
        cache[ticker] = res
        return res
    return fn


def _score_alert(alert: PersonAlert, scorer: TripleScorer, fn) -> None:
    has_ticker = bool(alert.subject_ticker) and not alert.needs_human_review
    mt = oq = None
    if has_ticker and fn:
        r = fn(alert.subject_ticker)
        if r:
            mt, oq = r
    try:
        role = SignalRole(alert.role)
    except ValueError:
        role = None
    inp = TripleInput(
        ticker=alert.subject_ticker if has_ticker else None,
        path_weight=alert.path_weight, verified=not alert.needs_human_review,
        is_primary_source=True, role=role, age_days=alert.age_days,
        has_public_ticker=has_ticker, is_private=alert.is_private,
        market_timing=mt, options_quality=oq)
    alert.triple = scorer.score(inp).to_dict()


def _score_statement(s, scorer: TripleScorer, fn) -> None:
    tier = SourceTier.parse(s.tier)
    cand = s.derived_candidates[0] if s.derived_candidates else None
    mt = oq = None
    if cand and fn:
        r = fn(cand)
        if r:
            mt, oq = r
    inp = TripleInput(
        ticker=cand, path_weight=_STMT_TIER_PATH.get(tier, 0.3),
        verified=tier in (SourceTier.OFFICIAL, SourceTier.PRIMARY),
        is_primary_source=tier in (SourceTier.OFFICIAL, SourceTier.PRIMARY),
        source_tier=tier, age_days=s.age_days,
        has_public_ticker=bool(cand), market_timing=mt, options_quality=oq)
    s.triple = scorer.score(inp).to_dict()


# ── orchestration ───────────────────────────────────────────────────────────
@dataclass
class MonitorResult:
    new_count: int                # NEW filing alerts (what they DID)
    alerts: list[PersonAlert]
    mode: str
    run_at: str
    principal_count: int          # alerts directly linked to a principal vehicle
    statements: list = field(default_factory=list)  # NEW conviction signals (what they SAY)

    @property
    def statement_count(self) -> int:
        return len(self.statements)

    @property
    def total_new(self) -> int:
        return self.new_count + self.statement_count

    @property
    def trade_candidates(self) -> list:
        """Alerts/statements whose three-axis gate passed (all axes good enough)."""
        out = [a for a in self.alerts if a.triple.get("gate_pass")]
        out += [s for s in self.statements if s.triple.get("gate_pass")]
        return sorted(out, key=lambda x: x.triple.get("final_trade_score", 0.0),
                      reverse=True)

    def to_dict(self) -> dict:
        return {
            "run_at": self.run_at, "mode": self.mode,
            "new_count": self.new_count,
            "statement_count": self.statement_count,
            "total_new": self.total_new,
            "principal_count": self.principal_count,
            "alerts": [a.to_dict() for a in self.alerts],
            "statements": [s.to_dict() for s in self.statements],
        }


def run_monitor(config: Optional[AppConfig] = None, *, mode: Optional[str] = None,
                since_days: int = 45, state_path: str | Path | None = None,
                fixtures_dir: str | Path | None = None,
                as_of: Optional[date] = None, resolve: bool = True,
                min_path_weight: float = 0.0,
                include_statements: bool = True,
                score_tradeability: bool = True,
                persist: bool = True) -> MonitorResult:
    """Run one monitor pass: collect filings + conviction statements → dedup →
    (optionally) persist.

    Does NOT send email — that is the caller's decision (see
    :func:`monitor_and_notify`). Returns the new alerts so callers can render
    and/or email them.
    """
    from ..pipeline import DEFAULT_FIXTURES

    cfg = config or AppConfig()
    run_mode = mode or cfg.mode
    fixtures = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
    client = load_fast_client(cfg.data_sources, run_mode, fixtures)
    graph = load_graph(cfg.config_dir)
    mapper = load_mapper(cfg.config_dir)
    proxy_map = load_proxy_map(cfg.config_dir)
    managers = cfg.investors.get("managers", []) or []

    # VORFELD: EDGAR full-text-search discovery (new filings + NEW entities)
    fts_client = None
    fts_terms: list = []
    try:
        from ..config_loader import load_yaml
        es = (load_yaml("early_sources", cfg.config_dir) or {}).get("edgar_fts", {})
        if es.get("enabled"):
            from .edgar_fts import load_fts_client
            fts_client = load_fts_client(cfg.data_sources, run_mode, fixtures)
            fts_terms = es.get("terms", []) or []
            # apply the global forms filter to any term that didn't set its own
            default_forms = es.get("forms")
            if default_forms:
                fts_terms = [{**t, "forms": t.get("forms", default_forms)}
                             if isinstance(t, dict) else t for t in fts_terms]
    except Exception as exc:                            # pragma: no cover
        logger.warning("early_sources/FTS not loaded: %s", exc)

    monitor = PersonMonitor(client, graph, mapper, proxy_map, managers,
                            fts_client=fts_client, fts_terms=fts_terms)
    store = SeenStore.load(state_path)
    alerts = monitor.new_alerts(store, since_days=since_days, as_of=as_of,
                                resolve=resolve, min_path_weight=min_path_weight)

    # second signal class: public conviction statements (what they SAY)
    statements: list = []
    if include_statements:
        try:
            from .statement_feed import load_statement_monitor
            sm, feeds = load_statement_monitor(
                cfg.config_dir, cfg.data_sources, run_mode, fixtures)
            for s in sm.collect(feeds, since_days=since_days, as_of=as_of):
                if store.is_new(s.content_hash):
                    statements.append(s)
        except Exception as exc:                        # pragma: no cover
            logger.warning("statement collection failed: %s", exc)

    # three-axis score + hard AND-gate on every signal (person · freshness ·
    # tradeability) — a trade-candidate needs ALL THREE good enough.
    scorer = TripleScorer(GateThresholds.from_config(getattr(cfg, "scoring", None)))
    trade_fn = (_make_tradeability_fn(cfg, run_mode, fixtures)
                if score_tradeability else None)
    for a in alerts:
        _score_alert(a, scorer, trade_fn)
    for s in statements:
        _score_statement(s, scorer, trade_fn)

    if persist and (alerts or statements):
        stamp = (as_of or date.today()).isoformat()
        for a in alerts:
            store.seen[a.accession] = {
                "first_seen": stamp, "entity": a.entity, "cik": a.cik,
                "form": a.form_raw, "filing_date": a.filing_date,
            }
        for s in statements:
            store.seen[s.content_hash] = {
                "first_seen": stamp, "kind": "statement",
                "speaker": s.speaker, "source": s.source, "date": s.date,
            }
        store.save()

    principal_count = sum(1 for a in alerts if a.path_weight >= 0.75)
    return MonitorResult(
        new_count=len(alerts), alerts=alerts, mode=run_mode,
        run_at=datetime.now(timezone.utc).isoformat(),
        principal_count=principal_count, statements=statements)
