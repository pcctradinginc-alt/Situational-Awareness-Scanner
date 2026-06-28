# Improvement Plan — Person-Intelligence-Layer

> **Scope:** `call_options_intel/` (the free-data, research/paper-only subsystem).
> The legacy paid `scanner/` pipeline is out of scope and untouched.
> **Mode:** offline-by-default, deterministic, no paid APIs, **never trades**.

## Core problem

The system tracks the **AI-infrastructure thesis** well, but it does not really
track the **people** the thesis is supposed to follow (Leopold Aschenbrenner /
Situational Awareness LP, Peter Thiel / Founders Fund / Thiel Capital / Mithril).
The thesis vector is a keyword lexicon over essays — it is a *prior*, not
*evidence* that a specific person did or said something actionable.

The fix is a **Person-Intelligence-Layer** with a strict, auditable dataflow:

```
Entity  ──▶  Signal (filing / statement)  ──▶  Evidence (verified, typed)
                                                   │
                                                   ▼
                                Thesis cluster  ──▶  private→public Proxy
                                                   │
                                                   ▼
                              Research score  ──▶  (separate) Trade-candidate score
```

**Thesis ≠ trade.** A strong, well-sourced *research* signal does **not** imply
a tradeable *option* candidate. The two scores are computed and reported
separately. Every alert carries a counter-argument and an explicit
**falsification condition**.

## Design principles

1. **Facts vs hypotheses are typed, never blurred.** Every entity, relation and
   claim carries a `confidence` and an `is_fact` / status flag.
2. **Filings are differentiated, not lumped.** 13F-HR/A ≠ SC 13D/A ≠ SC 13G/A ≠
   Form D/D-A. 13F is *quarterly confirmation*; 13D/G and Form 4/Form D are
   faster. ADV/IAPD is **not** an EDGAR filing — separate adapter.
3. **13F instruments are typed** (Common / CALL / PUT / ADR / ETF / NOTE). A 13F
   CALL line is **not** read as naive bullishness — direction is
   `direction_unknown` unless corroborated.
4. **No CUSIP→ticker match without a confidence + source.** Unknowns are flagged
   `needs_human_review`, never silently guessed.
5. **Statement layer is discovery-only.** We store URL + content hash + source +
   date + a short excerpt — **never** full third-party text. Official/primary
   sources outrank media reposts. The LLM (when used) *extracts and classifies*;
   it does not make the final call.
6. **Every thesis cluster has a falsification condition.** If it triggers, the
   proxy chain is demoted.
7. **No profitability claim without out-of-sample evidence** and a minimum
   sample size.

## Module map (new sub-package `call_options_intel/person_intel/`)

| Module | Goal | Status |
|--------|------|--------|
| `entities.py` + `config/entity_graph.yml` | A — entity graph, confidence, fact/hypothesis | ✅ this PR |
| `filings.py` | B/C — filing taxonomy, instrument type, verification status, `direction_unknown` | ✅ this PR |
| `cusip_map.py` + `config/cusip_map.yml` | D — CUSIP→ticker w/ confidence + `needs_human_review` | ✅ this PR |
| `statements.py` | E — discovery-only statement refs, dedup, official>media | ✅ this PR |
| `proxy_map.py` + `config/thesis_proxies.yml` | F — private→public proxy, order, evidence, falsification | ✅ this PR |
| `person_scoring.py` | G — split research vs trade scores, each with reasoning | ✅ this PR |
| `fills.py` | H — conservative fill, theta/vega sim, paper sizing | ✅ this PR |
| `outcomes.py` | I — multi-horizon outcome learning, benchmarks, walk-forward guard | ✅ this PR |

CLI: new `person-intel` subcommand renders the entity→evidence→proxy→score
chain on fixtures; `doctor` validates the new configs and extends the
no-live-trading guard over the sub-package.

## Scoring split (G)

