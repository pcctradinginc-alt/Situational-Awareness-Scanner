# Situational Awareness Scanner

Automatisiertes CALL-Options Intelligence System basierend auf Aschenbrenners
„The Decade Ahead". Identifiziert handelbare CALL-Optionen auf KI-Infrastruktur
durch Kombination von 13F-Monitoring, philosophisch-strategischer These-Analyse
(Thiel, Shulman) und Marktdaten.

> ⚠️ **Research / Paper Mode only — kein Live-Trading.** Das System erzeugt
> ausschließlich erklärbare, gerankte Trade-Kandidaten. Es platziert niemals
> Orders. Keine Anlageberatung. Optionen können wertlos verfallen (Totalverlust).

---

## CALL-Options Intelligence (free-data subsystem) 🆕

A self-contained, **free-data, research-only** subsystem lives in
[`call_options_intel/`](call_options_intel/). It is **additive** — it does not
modify or depend on the original paid-API pipeline above — and runs fully
**offline by default** on bundled fixtures (no API keys, no network).

### What it does
Ranks explainable CALL-option candidates on a configurable AI-infrastructure
universe by combining a structured **thesis vector** (Aschenbrenner / Thiel /
Shulman themes), **SEC 13F** smart-money accumulation, free **market-data**
momentum/trend context, free **options-chain** liquidity / IV-regime context, and
**catalyst** proximity — minus an explicit **risk penalty**. It enforces
**thesis ≠ trade**: a great thesis with rich IV, a wide spread, no catalyst or a
contradictory 13F is penalised, not blindly bought.

### Setup
```bash
pip install -r requirements.txt        # adds PyYAML; yfinance already present
python -m call_options_intel doctor    # health check — expect PASS, no keys needed
```

### CLI
```bash
# Full offline scan → Markdown + CSV + JSON (HTML optional)
python -m call_options_intel scan --output reports/latest --format markdown csv json

# Restrict universe, use the configured universe file, write one format
python -m call_options_intel scan --tickers NVDA MU AVGO --output reports/core --format markdown

# Free live sources instead of fixtures (yfinance/Stooq/EDGAR)
python -m call_options_intel scan --live --output reports/live

# Explain one ticker's score; 13F changes; paper-evaluation demo
python -m call_options_intel explain NVDA
python -m call_options_intel update-13f
python -m call_options_intel backtest demo --store reports/signals.jsonl
```

### Person-Intelligence-Layer 🆕 (`call_options_intel/person_intel/`)
Tracks the **people** behind the thesis (Aschenbrenner / Situational Awareness LP,
Thiel / Founders Fund / Thiel Capital / Mithril), not just the AI-infra narrative.
Strict, auditable dataflow — **thesis ≠ trade**:

```
Entity graph → 13F/13D/Form-D signal → verification → thesis proxy
             → market/options timing → final_research_score vs final_trade_candidate_score
```

```bash
# research signal vs trade candidate (offline), with falsification per row
python -m call_options_intel person-intel --limit 15
python -m call_options_intel person-intel --tickers NVDA CEG TSM

# recent FAST filings — leading person-signals; --resolve maps the subject→ticker
python -m call_options_intel early-filings --since 30 --resolve
python -m call_options_intel early-filings --since 30 --live --resolve

# build IV-history warmup (run daily); a warmed store feeds a REAL IV rank into scoring
python -m call_options_intel record-iv --store reports/iv_history.jsonl
python -m call_options_intel scan --output reports/latest --person-intel \
    --iv-history reports/iv_history.jsonl

# multi-horizon outcomes + walk-forward edge guard (demo seeds a synthetic store)
python -m call_options_intel outcomes-report --demo --min-sample 3
```

#### 📡 Live person-signal radar (`person-monitor`)
The twice-daily **radar**: it pulls the recent EDGAR feed for every tracked
Thiel / Aschenbrenner vehicle, **de-duplicates against a persistent store**
(`data/person_intel/seen_filings.json`) so each filing fires exactly once, and
— with `--email` — sends a digest **only when there is a genuinely new signal**.

