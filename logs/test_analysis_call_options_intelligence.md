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

---

## Loop 3 — independent Review/QA Agent audit

An independent Review/QA pass audited financial logic, overconfidence and the
safety perimeter. Verdict was **REQUEST-CHANGES** with concrete findings; all
valid findings were fixed and regression-tested.

| ID | Severity | Finding | Fix |
|---|---|---|---|
| B1 | BLOCKER | The #1 top pick (NVDA) shipped with an **empty reasons-against** and the report omitted the "Against" line when empty. | `candidates._build_candidate` now guarantees a non-empty `reasons_against` (standing caveat fallback); `report._candidate_block` always renders an "Against" line. Test: `test_every_candidate_has_reasons_against` (candidates + pipeline). |
| M1 | MAJOR | Event/earnings trades carried **zero penalty** — `setdefault("near_expiry_lottery", 0.0)` was a no-op. | Added a real, configurable `event_iv_risk` penalty (0.7) with its own key, surfaced as a reason-against. NVDA into earnings now scores 7.38 (was 8.08) with `risk_penalty=0.7`. Test: `test_event_trade_is_penalised`. |
| M2 | MAJOR | IV-richness gate too loose (`50×richness` only flagged at 1.6× realised). | New shared `iv_richness_assessment` with configurable slope (default 100): 1.30× → expensive, 1.42× → extreme, plus a "mild/somewhat rich" band. NVDA's 1.25× IV now flags as "somewhat rich". Test: `test_rich_iv_is_penalised`. |
| M3 | MAJOR | 13F concentration bonus added **unconditionally**, able to flip a `reduce` positive. | Concentration bonus now applies only to `new`/`add`/`hold`. Test: `test_concentration_does_not_flip_reduce_positive`. |
| m1 | MINOR | `data_quality.min_acceptable` configured but unused; also `catalyst` was always "present" (fake 2.0), so confidence could never fall below 0.4 and the floor could never fire. | `score_catalyst` now returns **absent (None)** when there is no catalyst (penalty still applies), making confidence honest; `candidates.generate` enforces `min_acceptable` (rejects `insufficient_data_quality`). Test: `test_insufficient_data_quality_rejected`. |
| n2/n3 | NIT | Dead code in `cli.py` (`ok = False if False else ok`, `return 0 if ok else 0`). | Removed. |

**Acknowledged-but-kept (documented, low risk):** option-proxy `max(linear,
intrinsic)` can over-credit unexpired deep-ITM moves (capped at −100% and
caveated); strike-zone boundaries overlap (first-match-wins, deterministic).

**Status after Loop 3:** ✅ COI suite **87 passed**. Full suite **109 passed, 2
pre-existing failures** (unchanged `scanner/` tests). `doctor` PASS. The #1 pick
now carries IV-richness flags, an earnings IV-crush warning and a real risk
penalty — the exact overconfidence failure modes the audit targeted are closed.
Re-review verdict basis: **APPROVE** (B1 + M1 + M2 + M3 addressed with tests).

---

## Loop 4 — green full suite, SEC contact, no false data, merge

Goal: real SEC contact email, a fully green test run (no bugs), and ensure
offline output cannot be mistaken for real data.

| Item | Change |
|---|---|
| SEC contact | Set `edgar_13f.user_agent` contact to **info@pcctradinginc.com** in `config/data_sources.yml` and the `config_loader` default (SEC fair-access requires a real contact for live EDGAR). |
| `test_iv_rank_warmup` (was failing) | **Stale test**, not a code bug: `get_iv_rank` deliberately returns `INSUFFICIENT_DATA`/`iv_rank=None` (documented "R-03 FIX") instead of the old `WARMUP`/50.0 default. Test updated to assert the intended behaviour. |
| `test_normal_mode` (was failing) | **Real scanner bug**: `regime_stability = (energy_breadth + grid_growth)/2` averaged a 0–1 breadth ratio with *raw* YoY growth, so a healthy 6% energy growth (0.06) pushed stability to 0.38 < 0.4 and falsely flagged STRESS. Fixed `grid_growth` normalisation (`/0.10`; 10% YoY ⇒ full). `test_stress_mode_iv` still STRESS via the IV>60 trigger. |
| "False data" guard | Offline reports (Markdown + HTML) now carry a prominent **"OFFLINE / DEMO DATA — synthetic fixtures, NOT real market data"** banner so demo figures cannot be mistaken for real quotes. Locked by `test_offline_reports_flag_synthetic_data`. |

**Status after Loop 4:** ✅ **Full suite 112 passed, 0 failed** (was 109/2). Both
long-standing `scanner/` failures resolved (one stale test, one genuine regime
scale bug). `doctor` PASS. Remaining 24 warnings are `datetime.utcnow()`
deprecations inside the legacy `scanner/` package only (not failures, out of
scope). Feature branch merged to `main`.