Positive components, each 0..10 with reasoning + raw inputs:

- `person_signal_score` — did a tracked person/entity actually *do/say* something
  (filing action, statement), weighted by entity confidence + timeliness.
- `source_reliability_score` — official/primary > media; verified > unverified.
- `verification_score` — `verified` / `partially_verified` /
  `not_verifiable_via_13f` / `needs_human_review` / `rejected_noise`.
- `thesis_alignment_score` — fit to the thesis clusters.
- `proxy_quality_score` — 1st-order > 2nd > 3rd; evidence level.
- `market_timing_score` — momentum/trend/regime (delegates to existing engine).
- `options_quality_score` — liquidity / IV-richness / fill quality (existing).
- `risk_penalty` — additive, capped.

→ `final_research_score` = person + source + verification + thesis + proxy
   (the *people are doing/saying X, and X maps to public proxy Y* claim).
→ `final_trade_candidate_score` = research **gated** by market_timing +
   options_quality − risk_penalty, and **capped** when verification is weak.
   A high research score with no verification, no liquid options, or a failed
   falsification check **cannot** become a top trade candidate.

## Options realism (H)

- **Conservative fill**, not mid: model the buyer crossing toward the ask with a
  configurable haircut; record `conservative_fill` separately from `mid`.
- **Theta/vega first-order sim** so a long call into a flat tape and an IV-crush
  event are both penalised explicitly.
- **OI/Volume gates** (already present) + **IV warmup** (IV-rank only after a
  warmup window; before that, use the realised-vol richness proxy).
- **Earnings IV-crush** as an explicit penalty (already present; documented).
- **Paper-only sizing** helper — risk-fraction of a notional paper book, never a
  live order.

## Outcome learning (I)

- Append-only signal store **including rejected candidates** (so we can measure
  what we correctly avoided).
- Outcomes at **7 / 14 / 30 / 60 / 90 / 180** days.
- Buckets by **score / source / thesis cluster / regime**.
- Benchmarks vs **underlying / QQQ / SOXX**.
- **Walk-forward** split + **minimum sample** gate: `summarize_oos()` refuses to
  report an edge below the configured sample size and without an out-of-sample
  fold.

## Tests / CI (J)

CUSIP mapping (+ `needs_human_review`); filing taxonomy & instrument typing;
`not_verifiable_via_13f`; 13D / Form-D parsing; statement dedup; conservative
fill `< mid`; IV warmup; **score monotonicity** (research vs trade); entity-graph
confidence ordering; proxy falsification presence; **no-live-trading guard** over
the new sub-package. Branch/PR only — no `main` push, no secrets in the diff.

## Phasing

- **Phase 1 (done):** all modules above as additive, tested, offline units +
  CLI + doctor + docs. Pipeline behaviour for the existing `scan` is unchanged.
- **Phase 2 (done):**
  - `person_intel/edgar_fast.py` — free, off-by-default EDGAR adapter for the FAST
    filings (Form 4 / SC 13D-G / Form D); injectable HTTP layer (offline fixtures
    / live SEC), CLI `early-filings`.
  - `person_intel/iv_history.py` — persistent IV-history store; real IV percentile
    after a warmup, else the realised-vol proxy fallback; CLI `record-iv`.
  - person-intel **research-vs-trade panel** wired into `scan --person-intel`
    (separate `<stem>_person_intel.md`/`.json` artifact; the base scan is
    unchanged). A full ADV/IAPD live fetch remains a stub (context-only).
- **Phase 3 (done):**
  - 13D/G/Form-4 **subject→ticker resolution** (`edgar_fast.resolve_subject`):
    extract a check-digit-valid CUSIP from the document and map it; never guesses
    (`early-filings --resolve`).
  - **Optional LLM statement classifier** (`llm_classify.py`): injectable,
    extract/classify only, `ANTHROPIC_API_KEY` from env (never logged), keyword
    fallback; results stay advisory.
  - **Warmed-up IV rank wired into live options scoring** (`scan --iv-history`):
    real IV percentile replaces the realised-vol proxy once a ticker is warmed up;
    threaded additively (default None = unchanged behaviour).
  - **Outcomes walk-forward report** (`outcomes-report`): multi-horizon summary +
    a guard that refuses an edge claim without a sufficient out-of-sample fold.
