"""Tests for the live-EDGAR fast-filings adapter (Phase 2), offline-injected."""
from datetime import date

from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.edgar_fast import (
    EdgarFastClient, FastFilingMonitor, FixtureFetcher, load_fast_client,
)
from call_options_intel.person_intel.filings import FilingType, SignalRole

AS_OF = date(2026, 6, 26)


class _FakeFetcher:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        return self.payload


def _client_from_fixture():
    return EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))


def test_recent_filings_are_typed():
    rows = _client_from_fixture().recent_filings("0001211060", "Thiel Peter")
    fts = [r.filing_type for r in rows]
    assert FilingType.FORM_4 in fts
    assert FilingType.SC_13D_A in fts
    assert FilingType.SC_13G in fts
    assert FilingType.FORM_13F_HR in fts
    assert FilingType.UNKNOWN in fts          # the 8-K is not a tracked form


def test_fast_filings_exclude_13f_and_noise():
    rows = _client_from_fixture().fast_filings("0001211060")
    fts = {r.filing_type for r in rows}
    assert FilingType.FORM_13F_HR not in fts   # 13F is confirmation, not fast
    assert FilingType.UNKNOWN not in fts       # 8-K dropped
    assert FilingType.FORM_4 in fts


def test_form4_is_most_leading():
    rows = _client_from_fixture().recent_filings("0001211060")
    form4 = next(r for r in rows if r.filing_type is FilingType.FORM_4)
    assert form4.role is SignalRole.EARLY
    assert form4.lag_days <= 2
    assert form4.age_days(AS_OF) == 2          # filed 2026-06-24


def test_url_is_constructed():
    rows = _client_from_fixture().recent_filings("0001211060")
    form4 = next(r for r in rows if r.filing_type is FilingType.FORM_4)
    assert form4.url.startswith(
        "https://www.sec.gov/Archives/edgar/data/1211060/000121106026000045/")


def test_feed_filters_by_recency_and_sorts():
    client = _client_from_fixture()
    mon = FastFilingMonitor(client)
    feed = mon.feed([{"cik": "0001211060", "name": "Thiel Peter"}],
                    since_days=30, as_of=AS_OF)
    # only the 3 fast filings within 30 days (Form4, 13D/A, 13G); 13F is 42d + slow
    assert len(feed) == 3
    assert feed[0].filing_date == "2026-06-24"      # most recent first
    assert all(r.age_days(AS_OF) <= 30 for r in feed)


def test_missing_fixture_is_graceful():
    rows = _client_from_fixture().recent_filings("9999999999", "Ghost")
    assert rows == []


def test_fake_fetcher_parses_inline_json():
    payload = (
        '{"name":"X","filings":{"recent":{'
        '"accessionNumber":["0001-26-000001"],"filingDate":["2026-06-25"],'
        '"form":["SC 13D"],"primaryDocument":["d.htm"]}}}')
    client = EdgarFastClient(fetcher=_FakeFetcher(payload))
    rows = client.recent_filings("123", "X")
    assert len(rows) == 1 and rows[0].filing_type is FilingType.SC_13D


def test_load_fast_client_offline_uses_fixtures():
    client = load_fast_client({}, "offline", DEFAULT_FIXTURES)
    assert isinstance(client.fetcher, FixtureFetcher)
