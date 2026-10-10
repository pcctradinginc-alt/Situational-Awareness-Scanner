"""Offline tests: an alert email goes out once per newly opened position.

Run: python -m pytest tests/
"""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alert
from src.config import Config, Paths, load_config
from src.utils import append_jsonl, read_json, write_json


def _row(issuer, ticker="", cusip="", shares=100):
    return {"issuer": issuer, "ticker": ticker, "cusip": cusip, "shares_latest": shares}


def _model(stock, exits=(), report_date="2026-06-30"):
    return {"available": True, "summary": {"latest_quarter": "Q2 2026", "report_date": report_date},
            "common_stock": list(stock), "new_buys": [], "exits": list(exits), "options": []}


def _news(eid, title, tickers=(), tier="alpha_signal", conf=0.92, category="invest"):
    return {"event_id": eid, "signal_type": "public_statement", "signal_tier": tier,
            "confidence": conf, "summary": title, "ticker_guess": list(tickers),
            "timestamp": "2026-10-01T00:00:00Z",
            "sources": [{"kind": "google_news", "signal_category": category, "llm_quote": ""}]}


def _sec(eid, sig, issuer, ticker="", cusip=""):
    return {"event_id": eid, "signal_type": sig, "confidence": 1.0, "ticker_guess": [],
            "summary": f"{sig}: {issuer}", "timestamp": "2026-10-01T00:00:00Z",
            "sources": [{"kind": "sec_filing", "issuer_name": issuer,
                         "issuer_ticker": ticker, "issuer_cusip": cusip}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Config on tmp dirs, a captured mailbox, and helpers to add events."""
    cfg = Config(copy.deepcopy(load_config().raw))
    paths = Paths(root=tmp_path, **{d: tmp_path / d for d in
                                    ("reference", "raw", "parsed", "derived", "state")})
    paths.ensure()
    object.__setattr__(cfg, "paths", paths)

    sent: list[str] = []
    ok = {"send": True}

    def fake_send(_cfg, subject, _html):
        if ok["send"]:
            sent.append(subject)
        return ok["send"]

    monkeypatch.setattr(alert, "_send", fake_send)

    class Env:
        pass

    e = Env()
    e.cfg, e.sent, e.ok = cfg, sent, ok
    e.add = lambda *events: [append_jsonl(paths.parsed / "events.jsonl", ev) for ev in events]
    e.run = lambda model: alert.check_and_alert(cfg, model=model)
    e.state = lambda: read_json(paths.state / "alert_state.json")
    return e


HELD = _model([_row("NEBIUS GROUP N.V.", cusip="N97284108"), _row("SANDISK CORP", "SNDK")])


def test_article_about_held_position_is_not_emailed(env):
    # Nebius is in the 13F without a ticker mapping; the LLM guesses vary.
    env.add(_news("n1", "Why Leopold Aschenbrenner Just Bought 5.6% of Nebius - MSN", ["NEBU"]),
            _news("n2", "Nebius Stock Is Up 170%, and Aschenbrenner Just Bought a 5.6% Stake - Yahoo",
                  ["NEBIUS"]))
    assert env.run(HELD) == 0
    assert env.sent == []
    # ...and they are recorded, so they are not re-examined forever.
    assert {"n1", "n2"} <= set(env.state()["alerted_event_ids"])


def test_new_position_is_emailed_exactly_once(env):
    env.run(HELD)  # seed
    env.add(_news("o1", "Leopold Aschenbrenner Takes Stake in Oklo Inc - Reuters", ["OKLO"]))
    assert env.run(HELD) == 1
    assert env.sent == ["SA Alert · Neue Position: Oklo Inc (OKLO)"]

    # Syndicated copies on later runs: other publisher, other ticker guess, other wording.
    env.add(_news("o2", "Leopold Aschenbrenner Takes Stake in Oklo Inc - MSN", ["OKL"]),
            _news("o3", "Aschenbrenner's fund discloses new position in Oklo - Bloomberg", []))
    assert env.run(HELD) == 0
    assert len(env.sent) == 1


def test_same_position_twice_in_one_run_is_one_email(env):
    env.run(HELD)
    env.add(_news("o1", "Leopold Aschenbrenner Takes Stake in Oklo Inc - Reuters", ["OKLO"]),
            _news("o2", "Leopold Aschenbrenner Takes Stake in Oklo Inc - MSN", ["OKL"]),
            _sec("s1", "ownership_13dg", "Oklo Inc.", cusip="02156V109"))
    assert env.run(HELD) == 1
    assert len(env.sent) == 1
    entry = [p for p in env.state()["reported_positions"] if "oklo" in p["names"]]
    assert len(entry) == 1 and entry[0]["cusips"] == ["02156V109"]


def test_updates_on_known_positions_are_not_emailed(env):
    env.add(_news("u1", "Situational Awareness LP Reduces Stake in Sandisk - MarketBeat",
                  ["SNDK"], tier="position_update", conf=1.0, category="sell"),
            _sec("a1", "ownership_13dg_amendment", "Nebius Group N.V."),
            _sec("f4", "insider_trade", "Sandisk Corp", ticker="SNDK"))
    assert env.run(HELD) == 0
    assert env.sent == []


def test_13f_emails_only_when_it_holds_an_unreported_position(env):
    env.run(HELD)
    filing = {"event_id": "13f_q3", "signal_type": "13f_position", "confidence": 1.0,
              "as_of": "2026-09-30", "summary": "Q3 2026 13F filed", "ticker_guess": [],
              "timestamp": "2026-11-14T00:00:00Z", "sources": [{"kind": "sec_filing"}]}
    env.add(filing)

    # Position model not rebuilt for this filing yet → wait, do not drop the event.
    assert env.run(HELD) == 0
    assert "13f_q3" not in env.state()["alerted_event_ids"]

    q3 = _model(HELD["common_stock"] + [_row("OKLO INC", "OKLO")], report_date="2026-09-30")
    assert env.run(q3) == 1
    assert env.sent == ["SA Alert · Neue Position: OKLO INC (OKLO)"]
    assert env.run(q3) == 0

    # A news echo of the freshly filed position is not a second email.
    env.add(_news("o1", "Aschenbrenner Takes Stake in Oklo - MSN", ["OKLO"]))
    assert env.run(q3) == 0
    assert len(env.sent) == 1


def test_reentry_after_13f_exit_counts_as_new_again(env):
    env.run(HELD)
    state = env.state()
    for p in state["reported_positions"]:
        p["first_reported_at"] = "2026-05-01T00:00:00Z"
    write_json(env.cfg.paths.state / "alert_state.json", state)

    exited = _model([_row("NEBIUS GROUP N.V.", cusip="N97284108")],
                    exits=[_row("SANDISK CORP", "SNDK", shares=0)], report_date="2026-09-30")
    env.add(_news("r1", "Leopold Aschenbrenner Takes Stake in Sandisk Corporation - Reuters", ["SNDK"]))
    assert env.run(exited) == 1
    assert len(env.sent) == 1
    env.add(_news("r2", "Leopold Aschenbrenner Takes Stake in Sandisk Corporation - MSN", ["SNDK"]))
    assert env.run(exited) == 0


def test_unsent_alert_is_retried_and_old_ids_are_not_purged(env):
    env.run(HELD)
    env.add(_news("o1", "Leopold Aschenbrenner Takes Stake in Oklo Inc - Reuters", ["OKLO"]))
    env.ok["send"] = False
    env.run(HELD)
    assert "o1" not in env.state()["alerted_event_ids"]
    env.ok["send"] = True
    assert env.run(HELD) == 1

    # IDs older than the cleanup window stay while the event is still on file.
    state = env.state()
    state["alerted_event_ids_ts"]["o1"] = "2026-01-01T00:00:00Z"
    write_json(env.cfg.paths.state / "alert_state.json", state)
    env.run(HELD)
    assert "o1" in env.state()["alerted_event_ids"]
    assert len(env.sent) == 1
