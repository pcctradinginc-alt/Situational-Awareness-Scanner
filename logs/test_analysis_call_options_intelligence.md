# Test Analysis — CALL-Options Intelligence

This file is the Review/QA analysis of each Build → Test → Log → Fix loop for the
`call_options_intel` subsystem. It is updated every iteration.

## Command run

```bash
# full suite (existing scanner tests + new COI tests)
python3 -m pytest tests/ -q | tee logs/test_run_call_options_intelligence.log

# COI subsystem only
python3 -m pytest tests/test_coi_*.py -q
```

Environment: Python 3.14, pytest 9.0.3. No network, no API keys, no paid sources.

---

## Loop 1 — initial implementation

**Status:** 2 failures, both in NEW tests (test-expectation bugs, not code bugs).

| Failure | Root cause | Fix |
|---|---|---|
| `test_coi_options::test_contract_derived_properties` | Test compared `spread_pct` to an unrounded float; the model rounds to 4 dp by design. | Compared against `round(0.4/4.2, 4)`. |
| `test_coi_candidates::test_accepts_good_contract_and_ranks` | Test asserted `contract.delta is not None`, but delta estimation lives in `OptionsDataProvider`, not `CandidateGenerator`; hand-built contracts legitimately have `delta=None`. | Replaced with `final_score > 0` (the generator must degrade gracefully on missing delta). |

**Financial-logic defect found in review (code bug, fixed):**
Per-contract candidate scoring dropped the ticker-level risk penalties
(contradictory 13F, no catalyst, excessive drawdown) — only options penalties
were applied. Result: TSM ranked #1 despite smart money having *exited* it.
Fix: `TickerEvaluation.penalties` now carries non-options penalties, merged into
every contract's score in `CandidateGenerator._build_candidate`. After the fix
TSM correctly drops to *watchlist* (risk_penalty 1.2) and an accumulated name
with a catalyst (NVDA) ranks #1. This enforces the required **thesis ≠ trade**
separation.

**Overconfidence defect found in review (fixed):**
`backtest demo` evaluated seeded entries against unrelated live fixture spots,
producing absurd ~1300% returns. Fix: the demo is now self-contained with modest
synthetic eval prices (NVDA +12.7%, VST −7.4%) → 50% hit rate, one winner / one
loser. No profitability is claimed and the summary always carries a caveat.

**Warnings addressed:** `datetime.utcnow()` deprecation (Python 3.14) removed
from all new source + test files (switched to timezone-aware UTC). Remaining
deprecation warnings originate only in the pre-existing `scanner/` package and
are out of scope for this change.

---

## Loop 2 — after fixes

**Status:** ✅ COI suite **81 passed**. Full suite **103 passed, 2 failed**.

### Remaining failures (PRE-EXISTING, not introduced here)

| Failure | Analysis |
|---|---|
| `test_api_connections::TestStateManager::test_iv_rank_warmup` | Pre-existing `scanner/` behaviour: returns `INSUFFICIENT_DATA` where the test expects `WARMUP`. Present in the baseline commit before any CALL-options work. Out of scope (do not break / do not silently "fix" unrelated modules). |
| `test_api_connections::TestRegimeDetector::test_normal_mode` | Pre-existing `scanner/` regime-detection assertion, also failing on the baseline. Out of scope. |

Both were confirmed failing on the baseline commit (`git stash`/baseline run) **before** the subsystem was added, so the change introduces **no regressions**.

### Coverage of required components

| Area | Test module | Result |
|---|---|---|
| Config loading + defaults | `test_coi_config.py` | ✅ |
| Universe parsing | `test_coi_universe.py` | ✅ |
| Thesis tag extraction | `test_coi_thesis.py` | ✅ |
| 13F parser (XML + JSON fixtures) | `test_coi_edgar13f.py` | ✅ |
| Black-Scholes greeks (math correctness) | `test_coi_blackscholes.py` | ✅ |
| Market-data metrics + fallback | `test_coi_market_data.py` | ✅ |
| Options provider + greek estimation | `test_coi_options.py` | ✅ |
| Scoring engine + degradation | `test_coi_scoring.py` | ✅ |
| Options filtering / rejection | `test_coi_candidates.py` | ✅ |
| Report generation (md/csv/json/html) | `test_coi_report.py` | ✅ |
| Backtest / paper evaluation | `test_coi_backtest.py` | ✅ |
| Full pipeline (offline) + thesis≠trade | `test_coi_pipeline.py` | ✅ |
| CLI smoke (scan/doctor/explain/13f/backtest) | `test_coi_cli.py` | ✅ |
| Missing-data behaviour | `test_coi_missing_data.py` | ✅ |

### Remaining risks / known limitations

- **Live data paths (`--live`) are not exercised by tests** (no network in CI).
  They are guarded with try/except + fallbacks and marked `# pragma: no cover`.
  Manual verification recommended before relying on live mode.
- **Option-proxy returns are first-order** (delta/intrinsic), not realised option
  P&L; theta/vega/IV-path are not modelled. The backtest explicitly caveats this.
- **CUSIP→ticker mapping for live 13F** uses a small name-heuristic; live use
  should add a proper CUSIP map. Offline fixtures are unaffected.
- **IV percentile is a realised-vol proxy** (no IV history store yet); flagged as
  such in candidate reasons.
- Two pre-existing `scanner/` test failures remain (documented above); fixing
  them is out of scope for this feature branch.
