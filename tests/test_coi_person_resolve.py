"""Tests for fast-filing subject→ticker resolution (Phase 3.1)."""
from call_options_intel.pipeline import DEFAULT_FIXTURES
from call_options_intel.person_intel.cusip_map import load_mapper
from call_options_intel.person_intel.edgar_fast import (
    EdgarFastClient, FastFilingRef, FixtureFetcher, extract_cusip, extract_issuer,
)
from call_options_intel.person_intel.filings import FilingType, SignalRole

DOC = (
    "SCHEDULE 13D/A\n"
    "Name of Issuer: Palantir Technologies Inc.\n"
    "CUSIP No. 69608A108\n"
    "Percent of Class: 7.8%\n"
)


def _ref(ft=FilingType.SC_13D_A, doc="sc13da.htm"):
    return FastFilingRef(
        cik="0001211060", entity="Peter Thiel", form="SC 13D/A",
        filing_type=ft, role=SignalRole.EARLY, lag_days=2,
        filing_date="2026-06-22", accession="0001211060-26-000044",
        primary_doc=doc, url=f"https://www.sec.gov/Archives/edgar/data/1211060/x/{doc}")


def test_extract_cusip_and_issuer():
    assert extract_cusip(DOC) == "69608A108"
    assert extract_issuer(DOC).startswith("Palantir")
    assert extract_cusip("no cusip here 12345") is None       # not valid/length
    assert extract_cusip("CUSIP 999999999") is None           # bad check digit


def test_resolve_13d_to_ticker():
    client = EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))
    res = client.resolve_subject(_ref(), load_mapper(), doc_text=DOC)
    assert res.mapped_ticker == "PLTR"
    assert res.cusip == "69608A108"
    assert res.mapping_confidence >= 0.9
    assert res.needs_human_review is False


def test_resolve_no_cusip_needs_review():
    client = EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))
    res = client.resolve_subject(_ref(), load_mapper(),
                                 doc_text="cover page with no identifiers")
    assert res.mapped_ticker is None
    assert res.needs_human_review is True


def test_resolve_form_d_has_no_public_ticker():
    formd_xml = ("<edgarSubmission><primaryIssuer><entityName>Frontier Compute LLC"
                 "</entityName></primaryIssuer></edgarSubmission>")
    ref = _ref(ft=FilingType.FORM_D, doc="primary_doc.xml")
    client = EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))
    res = client.resolve_subject(ref, load_mapper(), doc_text=formd_xml)
    assert res.mapped_ticker is None
    assert res.subject_issuer == "Frontier Compute LLC"
    assert res.needs_human_review is True


def test_resolve_via_fixture_document():
    # end-to-end: the bundled 13D/A document fixture resolves to PLTR
    client = EdgarFastClient(fetcher=FixtureFetcher(DEFAULT_FIXTURES))
    rows = client.recent_filings("0001211060", "Thiel Peter")
    ref = next(r for r in rows if r.filing_type is FilingType.SC_13D_A)
    res = client.resolve_subject(ref, load_mapper())     # fetches the fixture doc
    assert res.mapped_ticker == "PLTR"