```bash
# offline dry-run (fixtures): show what the radar would report, persist nothing
python -m call_options_intel person-monitor --dry-run --since 45

# live SEC (free, no key); email ONLY on a new signal (Gmail secrets via env)
python -m call_options_intel person-monitor --live --since 14 --email

# keep only confirmed-vehicle signals (drop adjacent-network noise)
python -m call_options_intel person-monitor --live --min-weight 0.75
```

Each alert keeps **facts and hypotheses typed apart**: the *filing* is a fact
(accession + SEC URL); the derived *subject ticker* is asserted only when a
check-digit-valid CUSIP maps with confidence (e.g. a Thiel SC 13D/A → `PLTR`),
otherwise it stays `needs_human_review` — never guessed. A Form D is flagged as
a **private / second-order** lead (no public ticker), and every mapped name
carries its thesis-cluster **falsification condition**. Filings are graded by the
controlled-path weight from a principal, so a confirmed Thiel vehicle outranks
merely adjacent smart money. Runs automatically via
[`.github/workflows/person_intel_monitor.yml`](.github/workflows/person_intel_monitor.yml)
at 06:00 + 14:00 UTC (German morning / afternoon across DST).

**Three-axis trade gate (the sharpened question).** The radar does not ask
"which AI-infra call looks good?" but *"which publicly tradeable stock/option best
reflects a NEW, VERIFIABLE capital- or conviction-move by Aschenbrenner / Thiel —
before it is broadly priced in?"* Every signal therefore carries **three
orthogonal scores** (`person_intel/triple_score.py`):
- **Person-signal** — how *directly* it hangs on Leo / Thiel / SA LP / Founders Fund
  (controlled-path directness × verification × primary source);
- **Freshness/latency** — how *early* it is vs the market (a leading Form 4/13D or a
  fresh first-party essay scores high; a 44-day-old 13F is already priced → low);
- **Tradeability** — is a liquid **call/spread derivable now** (a resolved *public*
  ticker with liquid options + acceptable timing; private/unmapped/no-options ⇒ 0).

A **TRADE-CANDIDATE** is proposed only when **all three clear their gate** — a hard
AND (`config/scoring_weights.yml → person_gates`), never a sum; the final score is
the *weakest link*. The digest/email lead with a Trade-Candidates section; a Thiel
SC 13D/A → `PLTR` (fresh + liquid) passes, while an unresolved Form 4, a stale 13F,
or a private Form D do not.

**Vorfeld discovery — earlier than the market, and beyond the filers we already
know.** Besides the per-CIK feed (which only sees tracked filers), the radar runs
**EDGAR Full-Text Search** (`person_intel/edgar_fts.py`, `config/early_sources.yml`)
over the tracked *names* (Peter Thiel, Founders Fund, Mithril, Leopold
Aschenbrenner, Situational Awareness, …) across **all** filers. A hit by a CIK we
do **not** yet track is a **NEW-ENTITY discovery** — a new LP / affiliate / fund
vehicle — surfaced in its own digest section with the filing that named the
principal (control link *unconfirmed* → `needs_human_review`, never assumed). FTS
is SEC-primary only (never media), keyless and free; its hits flow through the same
taxonomy → three-axis gate as every other filing.

**Non-filing vorfeld change-detection** (`person_intel/vorfeld.py`,
`config/early_sources.yml`) adds a third early-signal class — fetch→snapshot→diff
adapters that react to a *change*, not a level (first sight = baseline):
**ADV/IAPD** (AUM moves, new control persons / private funds, office moves),
**job postings** (Greenhouse/Lever — new roles classified into thesis clusters as
a hiring/theme proxy) and **website/domain watch** (content-hash change of official
fund pages → new LP/fund footprint). All advisory (`needs_human_review`),
deduped, triple-scored as CONTEXT, and shown in a "📡 Vorfeld" section.

