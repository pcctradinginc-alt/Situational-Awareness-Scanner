# Runbook — CALL-Options Scanner

Operational guide for the free-data `call_options_intel` subsystem.
**Research / paper mode only. It never trades.**

## 0. Prerequisites
```bash
pip install -r requirements.txt   # pandas, numpy, yfinance, PyYAML, ...
python -m call_options_intel doctor
```
`doctor` should print `Result: PASS` (configs load, fixtures present, no
order-execution code). It works with zero API keys.

## 1. Daily research scan (offline, deterministic)
```bash
python -m call_options_intel scan \
  --output reports/$(date +%F) \
  --format markdown csv json
```
Outputs `reports/<date>.md|.csv|.json`. The Markdown is the human report; CSV/JSON
are for tooling. Add `--format html` for a shareable page.

Read in this order: **Executive Summary → Data Quality Warnings → Top Candidates
→ Watchlist**. Treat every row as a hypothesis.

## 2. Live mode (free sources)
```bash
python -m call_options_intel scan --live --output reports/live_$(date +%F)
```
Live mode uses yfinance/Stooq for prices and yfinance for option chains, falling
back to fixtures, then to flagged-missing. Before first live 13F use, set a real
contact e-mail in `config/data_sources.yml → edgar_13f.user_agent` (SEC policy).

## 3. Explain a single name
```bash
python -m call_options_intel explain NVDA
```
Prints the full component breakdown, reasons for/against, data flags and the best
contract. Use this to understand *why* a name ranked where it did.

## 4. 13F smart-money check
```bash
python -m call_options_intel update-13f
```
Shows new/add/reduce/exit and conviction per ticker. Offline reads the bundled
two-quarter fixtures; `--live` fetches from EDGAR.

## 5. Paper evaluation / signal journal
```bash
# record today's ranked signals with a 30-day horizon
python -m call_options_intel scan --record --store reports/signals.jsonl --horizon 30

# later: evaluate matured signals against current prices
python -m call_options_intel backtest evaluate --store reports/signals.jsonl

# self-contained demo of the metrics (winner + loser)
python -m call_options_intel backtest demo --store /tmp/demo.jsonl
```
`evaluate` reports hit-rate, average return, drawdown proxy, score-bucket
performance and a buy-the-underlying benchmark. **No profitability is claimed** —
option-proxy returns are first-order delta/intrinsic approximations.

## 6. Interpreting scores
| Field | Meaning |
|---|---|
| `final_score` 0–10 | weighted positives − risk penalty. ≥6.5 = top gate, ≥4.5 = watchlist. |
| `confidence` (high/med/low) | **data completeness**, not conviction. Never size a `low` row. |
| `is_event_trade` / ⚡ | earnings within the flag window → IV-driven; expect IV crush. |
| `risk_penalty` | sum of penalties (illiquid, wide spread, rich IV, no catalyst, contradictory 13F, drawdown). |
| `reasons_against` | always shown — read these first. |

## 7. Tuning (carefully)
Edit `config/scoring_weights.yml` and `config/risk_thresholds.yml`. **Do not**
overfit weights to past winners. Validate any change with `backtest evaluate` on
out-of-sample recorded signals before trusting it.

## 8. Regenerating offline fixtures (dev only)
```bash
python scripts/generate_fixtures.py   # after editing config/ai_infra_universe.yml
```

## 9. Tests
```bash
python -m pytest tests/test_coi_*.py -q          # subsystem (must be green)
python -m pytest tests/ -q                        # full repo
```

## 10. Troubleshooting
| Symptom | Action |
|---|---|
| `doctor` WARN on fixtures | run `python scripts/generate_fixtures.py` |
| Many "no options chain" warnings in `--live` | provider throttling/holiday; rerun or use offline |
| 13F shows nothing | check `config/investors_13f.yml` managers + fixtures present |
| Scores look too confident | confirm `confidence_label`; low confidence rows are research-only |

## Safety invariants (must always hold)
- No order execution anywhere (`doctor` greps for it and fails loudly if found).
- No paid API required; no secrets in the repo.
- Missing data → flagged, never a crash.
