"""
cli.py
======
Command-line interface for the CALL-options intelligence subsystem.

Commands:
  scan        Run a full scan and write Markdown/CSV/JSON (and optional HTML).
  backtest    Record signals or evaluate matured signals (paper evaluation).
  update-13f  Recompute 13F smart-money changes from fixtures (or live).
  explain     Print the full signal breakdown for a single ticker.
  doctor      Health-check configs, fixtures and the environment.

RESEARCH / PAPER MODE ONLY. This CLI never places orders.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import DISCLAIMER, __version__
from .config_loader import AppConfig, CONFIG_DIR
from .pipeline import Pipeline, DEFAULT_FIXTURES
from .report import ReportGenerator
from .backtest import SignalStore


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S", stream=sys.stderr,
    )


def _build_pipeline(args) -> Pipeline:
    config_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else CONFIG_DIR
    cfg = AppConfig(config_dir=config_dir)
    mode = "live" if getattr(args, "live", False) else cfg.mode
    fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES
    thesis_dir = getattr(args, "thesis_dir", None)
    thesis_paths = [thesis_dir] if thesis_dir else None
    return Pipeline(config=cfg, mode=mode, fixtures_dir=fixtures,
                    thesis_paths=thesis_paths,
                    iv_history=getattr(args, "iv_history", None))


# ── commands ───────────────────────────────────────────────────────────────
def cmd_scan(args) -> int:
    pipe = _build_pipeline(args)
    tickers = _split(args.tickers)
    result = pipe.scan(only_tickers=tickers)

    formats = _split(args.format) or pipe.cfg.report.get("report", {}).get(
        "default_formats", ["markdown", "csv", "json"])
    gen = ReportGenerator(pipe.cfg.report)

    stem_arg = args.output or "reports/latest"
    stem_path = Path(stem_arg)
    if stem_path.suffix:  # strip extension if provided
        stem_path = stem_path.with_suffix("")
    out_dir = stem_path.parent if str(stem_path.parent) != "" else Path(".")
    written = gen.write(result, formats, out_dir, stem=stem_path.name)

    if args.record:
        store = SignalStore(args.store)
        store.record(result, horizon_days=args.horizon, only_top=args.top_only)

    if getattr(args, "person_intel", False):
        from .person_intel.runner import run_person_intel
        rows = run_person_intel(config=pipe.cfg, fixtures_dir=pipe.fixtures_dir,
                                today=pipe.today, only_tickers=tickers)
        written["person_intel_md"] = out_dir / f"{stem_path.name}_person_intel.md"
        written["person_intel_md"].write_text(
            gen.person_intel_markdown(rows), encoding="utf-8")
        written["person_intel_json"] = out_dir / f"{stem_path.name}_person_intel.json"
        written["person_intel_json"].write_text(
            gen.person_intel_json(rows), encoding="utf-8")

    print(f"\n{DISCLAIMER}\n")
    print(f"Scan ({result.mode}): {len(result.candidates)} candidates, "
          f"{len(result.top)} top, {len(result.watchlist)} watchlist, "
          f"{len(result.rejected_contracts)} rejected.")
    for fmt, path in written.items():
        print(f"  {fmt:9s} -> {path}")
    if result.top:
        print("\nTop:")
        for i, c in enumerate(result.top[:5], 1):
            ev = " ⚡EVENT" if c.is_event_trade else ""
            print(f"  {i}. {c.ticker} {c.contract.strike:g}C {c.contract.expiry} "
                  f"score={c.final_score:.2f} ({c.confidence_label}){ev}")
    if result.data_quality_warnings:
        print(f"\n⚠️  {len(result.data_quality_warnings)} data-quality warning(s).")
    return 0


def cmd_backtest(args) -> int:
    store = SignalStore(args.store)
    if args.action == "record":
        pipe = _build_pipeline(args)
        result = pipe.scan(only_tickers=_split(args.tickers))
        n = store.record(result, horizon_days=args.horizon, only_top=args.top_only)
        print(f"Recorded {n} signals -> {store.path}")
        return 0

    if args.action == "demo":
        price_map = _seed_demo_store(store)
        print(f"Seeded demo signal store -> {store.path}")
        # self-contained, realistic evaluation prices (NOT the live fixture spots,
        # which are unrelated to the demo entries and would inflate returns).
        evaluated = store.evaluate(lambda t: price_map.get(t))
        import json
        print(json.dumps(store.summarize(evaluated), indent=2))
        print(f"\n{DISCLAIMER}")
        return 0

    # evaluate (real): price the matured signals at current market data
    pipe = _build_pipeline(args)

    def price_lookup(ticker: str):
        return pipe.market.get_snapshot(ticker).spot

    evaluated = store.evaluate(price_lookup)
    summary = store.summarize(evaluated)
    import json
    print(json.dumps(summary, indent=2))
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_update_13f(args) -> int:
    pipe = _build_pipeline(args)
    changes = pipe._build_13f()
    if not changes:
        print("No 13F changes (no fixtures / no managers configured).")
        return 0
    print(f"{'TICKER':8s} {'ACTION':8s} {'CONVICTION':>10s}  MANAGERS")
    for t, ch in sorted(changes.items(), key=lambda kv: kv[1].conviction_score,
                        reverse=True):
        print(f"{t:8s} {ch.action:8s} {ch.conviction_score:>+10.2f}  "
              f"{', '.join(ch.managers)}")
    if pipe.mode != "live":
        print("\n(offline fixtures; pass --live to fetch from SEC EDGAR — free, "
              "requires a descriptive User-Agent in config/data_sources.yml)")
    return 0


def cmd_explain(args) -> int:
    pipe = _build_pipeline(args)
    out = pipe.explain(args.ticker)
    if out is None:
        print(f"{args.ticker} is not in the universe.")
        return 1
    ev = out["evaluation"]
    bd = ev.breakdown
    print(f"\n=== {ev.ticker} — {ev.name} ({ev.category}) ===")
    print(f"Relevance prior: {ev.relevance:.2f} | label: {ev.label} | "
          f"confidence: {bd.confidence_label} ({bd.confidence:.2f})")
    print(f"Final score: {bd.final_score:.2f}  (weighted+ {bd.weighted_positive:.2f} "
          f"- risk {bd.risk_penalty:.2f})")
    print("Components:")
    for k, v in bd.as_dict().items():
        print(f"  {k:18s}: {v}")
    print(f"Contracts available: {out['n_contracts']} | "
          f"candidates passing filters: {len(out['candidates'])}")
    if ev.reasons_for:
        print("FOR : " + "; ".join(dict.fromkeys(ev.reasons_for)))
    if ev.reasons_against:
        print("AGAINST: " + "; ".join(dict.fromkeys(ev.reasons_against)))
    if ev.data_flags:
        print("DATA FLAGS: " + ", ".join(dict.fromkeys(ev.data_flags)))
    if out["candidates"]:
        c = out["candidates"][0]
        print(f"\nBest contract: {c.contract.strike:g}C {c.contract.expiry} "
              f"({c.contract.dte} DTE) score={c.final_score:.2f}")
        print(f"  {c.paper_recommendation}")
    print(f"\n{DISCLAIMER}")
    return 0


def cmd_person_intel(args) -> int:
    """Run the offline person-intelligence layer (entity→evidence→proxy→score)."""
    from .person_intel.runner import run_person_intel
    cfg = AppConfig(config_dir=Path(args.config_dir) if args.config_dir else CONFIG_DIR)
    fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES
    rows = run_person_intel(config=cfg, fixtures_dir=fixtures,
                            only_tickers=_split(args.tickers))
    if not rows:
        print("No person-linked candidates (no tracked holdings / proxies match).")
        return 0
    print(f"\n{DISCLAIMER}\n")
    print("PERSON-INTELLIGENCE — research signal vs trade candidate (thesis ≠ trade)\n")
    print(f"{'TICKER':7s} {'RESEARCH':>8s} {'label':<8s} {'TRADE':>6s} {'label':<10s} "
          f"{'CLUSTER':<14s} HELD-BY / FALSIFICATION")
    for r in rows[: args.limit]:
        s = r.score
        held = ", ".join(r.held_by) or "—"
        print(f"{s.ticker:7s} {s.final_research_score:8.2f} {s.research_label:<8s} "
              f"{s.final_trade_candidate_score:6.2f} {s.trade_label:<10s} "
              f"{r.dominant_cluster:<14s} {held}")
        print(f"        ↳ falsify: {s.falsification}")
    print("\nResearch = people did/said X (verified) mapping to a public proxy.")
    print("Trade    = research GATED by market timing + options quality, CAPPED by "
          "verification. A strong thesis is not a trade.")
    return 0


def cmd_early_filings(args) -> int:
    """Recent FAST EDGAR filings (Form 4 / 13D-G / Form D) — leading person-signals."""
    from .person_intel.edgar_fast import FastFilingMonitor, load_fast_client
    cfg = AppConfig(config_dir=Path(args.config_dir) if args.config_dir else CONFIG_DIR)
    mode = "live" if getattr(args, "live", False) else cfg.mode
    fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES
    client = load_fast_client(cfg.data_sources, mode, fixtures)
    managers = cfg.investors.get("managers", []) or []
    feed = FastFilingMonitor(client).feed(managers, since_days=args.since)
    print(f"\n{DISCLAIMER}\n")
    if not feed:
        where = "live SEC EDGAR" if mode == "live" else "offline submissions fixtures"
        print(f"No fast filings in the last {args.since}d ({where}).")
        if mode != "live":
            print("(offline; pass --live to query SEC EDGAR — free, descriptive "
                  "User-Agent from config/data_sources.yml, no key)")
        return 0
    mapper = None
    if getattr(args, "resolve", False):
        from .person_intel.cusip_map import load_mapper
        mapper = load_mapper(cfg.config_dir)
    print(f"FAST filings (last {args.since}d) — leading person-signals vs 13F:\n")
    hdr = f"{'DATE':12s} {'FORM':10s} {'ROLE':<12s} {'AGE':>4s}  ENTITY"
    print(hdr + ("  → SUBJECT" if mapper else ""))
    for r in feed[: args.limit]:
        age = r.age_days()
        line = (f"{r.filing_date:12s} {r.form:10s} {r.role.value:<12s} "
                f"{(age if age is not None else '?'):>4}  {r.entity}")
        if mapper:
            res = client.resolve_subject(r, mapper)
            if res.mapped_ticker:
                tag = "" if not res.needs_human_review else " (review)"
                line += f"  → {res.mapped_ticker} ({res.mapping_confidence:.2f}){tag}"
            elif res.subject_issuer:
                line += f"  → {res.subject_issuer} (no ticker)"
            else:
                line += "  → (unresolved)"
        print(line)
    return 0


def cmd_person_monitor(args) -> int:
    """Live, dedup'd person-signal monitor (the twice-daily radar).

    Collects recent EDGAR filings for the tracked Thiel / Aschenbrenner vehicles,
    de-duplicates against a persistent store and reports ONLY the new signals.
    With --email it sends a digest *only when there is a new signal*.
    """
    from .person_intel.monitor_report import monitor_and_notify, render_markdown
    from .person_intel.monitor import run_monitor
    cfg = AppConfig(config_dir=Path(args.config_dir) if args.config_dir else CONFIG_DIR)
    mode = "live" if getattr(args, "live", False) else cfg.mode
    fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES

    if args.dry_run:
        # report without persisting the dedup store or writing artifacts
        result = run_monitor(config=cfg, mode=mode, since_days=args.since,
                             state_path=args.state, fixtures_dir=fixtures,
                             resolve=not args.no_resolve,
                             min_path_weight=args.min_weight, persist=False)
        print(f"\n{DISCLAIMER}\n")
        print(render_markdown(result))
        print(f"\nTrade-candidates (all 3 axes pass): {len(result.trade_candidates)}")
        return 0

    summary = monitor_and_notify(
        config=cfg, mode=mode, since_days=args.since, state_path=args.state,
        fixtures_dir=fixtures, resolve=not args.no_resolve,
        send_email=args.email, min_path_weight=args.min_weight)
    print(f"\n{DISCLAIMER}\n")
    print(f"PERSON-MONITOR — mode={summary['mode']} · "
          f"new={summary['total_new']} "
          f"({summary['new_count']} filing / {summary['statement_count']} statement / "
          f"{summary.get('vorfeld_count', 0)} vorfeld) · "
          f"principal-linked={summary['principal_count']} · "
          f"emailed={summary['emailed']}")
    print(f"  digest: {summary['artifacts']['digest_md']}")
    if summary["total_new"] == 0:
        print("  (no new signals — no email; nothing to report)")
    return 0


def cmd_proxies(args) -> int:
    """Ranked private→public proxy map: best LISTED proxy per thesis cluster,
    scored on link · liquidity · options-quality · valuation (+ falsification)."""
    from .person_intel.proxy_map import load_proxy_map, THESIS_CLUSTERS
    cfg = AppConfig(config_dir=Path(args.config_dir) if args.config_dir else CONFIG_DIR)
    pm = load_proxy_map(cfg.config_dir)
    opt_fn = None
    if getattr(args, "live", False):
        from .person_intel.monitor import _make_tradeability_fn
        fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES
        tf = _make_tradeability_fn(cfg, "live", fixtures)
        opt_fn = (lambda t: (lambda r: (r[1] / 10.0, r[1] / 10.0) if r else None)(tf(t))) if tf else None
    print(f"\n{DISCLAIMER}\n")
    print("PRIVATE THEME → ranked PUBLIC proxy (edge = link·liquidity·options·valuation)\n")
    clusters = _split(args.cluster) or list(THESIS_CLUSTERS)
    for cl in clusters:
        ranked = pm.best_proxies(cl, top=args.limit, options_fn=opt_fn)
        if not ranked:
            continue
        print(f"«{cl}»")
        print(f"  {'TICK':5s} {'EDGE':>4s}  {'link':>4s} {'liq':>4s} {'opt':>4s} {'val':>4s}  rationale")
        for r in ranked:
            print(f"  {r.ticker:5s} {r.edge_score:4.1f}  {r.link:4.2f} "
                  f"{r.liquidity:4.2f} {r.options_quality:4.2f} {r.valuation:4.2f}  "
                  f"{r.rationale}")
        print(f"  ↳ falsify: {pm.falsification_for(cl)}\n")
    return 0


def cmd_record_iv(args) -> int:
    """Append today's ATM IV per ticker to the IV-history store (warmup builder)."""
    from .scoring import _atm_iv
    from .person_intel.iv_history import IVHistoryStore
    pipe = _build_pipeline(args)
    universe = pipe.universe_builder.build()
    tickers = _split(args.tickers)
    if tickers:
        from .universe import UniverseBuilder
        universe = UniverseBuilder.filter_tickers(universe, tickers)
    store = IVHistoryStore(args.store)
    iv_by_ticker: dict[str, float] = {}
    for entry in universe:
        snap = pipe.market.get_snapshot(entry.ticker)
        contracts = pipe.options.get_call_contracts(
            entry.ticker, snap.spot, snap.hist_vol_annual)
        atm = _atm_iv(contracts)
        if atm:
            iv_by_ticker[entry.ticker] = atm
    n = store.record_many(iv_by_ticker)
    print(f"Recorded {n} ATM-IV observations -> {store.path}")
    warm = [t for t in iv_by_ticker if store.rank(t, iv_by_ticker[t],
            warmup_min_obs=args.warmup).warmed_up]
    print(f"Warmed-up tickers (>= {args.warmup} obs): {len(warm)}"
          + (f" — {', '.join(sorted(warm)[:10])}" if warm else
             " (keep recording daily to warm up IV rank)"))
    return 0


