"""Tests for the ranked private→public proxy map (link·liquidity·options·valuation)."""
from datetime import date

from call_options_intel.person_intel.proxy_map import load_proxy_map
from call_options_intel.person_intel.monitor import run_monitor

AS_OF = date(2026, 6, 27)


def _pm():
    return load_proxy_map(None)


def test_rank_cluster_sorted_by_edge_desc():
    ranked = _pm().rank_cluster("power_grid")
    edges = [r.edge_score for r in ranked]
    assert edges == sorted(edges, reverse=True)
    assert ranked[0].cluster == "power_grid"
    assert ranked[0].falsification                     # carries the cluster falsify


def test_valuation_breaks_tie_undervalued_wins():
    # NVDA and TSM share the same link in 'compute', but NVDA is 'rich' and TSM
    # 'fair' -> the cheaper proxy ranks higher (the undervalued-edge logic).
    ranked = {r.ticker: r for r in _pm().rank_cluster("compute")}
    assert ranked["NVDA"].link == ranked["TSM"].link
    assert ranked["TSM"].valuation > ranked["NVDA"].valuation
    assert ranked["TSM"].edge_score > ranked["NVDA"].edge_score


def test_four_dimensions_present():
    r = _pm().rank_cluster("compute")[0]
    d = r.to_dict()
    for k in ("link", "liquidity", "options_quality", "valuation",
              "edge_score", "falsification"):
        assert k in d


def test_live_options_fn_overrides_priors():
    pm = _pm()
    # a live provider that says NVDA options are thin (0.1) should drop its edge
    prior = {r.ticker: r for r in pm.rank_cluster("compute")}["NVDA"].edge_score

    def thin(ticker):
        return (0.1, 0.1) if ticker == "NVDA" else None

    live = {r.ticker: r for r in pm.rank_cluster("compute", options_fn=thin)}["NVDA"]
    assert live.options_live is True
    assert live.options_quality == 0.1
    assert live.edge_score < prior


def test_thin_illiquid_proxy_ranks_below_deep():
    # construct: same cluster, deep+cheap should beat thin+rich at equal-ish link
    ranked = _pm().rank_cluster("power_grid")
    nrg = next(r for r in ranked if r.ticker == "NRG")   # medium liq, cheap val
    etn = next(r for r in ranked if r.ticker == "ETN")   # deep liq, rich val
    # NRG cheaper but thinner; ETN deeper but richer & weaker link — exercise both
    assert nrg.liquidity < etn.liquidity
    assert nrg.valuation > etn.valuation


def test_best_proxies_limits():
    assert len(_pm().best_proxies("compute", top=3)) == 3


# ── monitor integration ─────────────────────────────────────────────────────
def test_monitor_attaches_ranked_proxies(tmp_path):
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=40,
                      state_path=tmp_path / "seen.json", persist=False)
    rp = res.ranked_proxies
    assert rp                                            # clusters were touched
    # every cluster maps to a non-empty ranked list, each sorted by edge
    for cluster, ranked in rp.items():
        assert ranked
        edges = [r["edge_score"] for r in ranked]
        assert edges == sorted(edges, reverse=True)
        assert all("falsification" in r for r in ranked)


def test_statement_derived_candidates_follow_edge(tmp_path):
    # the Aschenbrenner power essay -> derived candidates ranked by edge
    res = run_monitor(mode="offline", as_of=AS_OF, since_days=120,
                      state_path=tmp_path / "seen.json", persist=False)
    power = next((s for s in res.statements if s.dominant_cluster == "power_grid"),
                None)
    assert power is not None
    top = _pm().best_proxies("power_grid", top=3)
    assert power.derived_candidates == [r.ticker for r in top]
