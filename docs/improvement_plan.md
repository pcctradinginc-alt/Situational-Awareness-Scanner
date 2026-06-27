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
  - **Widened CUSIP map** (32 curated): well-documented large caps at high
    confidence; a Tier-B set is check-digit-valid but kept below `mapping_floor` so
    it is mapped yet always `needs_human_review` (honest about unconfirmed CUSIPs). ✅
  - Remaining: live ADV/IAPD fetch; accumulate real paper outcomes to a genuine
    out-of-sample edge report; broaden Tier-A CUSIP coverage from an authoritative
    source; richer 13D/Form-D HTML parsing.

## Acceptance

Tests green; repo paper-only; clear Entity→Signal→Evidence→Candidate dataflow;
every alert carries a counter-argument + falsification; no unsupported profit
claims.