def cmd_outcomes_report(args) -> int:
    """Multi-horizon outcome summary + walk-forward guard (no edge claim without
    a sufficient out-of-sample fold)."""
    import json as _json
    from .person_intel.outcomes import (
        OutcomeStore, summarize, walk_forward_guard,
    )
    store = OutcomeStore(args.store)
    price_fn = bench_fn = price_at = None
    approx = False
    if args.demo:
        price_map = _seed_outcomes_demo(store)
        price_fn = lambda t, h: price_map.get(t)        # noqa: E731
        bench_fn = lambda s, h: 0.03                     # noqa: E731
    elif args.live or args.historical:
        # date-aware REAL per-horizon pricing (Stooq live or offline CSV fixtures)
        from .person_intel.historical import HistoricalPriceProvider
        fixtures = getattr(args, "fixtures_dir", None) or DEFAULT_FIXTURES
        prov = HistoricalPriceProvider(
            mode=("live" if args.live else "offline"), fixtures_dir=fixtures)
        price_at = prov.make_price_at()
    else:
        pipe = _build_pipeline(args)
        # No per-horizon history available -> approximate with current spot for
        # every horizon (flagged). Pass --historical/--live for real pricing.
        price_fn = lambda t, h: pipe.market.get_snapshot(t).spot  # noqa: E731
        approx = True
    evaluated = store.evaluate(price_fn=price_fn, benchmark_fn=bench_fn,
                               price_at=price_at)
    report = {
        "summary": summarize(evaluated),
        "walk_forward": walk_forward_guard(
            evaluated, split_date=args.split, horizon=args.horizon,
            min_sample=args.min_sample),
    }
    print(_json.dumps(report, indent=2))
    print(f"\n{DISCLAIMER}")
    if approx and report["summary"].get("n"):
        print("\n⚠️  per-horizon prices approximated by current spot — "
              "pass --historical or --live for real per-horizon pricing.")
    return 0