- **Phase 4 (in progress):**
  - **Real per-horizon historical pricing** (`historical.py`,
    `outcomes-report --historical/--live`): date-aware close lookup (offline CSV
    fixtures / Stooq) prices each signal at recorded_date+horizon and computes real
    QQQ/SOXX benchmark returns; never fabricates a price beyond the series. ✅
  - **Widened CUSIP map** (29 curated): well-documented large caps, each
    check-digit-validated. Tickers whose CUSIP could not be authoritatively
    confirmed (GEV/EQIX/SMCI) are deliberately left to the name heuristic
    (`needs_human_review`) rather than risk a wrong CUSIP key. ✅
  - Remaining: live ADV/IAPD fetch; accumulate real paper outcomes to a genuine
    out-of-sample edge report; broaden Tier-A CUSIP coverage from an authoritative
    source; richer 13D/Form-D HTML parsing.
- **Phase 5 (done) — the live twice-daily radar:**
  - `person_intel/monitor.py` — `PersonMonitor` collects the recent EDGAR feed
    for every tracked Thiel/Aschenbrenner CIK (reusing the injectable
    `EdgarFastClient`: offline fixtures by default, live SEC on opt-in), classifies
    each filing into a typed signal category (insider / new stake / passive stake /
    private placement / quarterly 13F) with its early-vs-confirmation role, and
    grades it by the controlled-path weight from a principal.
  - **`SeenStore`** — append-only JSON dedup keyed by SEC accession number (the
    git-versioned *historical signal archive*). A filing fires **exactly once**,
    which is the basis for *email only on a NEW signal*.
  - **Fact vs hypothesis stays typed:** the filing is a fact (accession + URL); the
    subject ticker is asserted only when a CUSIP maps with confidence (e.g. Thiel
    SC 13D/A → PLTR), else `needs_human_review`. Form D = private/second-order lead.
  - `person_intel/monitor_report.py` — Markdown + Apple-style HTML digest that
    separates facts from hypotheses and prints each cluster's falsification;
    `send_person_email` (Gmail SMTP, **credentials from env only, never logged**)
    sends **only when `new_count > 0`**; `monitor_and_notify` is the entrypoint.
  - CLI `person-monitor` (`--live/--since/--email/--min-weight/--dry-run`);
    `doctor` validates principals + tracked CIKs.
  - `.github/workflows/person_intel_monitor.yml` runs 06:00 + 14:00 UTC (German
    morning/afternoon across DST), live SEC (keyless), emails only on a new signal,
    commits state under `data/person_intel/`. Email is optional and degrades
    gracefully if Gmail secrets are absent.
  - Tests: `tests/test_coi_person_monitor.py` — collection/typing, role honesty,
    sourced-vs-unguessed subject, principal linkage, **dedup fires once**, dry-run
    persists nothing, digest fact/hypothesis split, email-skipped-when-unconfigured.
  - **Second signal class — conviction (what they SAY):**
    `person_intel/statement_feed.py` + `config/statement_sources.yml` turn public
    first-party essays (situational-awareness.ai, forourposterity.com) and
    name-matched news RSS into conviction signals — reusing the discovery-only
    `statements.py` (url + short excerpt + hash + advisory cluster classification,
    never full text) and `proxy_map` to derive **second-order candidates**
    (e.g. a power/compute essay → VST/CEG/GEV) that are HYPOTHESIS/watchlist only,
    never confirmed investments, always `needs_human_review`. They flow through the
    **same `SeenStore` dedup** (namespaced `stmt:` keys) and the same email — so a
    new essay can trigger the radar just like a new filing.
    `MonitorResult.total_new = filing + statement`. Injectable fetcher
    (offline fixture / live RSS via `feedparser`), off-toggle
    `include_statements`. Tests: `tests/test_coi_person_statement_feed.py`.
  - **Live 13F position-diff:** `person_intel/holdings_diff.py` turns a new
    13F-HR into a CUSIP-level diff vs the prior quarter (new / add / trim / exit
    per name) — reusing the typed info-table parser (CALL/PUT stays
    `direction_unknown`) and the person CUSIP→ticker mapper (unmapped =
    `needs_human_review`, never guessed). `fetch_infotable_xml` navigates the
    accession `index.json` to locate the holdings table (case-sensitive path);
    the monitor enriches a fresh 13F alert with the top moves and degrades to the
    event-level alert when the table can't be fetched.
    Tests: `tests/test_coi_person_holdings_diff.py`.
  - **Documented network graph:** `config/entity_graph.yml` gains well-reported
    Founders-Fund/Thiel portfolio companies (Anduril, SpaceX, Stripe — all PRIVATE,
    flagged) with fact-graded fund-exposure edges. `person_intel/network.py`
    summarises a principal's **fact-only** reachable network (public ticker vs
    private name + recurring thesis themes) via `fact_path_weight` (ignores
    hypothesis edges, so Aschenbrenner does NOT inherit Thiel's network through the
    ideological-alignment hypothesis). The digest prints a "Documented network
    context" line; private names are explicitly second-order, never tradeable.
    Tests: `tests/test_coi_person_network.py`.

