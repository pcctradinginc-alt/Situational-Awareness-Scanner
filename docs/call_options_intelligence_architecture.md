# CALL-Options Intelligence — Architecture

A free-data, research-only subsystem that ranks explainable CALL-option
candidates on AI-infrastructure equities. It is **additive**: it lives in the new
`call_options_intel/` package and does **not** modify or depend on the existing
paid-API `scanner/` pipeline. Default mode is **offline** (bundled fixtures), so a
scan always produces a report with zero network calls and zero API keys.

> RESEARCH / PAPER MODE ONLY — no live trading, ever. Not investment advice.

## Data flow

```
                 config/*.yml                 call_options_intel/fixtures/ (offline)
                      │                                   │
                      ▼                                   ▼
 ┌───────────┐   ┌──────────────────────────────────────────────────┐
 │  Universe │   │                 Data Source Layer                 │
 │  Builder  │   │  market_data (yfinance→stooq→fixture)             │
 └─────┬─────┘   │  options_data (yfinance→fixture, BS greeks est.)  │
       │         │  edgar_13f   (EDGAR XML/JSON fixtures)            │
       │         │  thesis      (local markdown/text → theme vector) │
       │         │  regime/VIX, earnings calendar                    │
       │         └───────────────────────┬──────────────────────────┘
       │                                 │
       ▼                                 ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                         Signal Engine (scoring.py)                 │
 │  thesis · 13F · market · options-env · catalyst   (0..10 each)     │
 │  weighted (configurable) − explicit RISK PENALTY                   │
 │  → final_score, confidence (data completeness), labels             │
 └───────────────────────────────┬──────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │              Candidate Generator (candidates.py)                   │
 │  hard filters: DTE window, min OI, min volume, max spread,         │
 │  strike zone, essential-data present → reject w/ reason            │
 │  per-contract re-score (liquidity, IV richness, delta, expiry)     │
 │  → ranked OptionCandidate list                                     │
 └───────────────────────────────┬──────────────────────────────────┘
                                 ▼
        ┌───────────────────────────────────────────────┐
        │  Report (md/csv/json/html)   Backtest store    │
        │  report.py                   backtest.py       │
        └───────────────────────────────────────────────┘
                                 ▲
                         CLI (cli.py): scan / backtest / update-13f / explain / doctor
```

## Scoring model

`call_opportunity_score = weighted_positive − risk_penalty`, clamped to 0..10.

- **Positive components** (each normalised to 0..10): `thesis`, `thirteen_f`,
  `market`, `options`, `catalyst`. Weights are configurable in
  `config/scoring_weights.yml`. Missing components are **dropped** and the
  remaining weights re-normalised, so partial data degrades gracefully.
- **Risk penalty** (subtracted, capped): illiquid options, wide spread, extreme
  IV, near-expiry lottery, weak data quality, no catalyst, contradictory 13F,
  excessive drawdown without stabilisation.
- **Confidence** is a *separate* axis = fraction of components with real data. A
  high score with low confidence is flagged and never sized.

### Thesis ≠ Trade
The thesis sub-score answers "good company for the thesis?". The final score
answers "reasonable CALL *right now*?". Penalties (rich IV, exits, no catalyst)
can pull a strong-thesis name down to *watchlist*. This is enforced and tested
(`test_coi_pipeline::test_thesis_separate_from_trade`).

## Graceful degradation
Every provider returns flagged-but-valid objects on failure (e.g. a
`MarketSnapshot` with `no_market_data`). The pipeline never raises on data gaps;
it records `data_quality_warnings` and lowers confidence instead.

## Greeks
Where a free source omits delta, it is estimated with a dependency-free
Black-Scholes (`blackscholes.py`, `math.erf` normal CDF) and marked
`delta_estimated=true` in output. IV percentile is approximated from ATM IV vs
realised vol (no IV-history store yet) and flagged as a proxy.

## What is intentionally NOT here
- No order execution / broker integration of any kind.
- No paid APIs (Tradier/Finnhub/Alpha Vantage are listed only as disabled).
- No hardcoded secrets or keys.

## Extension points
- Add tickers/categories → `config/ai_infra_universe.yml`.
- Add managers → `config/investors_13f.yml`.
- Tune weights/penalties → `config/scoring_weights.yml` (validate out-of-sample;
  do not overfit to past winners).
- Add a free data provider → implement in `market_data.py`/`options_data.py`
  behind the existing fallback list in `config/data_sources.yml`.