def _seed_outcomes_demo(store) -> dict:
    """Seed a small in-/out-of-sample store (incl. a rejected name) for the demo."""
    from datetime import datetime, timedelta, timezone

    def iso(days_ago):
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    rows = []
    for i in range(4):  # in-sample (before 2026-03-01 split), winners
        rows.append({"recorded_at": iso(190), "ticker": f"IS{i}", "strike": 100,
                     "entry_spot": 100, "entry_premium": 6.0, "entry_delta": 0.45,
                     "final_score": 7.5, "source": "13D", "thesis_cluster": "compute",
                     "regime": "normal", "label": "candidate"})
    for i in range(4):  # out-of-sample (after split), winners
        rows.append({"recorded_at": iso(100), "ticker": f"OOS{i}", "strike": 100,
                     "entry_spot": 100, "entry_premium": 6.0, "entry_delta": 0.45,
                     "final_score": 7.5, "source": "Form 4", "thesis_cluster": "power_grid",
                     "regime": "normal", "label": "candidate"})
    rows.append({"recorded_at": iso(100), "ticker": "BADCO", "strike": 100,
                 "entry_spot": 100, "entry_premium": 6.0, "entry_delta": 0.45,
                 "final_score": 2.0, "source": "13F", "thesis_cluster": "compute",
                 "regime": "normal", "label": "rejected"})
    store.record_many(rows)
    # winners up ~25%, the rejected name fell -> system correctly avoided it
    pm = {f"IS{i}": 125.0 for i in range(4)}
    pm.update({f"OOS{i}": 125.0 for i in range(4)})
    pm["BADCO"] = 70.0
    return pm


