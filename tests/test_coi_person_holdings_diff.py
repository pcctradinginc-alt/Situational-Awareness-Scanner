"""Tests for the CUSIP-level 13F position-diff (new/add/trim/exit)."""
from call_options_intel.person_intel.cusip_map import load_mapper
from call_options_intel.person_intel.holdings_diff import (
    diff_infotables, fetch_infotable_xml, summarize_changes,
)

NS = 'xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"'


def _it(name, cusip, shares, value, put_call=None):
    pc = f"<putCall>{put_call}</putCall>" if put_call else ""
    return (f"<infoTable><nameOfIssuer>{name}</nameOfIssuer>"
            f"<titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>"
            f"<value>{value}</value>"
            f"<shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt>"
            f"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>{pc}</infoTable>")


def _table(*items):
    return f"<informationTable {NS}>" + "".join(items) + "</informationTable>"


def _mapper():
    return load_mapper(None)


def test_new_add_trim_exit_detected():
    prior = _table(
        _it("NVIDIA CORP", "67066G104", 100000, 12000000),
        _it("MICRON TECHNOLOGY INC", "595112103", 50000, 6000000),
        _it("TAIWAN SEMICONDUCTOR MFG LTD", "874039100", 40000, 4000000),
    )
    current = _table(
        _it("NVIDIA CORP", "67066G104", 150000, 18000000),   # +50% -> add
        _it("MICRON TECHNOLOGY INC", "595112103", 30000, 3600000),  # -40% -> trim
        # TSM dropped -> exit
        _it("ARISTA NETWORKS INC", "0404131064" if False else "040413106",
            20000, 5000000),                                  # brand new
    )
    changes = {c.ticker or c.cusip: c for c in
               diff_infotables(current, prior, _mapper())}
    assert changes["NVDA"].action == "add"
    assert round(changes["NVDA"].pct_change, 2) == 0.50
    assert changes["MU"].action == "trim"
    assert changes["MU"].pct_change < 0
    assert changes["TSM"].action == "exit"
    # the new name is present as a 'new' action
    assert any(c.action == "new" for c in
               diff_infotables(current, prior, _mapper()))


def test_hold_is_not_surfaced():
    prior = _table(_it("NVIDIA CORP", "67066G104", 100000, 12000000))
    current = _table(_it("NVIDIA CORP", "67066G104", 100500, 12060000))  # +0.5%
    assert diff_infotables(current, prior, _mapper()) == []


def test_option_leg_flagged_direction_unknown():
    prior = _table(_it("NVIDIA CORP", "67066G104", 100000, 12000000))
    current = _table(
        _it("NVIDIA CORP", "67066G104", 100000, 12000000),
        _it("MICRON TECHNOLOGY INC", "595112103", 5000, 500000, put_call="Call"),
    )
    changes = diff_infotables(current, prior, _mapper())
    mu = next(c for c in changes if c.cusip == "595112103")
    assert mu.action == "new"
    assert "direction_unknown" in mu.instrument_note


def test_unmapped_cusip_needs_review_not_guessed():
    # a syntactically-valid but unmapped CUSIP must not get a guessed ticker
    prior = _table()
    current = _table(_it("SOME PRIVATE HOLDCO", "000000000", 1000, 1000))
    changes = diff_infotables(current, prior, _mapper())
    c = changes[0]
    assert c.ticker is None
    assert c.needs_human_review is True


def test_summary_orders_new_first():
    prior = _table(_it("MICRON TECHNOLOGY INC", "595112103", 50000, 6000000))
    current = _table(
        _it("MICRON TECHNOLOGY INC", "595112103", 60000, 7200000),   # add
        _it("NVIDIA CORP", "67066G104", 10000, 1200000),             # new
    )
    summary = summarize_changes(diff_infotables(current, prior, _mapper()))
    assert summary.startswith("NEW NVDA")


def test_fetch_infotable_navigates_index():
    table = _table(_it("NVIDIA CORP", "67066G104", 1000, 100000))

    class _Fetcher:
        def get(self, url):
            if url.endswith("index.json"):
                return ('{"directory": {"item": ['
                        '{"name": "primary_doc.xml"},'
                        '{"name": "form13fInfoTable.xml"}]}}')
            if url.endswith("form13fInfoTable.xml"):
                return table
            return None

    xml = fetch_infotable_xml(_Fetcher(), "0001211060", "0001211060-26-000040")
    assert xml is not None and "NVIDIA" in xml


def test_fetch_infotable_missing_returns_none():
    class _Empty:
        def get(self, url):
            return None
    assert fetch_infotable_xml(_Empty(), "0001211060", "x-26-1") is None