- **Phase 6 (done) — the three-axis trade gate (sharpened goal):**
  Reframe the decision from "which AI-infra call looks good?" to "which publicly
  tradeable instrument best reflects a NEW, VERIFIABLE capital/conviction move by
  Aschenbrenner/Thiel before it is broadly priced?" `person_intel/triple_score.py`
  scores every signal on three ORTHOGONAL axes (each 0..10):
  - **person_signal** — controlled-path directness × verification × primary source;
  - **freshness** — leading filing/fresh statement decayed by age (stale 13F → low);
  - **tradeability** — resolved PUBLIC ticker + liquid options + timing
    (private/unmapped/no-options ⇒ 0).
  A **hard AND-gate** (`config/scoring_weights.yml → person_gates`) yields a
  TRADE-CANDIDATE only when all three clear their bar; `final_trade_score` is the
  weakest link. Wired into the monitor (each alert/statement gets `.triple`;
  `MonitorResult.trade_candidates`), with a Pipeline-backed tradeability provider
  (offline fixtures / live yfinance, memoised, degrades to conservative on
  failure). Digest/email lead with a Trade-Candidates section. Tests:
  `tests/test_coi_person_triple_score.py` (11) + monitor wiring tests.

- **Phase 7 (in progress) — early-signal VORFELD sources:**
  Make discovery *earlier than the market* and beyond already-tracked filers.
  - **EDGAR Full-Text Search** (`person_intel/edgar_fts.py` + `config/early_sources.yml`):
    queries the tracked NAMES (Thiel / Founders Fund / Mithril / Aschenbrenner /
    Situational Awareness) over ALL filers via the free keyless
    `efts.sec.gov/LATEST/search-index`, reduces each hit to the same
    `FastFilingRef` the pipeline already types/scores, and dedups. Injectable
    (offline fixtures / live), SEC-primary only — never media.
  - **NEW-ENTITY discovery:** an FTS hit by a CIK NOT in the entity graph is
    flagged `is_new_entity` (a new LP/affiliate/fund vehicle); the filing names a
    principal, so person-directness gets a 0.5 floor but stays
    `needs_human_review` (control UNCONFIRMED). Surfaced in a dedicated digest
    section "🆕 NEW ENTITIES discovered". This is the "neue Entitäten /
    CIK-Verknüpfungen / Fund-Namen / neue SEC-File-Numbers" path.
  - Wired into the monitor as a PRIMARY path (merged + deduped with the per-CIK
    feed, triple-scored). Expanded `statement_sources.yml` with first-party fund
    feeds (Founders Fund, Mithril) + a podcast/transcript slot; media stays
    secondary. `doctor` reports the FTS terms. Tests:
    `tests/test_coi_person_edgar_fts.py` (8).
  - **Non-filing VORFELD change-detection** (`person_intel/vorfeld.py` +
    `config/early_sources.yml`) — a third signal class, unified as
    fetch→snapshot→diff adapters (first sight = baseline, deltas fire thereafter):
    * **ADV/IAPD** (`AdvIapdAdapter`) — per-adviser CRD: AUM moves (≥10%), new
      control persons, new private funds, office moves. NOT an EDGAR filing.
    * **Job postings** (`JobPostingsAdapter`) — Greenhouse/Lever public boards:
      NEW roles classified into thesis clusters (hiring = theme-shift proxy).
    * **Domain watch** (`DomainWatchAdapter`) — content-hash change of official
      fund pages (new LP/fund/product footprint); stores only hash + title.
    All injectable (offline fixtures / live), advisory (`needs_human_review`),
    deduped via the SeenStore, triple-scored (CONTEXT-grade: modest person-signal
    so they rarely auto-trade), and surfaced in a "📡 Vorfeld" digest/email
    section. `MonitorResult.vorfeld`; counts into `total_new`. Tests:
    `tests/test_coi_person_vorfeld.py`.
  - ADV CRDs / job tokens VERIFIED live (adviserinfo.sec.gov / boards): Founders
    Fund 155462, Mithril 164135, Thiel Bio 324760; Anduril `andurilindustries`,
    Palantir `palantir`. Thiel Macro removed (not a registered adviser).
  - **Certificate Transparency** (`CertTransparencyAdapter`, crt.sh): discover
    BRAND-NEW domains/subdomains the moment their TLS cert is logged — often the
    earliest footprint of a forming fund/LP, weeks before any filing. Distinctive
    brand patterns keep noise low; each new domain is `needs_human_review`,
    classified into a thesis cluster, and recency-gated. Offline demo discovers
    `ff-compute-spv.foundersfund.com` → cluster `compute`.
  - **Remaining (next):** richer ADV parsing (SMA/AUM breakdown); podcast/
    transcript ingestion for the statement-feed primary slot.