def cmd_doctor(args) -> int:
    ok = True
    print("CALL-Options Intelligence — health check\n")

    cfg = AppConfig(config_dir=Path(args.config_dir) if args.config_dir else CONFIG_DIR)
    warns = cfg.validate()
    _line("configs load", True, f"mode={cfg.mode}")
    for w in warns:
        _line("config warning", False, w)  # informational; does not fail doctor
    _line("universe tickers", bool(cfg.universe.get("tickers")),
          f"{len(cfg.universe.get('tickers', []))} tickers")

    # person-intelligence configs (Goals A/D/F)
    try:
        from .person_intel.entities import load_graph
        from .person_intel.cusip_map import load_mapper
        from .person_intel.proxy_map import load_proxy_map, THESIS_CLUSTERS
        g = load_graph(cfg.config_dir)
        _line("entity graph", bool(g.entities),
              f"{len(g.entities)} entities, {len(g.facts())} facts, "
              f"{len(g.hypotheses())} hypotheses")
        m = load_mapper(cfg.config_dir)
        _line("cusip map", bool(m.explicit), f"{len(m.explicit)} curated CUSIPs")
        pm = load_proxy_map(cfg.config_dir)
        cover = all(c in pm.clusters for c in THESIS_CLUSTERS)
        _line("proxy map clusters", cover,
              f"{len(pm.clusters)} clusters")
        _line("proxy falsification complete", not pm.warnings,
              "; ".join(pm.warnings) if pm.warnings else "every cluster has one")
        ok = ok and cover and not pm.warnings

        # live person-signal monitor: tracked principals must have CIKs to watch
        from .person_intel.monitor import PRINCIPALS, SeenStore  # noqa: F401
        tracked_ciks = [m for m in (cfg.investors.get("managers", []) or [])
                        if m.get("cik")]
        principals_present = all(p in g.entities for p in PRINCIPALS)
        _line("person-monitor principals", principals_present,
              ", ".join(PRINCIPALS))
        _line("person-monitor tracked CIKs", bool(tracked_ciks),
              f"{len(tracked_ciks)} managers with a CIK to watch")
        ok = ok and principals_present and bool(tracked_ciks)

        # conviction / statement feeds (the "what they SAY" signal class)
        from .config_loader import load_yaml as _ly
        stmt_cfg = _ly("statement_sources", cfg.config_dir)
        feeds = stmt_cfg.get("feeds", []) or []
        _line("statement feeds configured", bool(feeds),
              f"{len(feeds)} feeds (essays + news)")

        # vorfeld: EDGAR full-text-search discovery terms
        es = (_ly("early_sources", cfg.config_dir) or {}).get("edgar_fts", {})
        _line("EDGAR FTS discovery", bool(es.get("enabled")),
              f"{len(es.get('terms', []))} name terms (new-entity discovery)"
              if es.get("enabled") else "disabled")

        # vorfeld non-filing change-detection (ADV/IAPD · jobs · domain · CT)
        early = _ly("early_sources", cfg.config_dir) or {}
        vf = sum(len((early.get(k, {}) or {}).get(sub, []))
                 for k, sub in (("adv_iapd", "advisers"),
                                ("job_postings", "companies"),
                                ("domain_watch", "sites"),
                                ("cert_transparency", "patterns"))
                 if (early.get(k, {}) or {}).get("enabled"))
        _line("vorfeld change-detection", vf > 0,
              f"{vf} targets (ADV/IAPD · jobs · domain · cert-transparency)")

        # three-axis trade gate (person · freshness · tradeability)
        from .person_intel.triple_score import GateThresholds
        gt = GateThresholds.from_config(cfg.scoring)
        _line("three-axis trade gate", True,
              f"person>={gt.person_signal} · fresh>={gt.freshness} · "
              f"trade>={gt.tradeability} (hard AND)")
    except Exception as exc:  # pragma: no cover - defensive
        _line("person-intel configs", False, str(exc))
        ok = False

    fx = DEFAULT_FIXTURES
    for sub in ("market/market_fixture.json", "options/options_fixture.json"):
        present = (fx / sub).exists()
        _line(f"fixture {sub}", present)
        ok = ok and present

    try:
        import yfinance  # noqa: F401
        _line("yfinance import (live mode)", True)
    except Exception as exc:
        _line("yfinance import (live mode)", False, f"{exc} — offline still works")

    # secret / live-trading guards
    import call_options_intel
    src_root = Path(call_options_intel.__file__).resolve().parent
    bad = _grep_dangerous(src_root)
    _line("no live-trading / order code", not bad,
          "; ".join(bad) if bad else "clean")
    ok = ok and not bad

    print(f"\nResult: {'PASS' if ok else 'WARN — see above'}")
    print(DISCLAIMER)
    return 0  # doctor reports but never hard-fails CI


