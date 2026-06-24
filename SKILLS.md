# System Skills — CALL-Options Intelligence

Documented capabilities ("skills") of the free-data `call_options_intel`
subsystem. Each skill is implemented, tested and runs offline by default. The
system is **research / paper mode only and never places orders**.

> See also the agent skill manifest at
> [`.claude/skills/call-options-intelligence/SKILL.md`](.claude/skills/call-options-intelligence/SKILL.md).

## 1. 13F Monitoring Skill
Parse SEC 13F filings (standard information-table XML **or** simplified JSON
fixtures) for a **configurable** list of managers (`config/investors_13f.yml`,
e.g. Founders Fund / Thiel Capital / a thesis-native LP). Detect **new / add /
reduce / exit** quarter-over-quarter and score conviction from position change,
portfolio concentration and persistence across filings.
- Module: `call_options_intel/edgar_13f.py`
- Free EDGAR only; live fetch is optional and off by default (a descriptive
  User-Agent is required by SEC fair-access policy — no API key).
- CLI: `python -m call_options_intel update-13f`

## 2. Thesis-Analysis Skill
Ingest local markdown/text (Aschenbrenner "The Decade Ahead", Thiel/Shulman
adjacent notes) and extract a **structured, uncertainty-tagged thesis vector**
over canonical themes: compute scarcity, semiconductor bottlenecks, AI data
centers, cloud capex, power/grid constraints, memory bandwidth, networking,
cooling, frontier-model race, national-security AI race, automation/robotics,
supply-chain bottlenecks, geopolitical chokepoints, monetization lag.
- Transparent lexicon model (not an LLM) → no philosophical certainty claimed.
- Module: `call_options_intel/thesis.py`

## 3. Market-Data & Options Skill
Free OHLCV (yfinance → Stooq → fixture fallback) reduced to momentum, trend
(200-DMA), RSI, drawdown, realised vol and dollar-volume. Free option chains
(yfinance → fixture) with strike, expiry, bid/ask/last, IV, OI, volume, DTE and
**moneyness**. Missing **delta** is estimated via Black-Scholes and flagged as
estimated. Candidate selection prefers **DTE 60–180** and balanced delta
(~0.30–0.55), with **hard** liquidity filters (min OI, min volume, max spread).
- Modules: `market_data.py`, `options_data.py`, `blackscholes.py`, `candidates.py`

## 4. Volatility-Regime Skill
VIX-based regime label (calm / normal / elevated / stress) plus a per-name IV
richness proxy (ATM IV vs realised vol) to flag expensive calls. Calls held into
earnings are tagged as **event trades** (IV-crush risk), distinct from trend
trades.
- Modules: `pipeline.py` (regime), `scoring.py` / `candidates.py` (IV richness)

## 5. Open-Source Expansion Skill
The data layer is provider-pluggable (`config/data_sources.yml`). Additional
**free** sources (e.g. FRED macro, RSS) can be added behind the same graceful
fallback contract. **No paid APIs** are required; optional paid providers are
listed only to mark them as intentionally disabled by default.

## Skill outputs
- Ranked CALL candidates → Markdown / CSV / JSON / HTML (`report.py`).
- Timestamped signal store + paper evaluation vs a buy-the-underlying benchmark
  (`backtest.py`). No profitability is claimed without evidence.

## Guardrails baked into every skill
Uncertainty + risk + liquidity warnings on every candidate; data-quality
confidence flag; missing data degrades gracefully; `doctor` self-check verifies
no order-execution code exists.
