# Call Options Intelligence Skill

## Purpose
Transform the Situational Awareness Scanner into an automated research system for
identifying potentially profitable CALL-option opportunities connected to AI
infrastructure. **Research / paper mode only — it never executes trades.**

## Core Thesis
The system tracks whether the market is underpricing companies that benefit from
accelerating AI infrastructure demand, compute buildout, semiconductor
bottlenecks, data centers, power constraints, networking, memory, cooling, cloud
capex, frontier-model scaling and related supply chains.

A deliberate separation is enforced: **thesis ≠ trade**. A company can fit the
Aschenbrenner thesis perfectly and still be a poor CALL right now (rich IV, wide
spread, no catalyst, contradictory 13F, bad timing). The thesis score and the
final CALL-opportunity score are distinct axes.

## Main Inputs
- SEC 13F filings (free EDGAR; offline fixtures by default)
- Aschenbrenner / Situational Awareness thesis material (local markdown/text)
- Public commentary by Thiel, Shulman and other strategic AI thinkers (local text)
- Free equity market data (yfinance / Stooq, with offline fixture fallback)
- Free options-chain data where available (yfinance), with fixture fallback
- VIX / volatility regime data (free; fixture fallback)
- Earnings calendar (free; fixture fallback)
- Optional news/RSS (disabled by default)

## Output
Ranked CALL-option candidates with: ticker, company name, AI-infra relevance,
catalyst, thesis score, 13F accumulation score, market momentum score,
volatility/liquidity score, options-chain score, expected reward/risk framing,
suggested expiry window, suggested strike zone, confidence (data completeness),
reasons for, reasons against, data-quality warning, and a paper-trading
recommendation. Reports render to Markdown, CSV, JSON and HTML.

## Constraints
- No live trading. No paid APIs required. No secret leakage.
- Always explain uncertainty; never overstate confidence.
- Prefer robust, testable code over clever fragile code.
- Missing data must not crash the pipeline — flag and degrade.

## How it is implemented
Python package `call_options_intel/` (free-data, self-contained, additive — it
does not modify or depend on the existing paid-API `scanner/` pipeline):

| Module | Role |
|---|---|
| `config_loader.py` | Load YAML configs with safe defaults |
| `universe.py` | Build the configurable AI-infra equity universe |
| `thesis.py` | Extract a structured thesis vector from text |
| `edgar_13f.py` | Parse 13F (XML/JSON), detect new/add/reduce/exit, score conviction |
| `market_data.py` | Free OHLCV → momentum/trend/RSI/drawdown/vol (fallbacks) |
| `options_data.py` | Free option chains; estimate missing greeks via Black-Scholes |
| `blackscholes.py` | Dependency-free BS delta/price/IV |
| `scoring.py` | Weighted positive scores − explicit risk penalty |
| `candidates.py` | Hard liquidity/DTE filters, strike zones, per-contract ranking |
| `report.py` | Markdown / CSV / JSON / HTML |
| `backtest.py` | Timestamped signal store + paper evaluation |
| `pipeline.py` | Orchestration with graceful degradation |
| `cli.py` | `scan`, `backtest`, `update-13f`, `explain`, `doctor` |

Configs live in `config/*.yml`; offline fixtures in `call_options_intel/fixtures/`.

## Engineering Loop
1. Inspect existing architecture.
2. Identify gaps.
3. Implement the smallest coherent improvement.
4. Run tests (`pytest tests/test_coi_*.py`).
5. Analyze logs (`logs/test_analysis_call_options_intelligence.md`).
6. Fix defects.
7. Repeat until acceptance criteria are met.

Two roles operate the loop: a **Code Build Agent** (implements modules, tests,
configs, docs, CLI) and a **Review/QA Agent** (reviews logic & financial
correctness, runs tests, analyzes logs, rejects overconfident or fragile output,
verifies no paid-source dependency or live-trading code was introduced).

## Quick start
```bash
python -m call_options_intel doctor
python -m call_options_intel scan --output reports/latest --format markdown csv json
python -m call_options_intel explain NVDA
python -m call_options_intel backtest demo --store reports/signals.jsonl
```