# ── helpers ────────────────────────────────────────────────────────────────
def _line(label: str, good: bool, detail: str = ""):
    mark = "✅" if good else "⚠️ "
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def _split(value):
    if not value:
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(str(v).replace(",", " ").split())
        return [x.strip().upper() if len(x) <= 5 else x.strip() for x in out if x.strip()]
    return [x.strip() for x in str(value).replace(",", " ").split() if x.strip()]


def _grep_dangerous(root: Path) -> list[str]:
    """Defensive self-check: ensure no order-execution calls slipped in.

    Skips this guard file itself (it necessarily names the patterns it hunts).
    """
    needles = ("place_order", "submit_order", "create_order", "place_option_order",
               "place_stock_order", "broker.execute", "live_trade(")
    self_name = Path(__file__).name
    hits = []
    # rglob so the whole package (incl. person_intel/) is scanned, not just the top
    for f in root.rglob("*.py"):
        if f.name == self_name or "__pycache__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore").lower()
        for n in needles:
            if n in text:
                hits.append(f"{f.name}:{n}")
    return hits


def _seed_demo_store(store: SignalStore) -> dict[str, float]:
    """Write a tiny synthetic, already-matured signal store for demo/eval.

    Returns a realistic evaluation-price map (modest moves) so the demo shows
    one winner and one loser — never an inflated, overconfident result.
    """
    import json
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    demo = [
        {"recorded_at": old, "ticker": "NVDA", "expiry": "2026-12-18", "strike": 120,
         "dte": 90, "entry_spot": 110.0, "entry_premium": 8.0, "entry_delta": 0.45,
         "iv": 0.45, "final_score": 7.4, "confidence_label": "high",
         "is_event_trade": False, "horizon_days": 30, "label": "top"},
        {"recorded_at": old, "ticker": "VST", "expiry": "2026-12-18", "strike": 100,
         "dte": 90, "entry_spot": 95.0, "entry_premium": 6.0, "entry_delta": 0.40,
         "iv": 0.40, "final_score": 6.1, "confidence_label": "medium",
         "is_event_trade": False, "horizon_days": 30, "label": "candidate"},
    ]
    with store.path.open("w", encoding="utf-8") as fh:
        for d in demo:
            fh.write(json.dumps(d) + "\n")
    # NVDA +12.7% (winner), VST -7.4% (loser)
    return {"NVDA": 124.0, "VST": 88.0}