The radar carries **two filing/statement signal classes** plus the vorfeld class
above (all dedup'd, all can trigger the email):
- **Filings — what they DID** (the SEC feed above), and
- **Conviction — what they SAY**: public first-party essays
  (situational-awareness.ai, forourposterity.com) and name-matched news RSS, via
  [`config/statement_sources.yml`](config/statement_sources.yml). A statement is
  classified into a thesis cluster and yields **second-order, derived candidates**
  (e.g. an Aschenbrenner power/compute essay → `VST, CEG, GEV`) — explicitly a
  *watchlist hypothesis*, **never** a confirmed investment, always
  `needs_human_review`, discovery-only (short excerpt + hash, never full text).

Statement classification can optionally use an LLM (extract/classify only, never
the final word) via `ANTHROPIC_API_KEY` (env only); without it, a transparent
keyword model is used. Subject→ticker resolution and the LLM path are both
off-by-default and degrade gracefully.

Building blocks (all offline, deterministic, additive, never trade):
- **Entity graph** (`config/entity_graph.yml`) — confidence levels, facts vs
  hypotheses, path-confidence discounting.
- **Filing taxonomy** — 13F-HR/A vs SC 13D/A vs SC 13G/A vs Form 4 vs Form D/A,
  plus a **non-EDGAR ADV/IAPD** adapter; 13F instrument typing
  (Common/CALL/PUT/ADR/ETF) where options are `direction_unknown`, and a
  verification status (`verified` … `not_verifiable_via_13f` … `rejected_noise`).
- **CUSIP map** (`config/cusip_map.yml`) — `mapped_ticker` + `confidence` +
  `mapping_source` + `needs_human_review`, with check-digit validation. No match
  without confidence.
- **Statement layer** — discovery-only (URL + hash + source + date + short
  excerpt, **never** full text), official > media, advisory classification.
- **Thesis proxies** (`config/thesis_proxies.yml`) — 8 clusters (compute,
  power_grid, nuclear, cooling, networking, defense_ai, export_controls,
  automation) with private→public 1st/2nd/3rd-order proxies and a **mandatory
  falsification condition** per cluster.
- **Conservative options** — fill toward the ask (not mid), theta/vega reprice
  sim, IV-rank only after warmup, paper-only sizing.
- **Outcome learning** — append-only (incl. rejected), 7/14/30/60/90/180-day
  horizons, score/source/thesis/regime buckets, QQQ/SOXX benchmark, and a
  walk-forward + min-sample guard (no profit claim without out-of-sample evidence).

See [docs/improvement_plan.md](docs/improvement_plan.md).

### Configuration (no code edits needed)
`config/ai_infra_universe.yml` (universe), `config/scoring_weights.yml` (weights &
penalties), `config/risk_thresholds.yml` (DTE/strike/liquidity/IV guardrails),
`config/data_sources.yml` (providers & offline/live mode),
`config/investors_13f.yml` (tracked managers), `config/report_settings.yml`,
`config/entity_graph.yml` · `config/cusip_map.yml` · `config/thesis_proxies.yml`
(person-intelligence layer).

### Interpreting scores
- **final_score (0–10)** = weighted positives − risk penalty (≥6.5 top, ≥4.5 watchlist).
- **confidence (high/med/low)** = *data completeness*, not conviction — never size a `low` row.
- **⚡ event trade** = earnings inside the flag window → IV-driven, expect IV crush.
- **reasons_against** + **data-quality warning** appear on every candidate.

### Free-data limitations
Free sources are delayed/incomplete; IV percentile is a realised-vol proxy (no IV
history store yet); option-proxy backtest returns are first-order delta/intrinsic
approximations, **not** realised option P&L. No profitability is claimed without
recorded out-of-sample evidence (`backtest evaluate`).

### Docs
[Architecture](docs/call_options_intelligence_architecture.md) ·
[Runbook](docs/runbook_call_options_scanner.md) ·
[Skill](.claude/skills/call-options-intelligence/SKILL.md) ·
[Skills overview](SKILLS.md)

### No-live-trading disclaimer
This subsystem **never** executes trades and contains **no** broker/order code.
`doctor` actively greps the package for order-execution patterns and fails loudly
if any are ever introduced. Paper/research only. Not investment advice.

---

## Architektur

```
main.py                         Orchestrierung
scanner/
  sources/
    tradier_client.py           Tradier Vollzugriff (Option Chain, Greeks, IV-Historie)
    data_fetcher.py             Koordiniert alle Quellen (yfinance, EIA, FRED, Finnhub, RSS)
    sec_edgar.py                13F-Filing Monitor
  signals/
    regime_detector.py          Normal/Stress-Modus Bestimmung
    contrarian_gate.py          Gegenthesen-Check (verhindert Überzeugungsschleifen)
    shulman_layer.py            Empirische Validierung + qualitative Extraktion
    thiel_layer.py              Handlung vs. These + Katechon-Bonus
  analysis/
    pre_filter.py               Quick-Score ohne Claude (Token-Ökonomie)
    scoring_engine.py           Gewichteter Conviction-Score
    claude_analyzer.py          Anthropic API + Master-Prompt
  output/
    trading_card_generator.py   JSON → HTML Trading Card
    dashboard_generator.py      GitHub Pages Dashboard
  utils/
    config.py                   Alle Konstanten und Gewichte
    state_manager.py            SQLite + Git-Commit Persistenz
    ticker_mapper.py            CIK → Ticker → Sektor Mapping
    rate_limiter.py             Pro-API Rate-Limiting
```

## Setup

### 1. Repository

```bash
git clone https://github.com/DEIN-USER/sa-scanner
cd sa-scanner
pip install -r requirements.txt
```

### 2. GitHub Secrets

Unter `Settings → Secrets and variables → Actions`:

| Secret | Quelle | Pflicht |
|--------|--------|---------|
| `ANTHROPIC_API_KEY` | console.anthropic.com | ✅ |
| `TRADIER_API_KEY` | tradier.com/user/applications | ✅ |
| `FINNHUB_API_KEY` | finnhub.io | ✅ |
| `EIA_API_KEY` | eia.gov/opendata | Empfohlen |
| `FRED_API_KEY` | fred.stlouisfed.org | Empfohlen |

### 3. GitHub Actions Schreibrechte

`Settings → Actions → General → Workflow permissions → Read and write permissions`

### 4. GitHub Pages

`Settings → Pages → Source → GitHub Actions`

### 5. CIK-Nummern verifizieren

In `scanner/utils/config.py` die `SEC_CIK_TARGETS` über
[EDGAR Company Search](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany)
verifizieren.

### 6. Lokaler Test

```bash
# Nur Daten fetchen (kein Claude)
python main.py --no-claude

# Spezifische Ticker
python main.py --ticker VST PLTR

# Nur EDGAR-Check
python main.py --edgar-only

# Tests
pytest tests/ -v
```

## Gewichtungsstruktur

| Layer | Normal | Stress |
|-------|--------|--------|
| SA LP Alignment | 40% | 50% |
| Thiel (inkl. Philosophical) | 14% | 10% |
| Shulman-Metriken | 15% | 13% |
| Multi-Signal-Gate | 4% | 3% |
| Markt-Regime | 15% | 12% |
| Contrarian Gate | 12% | 12% |

**Conviction-Schwellenwert:** ≥ 7.5 (Normal) / ≥ 8.0 (Stress)

**Contrarian Gate:** Bei Score < -3.0 wird der Trade blockiert (binäres Gate).

## Kosten

| Quelle | Kosten |
|--------|--------|
| Anthropic API (claude-sonnet-4-6) | ~2-3 USD/Monat (Pre-Filter) |
| Tradier (Vollzugriff) | Laut Plan |
| Alle anderen Quellen | kostenlos |

## Wichtige Einschränkungen

Das System liefert **Richtungs-Signale**, keine Timing-Garantien.

- Philosophische Signale (Thiel-These) sind Priors, keine Trigger
- Shulman-Signale liegen 6-12 Monate vor dem Mainstream-Markt
- IV-Rank ist erst nach 30+ Tagen eigener Datensammlung zuverlässig (Warmup-Phase)
- Der Contrarian Gate ist der einzige Schutz gegen geschlossene Überzeugungsschleifen
- Nicht investiere niemals mehr als du bereit bist zu verlieren

## Lizenz

Privat — nicht für kommerzielle Weitergabe.