- **Phase 8 (done) — ranked private→public proxy map:**
  Reframe the edge: not "copy the trade" but *private theme A → the most LIQUID,
  UNDERVALUED, OPTIONABLE listed proxy B*. Each proxy in `config/thesis_proxies.yml`
  now carries `liquidity` + `valuation` priors; `proxy_map.RankedProxy` /
  `rank_cluster(options_fn)` score four dimensions — **link** (order×evidence),
  **liquidity**, **options-quality** (LIVE from the pipeline when available, else
  the liquidity prior), **valuation** (cheaper = bigger edge) — into an
  `edge_score` (weights 0.40/0.25/0.20/0.15), each carrying the cluster
  falsification. Wired: statement derived-candidates now follow the edge ranking;
  `MonitorResult.ranked_proxies` (top-3 per touched cluster, live options where
  resolvable); digest "🎯 Best public proxy per private theme" section; CLI
  `proxies [--cluster …] [--live]`. Demo: in `compute`, TSM (fair) edges NVDA
  (rich) at equal link; MU climbs on cheap valuation. Tests:
  `tests/test_coi_person_proxy_rank.py` (8).

## Acceptance

Tests green; repo paper-only; clear Entity→Signal→Evidence→Candidate dataflow;
every alert carries a counter-argument + falsification; no unsupported profit
claims.