# ── parser ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coi", description="AI-infrastructure CALL-options intelligence "
        "(research / paper mode only — never trades).")
    p.add_argument("--version", action="version", version=f"coi {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def _common(sp):
        sp.add_argument("--config-dir", help="override config directory")
        sp.add_argument("--fixtures-dir", help="override fixtures directory")
        sp.add_argument("--live", action="store_true",
                        help="use FREE live sources instead of offline fixtures")

    sp = sub.add_parser("scan", help="run a scan and write reports")
    _common(sp)
    sp.add_argument("--tickers", nargs="*", help="restrict to these tickers")
    sp.add_argument("--output", help="output stem (default reports/latest)")
    sp.add_argument("--format", nargs="*",
                    help="formats: markdown csv json html (default from config)")
    sp.add_argument("--thesis-dir", help="directory of thesis markdown/text")
    sp.add_argument("--record", action="store_true", help="record signals to store")
    sp.add_argument("--store", default="reports/signals.jsonl")
    sp.add_argument("--horizon", type=int, default=30)
    sp.add_argument("--top-only", action="store_true")
    sp.add_argument("--person-intel", action="store_true",
                    help="also write a person-intelligence (research vs trade) panel")
    sp.add_argument("--iv-history",
                    help="IV-history store path; uses a real IV rank once warmed")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("backtest", help="record / evaluate paper signals")
    _common(sp)
    sp.add_argument("action", choices=["record", "evaluate", "demo"],
                    default="evaluate", nargs="?")
    sp.add_argument("--store", default="reports/signals.jsonl")
    sp.add_argument("--tickers", nargs="*")
    sp.add_argument("--horizon", type=int, default=30)
    sp.add_argument("--top-only", action="store_true")
    sp.add_argument("--thesis-dir")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("update-13f", help="recompute 13F changes")
    _common(sp)
    sp.set_defaults(func=cmd_update_13f)

    sp = sub.add_parser("explain", help="explain one ticker's score")
    _common(sp)
    sp.add_argument("ticker")
    sp.add_argument("--thesis-dir")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("person-intel",
                        help="person-intelligence: research vs trade (thesis≠trade)")
    sp.add_argument("--config-dir")
    sp.add_argument("--fixtures-dir")
    sp.add_argument("--tickers", nargs="*", help="restrict to these tickers")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_person_intel)

    sp = sub.add_parser("early-filings",
                        help="recent FAST EDGAR filings (Form 4 / 13D-G / Form D)")
    sp.add_argument("--config-dir")
    sp.add_argument("--fixtures-dir")
    sp.add_argument("--live", action="store_true",
                    help="query SEC EDGAR (free) instead of offline fixtures")
    sp.add_argument("--since", type=int, default=30, help="lookback days")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--resolve", action="store_true",
                    help="resolve each filing's subject company to a ticker")
    sp.set_defaults(func=cmd_early_filings)

    sp = sub.add_parser("person-monitor",
                        help="live dedup'd Thiel/Aschenbrenner signal radar "
                             "(emails only on a NEW signal)")
    sp.add_argument("--config-dir")
    sp.add_argument("--fixtures-dir")
    sp.add_argument("--live", action="store_true",
                    help="query SEC EDGAR (free) instead of offline fixtures")
    sp.add_argument("--since", type=int, default=45,
                    help="lookback window in days (default 45)")
    sp.add_argument("--state", default="data/person_intel/seen_filings.json",
                    help="dedup/history store path")
    sp.add_argument("--email", action="store_true",
                    help="send the digest IF (and only if) there is a new signal")
    sp.add_argument("--min-weight", type=float, default=0.0,
                    help="drop alerts below this principal-path weight (0..1)")
    sp.add_argument("--no-resolve", action="store_true",
                    help="skip CUSIP subject resolution (fewer SEC requests)")
    sp.add_argument("--dry-run", action="store_true",
                    help="report without persisting state or sending email")
    sp.set_defaults(func=cmd_person_monitor)

    sp = sub.add_parser("proxies",
                        help="ranked private→public proxy map per thesis cluster")
    sp.add_argument("--config-dir")
    sp.add_argument("--fixtures-dir")
    sp.add_argument("--cluster", nargs="*", help="restrict to these clusters")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--live", action="store_true",
                    help="use live options data for liquidity/options-quality")
    sp.set_defaults(func=cmd_proxies)

    sp = sub.add_parser("outcomes-report",
                        help="multi-horizon outcomes + walk-forward edge guard")
    _common(sp)
    sp.add_argument("--store", default="reports/outcomes.jsonl")
    sp.add_argument("--demo", action="store_true",
                    help="seed a synthetic in/out-of-sample store and report on it")
    sp.add_argument("--historical", action="store_true",
                    help="real per-horizon pricing from offline historical CSV fixtures")
    sp.add_argument("--split", default="2026-03-01",
                    help="walk-forward split date (in- vs out-of-sample)")
    sp.add_argument("--horizon", type=int, default=30)
    sp.add_argument("--min-sample", type=int, default=30)
    sp.set_defaults(func=cmd_outcomes_report)

    sp = sub.add_parser("record-iv",
                        help="append ATM IV per ticker to the IV-history store")
    _common(sp)
    sp.add_argument("--tickers", nargs="*")
    sp.add_argument("--store", default="reports/iv_history.jsonl")
    sp.add_argument("--warmup", type=int, default=20)
    sp.set_defaults(func=cmd_record_iv)

    sp = sub.add_parser("doctor", help="health-check the install")
    sp.add_argument("--config-dir")
    sp.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except Exception as exc:  # pragma: no cover - top-level safety net
        logging.getLogger("coi.cli").error("Command failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
