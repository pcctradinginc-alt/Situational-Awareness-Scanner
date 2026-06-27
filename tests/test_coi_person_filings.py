"""Tests for filing taxonomy, instrument typing, verification and CUSIP map
(Goals B, C, D)."""
from pathlib import Path

from call_options_intel.person_intel.filings import (
    Direction, FilingType, InstrumentType, SignalRole, SourceSystem,
    VerificationStatus, ADVAdapter, assess_verification, classify_form,
    classify_instrument, filing_meta, infer_direction, parse_13f_infotable_typed,
    parse_form_d, parse_form_d_xml, parse_schedule_13d,
)
from call_options_intel.person_intel.cusip_map import (
    CusipMapper, cusip_check_digit, is_valid_cusip, load_mapper,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ── B: filing taxonomy ──────────────────────────────────────────────────────
def test_classify_form_variants():
    assert classify_form("13F-HR") is FilingType.FORM_13F_HR
    assert classify_form("13F-HR/A") is FilingType.FORM_13F_HR_A
    assert classify_form("SC 13D") is FilingType.SC_13D
    assert classify_form("SC 13D/A") is FilingType.SC_13D_A
    assert classify_form("SC 13G") is FilingType.SC_13G
    assert classify_form("Form 4") is FilingType.FORM_4
    assert classify_form("Form D") is FilingType.FORM_D
    assert classify_form("ADV") is FilingType.FORM_ADV
    assert classify_form("totally-bogus") is FilingType.UNKNOWN
    assert classify_form(None) is FilingType.UNKNOWN


def test_13f_is_confirmation_not_early():
    m13f = filing_meta(FilingType.FORM_13F_HR)
    assert m13f.role is SignalRole.CONFIRMATION
    # 13D / Form 4 / Form D are faster (smaller lag) than 13F
    assert filing_meta(FilingType.FORM_4).typical_lag_days < m13f.typical_lag_days
    assert filing_meta(FilingType.SC_13D).typical_lag_days < m13f.typical_lag_days
    assert filing_meta(FilingType.FORM_D).typical_lag_days < m13f.typical_lag_days
    assert filing_meta(FilingType.FORM_4).role is SignalRole.EARLY


def test_adv_is_not_edgar():
    m = filing_meta(FilingType.FORM_ADV)
    assert m.source is SourceSystem.IAPD
    assert m.is_edgar is False
    assert filing_meta(FilingType.FORM_13F_HR).is_edgar is True


# ── C: instrument typing + direction ────────────────────────────────────────
def test_classify_instrument_types():
    assert classify_instrument("COM", None, "NVIDIA CORP") is InstrumentType.COMMON
    assert classify_instrument("COM", "Call", "NVIDIA CORP") is InstrumentType.CALL
    assert classify_instrument("COM", "Put", "MICRON") is InstrumentType.PUT
    assert classify_instrument("SPONSORED ADR", None, "TAIWAN SEMI") is InstrumentType.ADR
    assert classify_instrument("ETF", None, "ISHARES SEMICONDUCTOR ETF") is InstrumentType.ETF


def test_options_direction_is_unknown_not_naive():
    # the core rule: a 13F CALL/PUT line is NOT read as naive long/short
    assert infer_direction(InstrumentType.CALL) is Direction.DIRECTION_UNKNOWN
    assert infer_direction(InstrumentType.PUT) is Direction.DIRECTION_UNKNOWN
    assert infer_direction(InstrumentType.COMMON) is Direction.LONG_EXPOSURE
    assert infer_direction(InstrumentType.ADR) is Direction.LONG_EXPOSURE


def test_parse_typed_infotable():
    xml = (FIXTURES / "sample_13f_typed.xml").read_text()
    rows = parse_13f_infotable_typed(xml)
    by = {(r.cusip, r.instrument) for r in rows}
    assert ("67066G104", InstrumentType.COMMON) in by
    assert ("67066G104", InstrumentType.CALL) in by
    assert ("595112103", InstrumentType.PUT) in by
    assert ("874039100", InstrumentType.ADR) in by
    # the CALL row must not be a naive long
    call_row = next(r for r in rows if r.instrument is InstrumentType.CALL)
    assert call_row.direction is Direction.DIRECTION_UNKNOWN


# ── C: verification status ──────────────────────────────────────────────────
def test_verification_common_long_verified():
    st, why = assess_verification(InstrumentType.COMMON, Direction.LONG_EXPOSURE,
                                  mapping_confidence=0.97, portfolio_pct=0.20)
    assert st is VerificationStatus.VERIFIED
    assert why


def test_verification_option_not_verifiable_via_13f():
    st, _ = assess_verification(InstrumentType.CALL, Direction.DIRECTION_UNKNOWN,
                                mapping_confidence=0.97, portfolio_pct=0.05)
    assert st is VerificationStatus.NOT_VERIFIABLE_VIA_13F


def test_verification_low_mapping_needs_human():
    st, _ = assess_verification(InstrumentType.COMMON, Direction.LONG_EXPOSURE,
                                mapping_confidence=0.2, portfolio_pct=0.05)
    assert st is VerificationStatus.NEEDS_HUMAN_REVIEW
    st2, _ = assess_verification(InstrumentType.COMMON, Direction.LONG_EXPOSURE,
                                 mapping_confidence=None, portfolio_pct=0.05)
    assert st2 is VerificationStatus.NEEDS_HUMAN_REVIEW


def test_verification_immaterial_is_noise():
    st, _ = assess_verification(InstrumentType.COMMON, Direction.LONG_EXPOSURE,
                                mapping_confidence=0.97, portfolio_pct=0.0001)
    assert st is VerificationStatus.REJECTED_NOISE


def test_verification_etf_partial():
    st, _ = assess_verification(InstrumentType.ETF, Direction.LONG_EXPOSURE,
                                mapping_confidence=0.9, portfolio_pct=0.05)
    assert st is VerificationStatus.PARTIALLY_VERIFIED


# ── B: 13D + Form D parsing ─────────────────────────────────────────────────
def test_parse_schedule_13d():
    f = parse_schedule_13d({
        "form_type": "SC 13D", "subject_issuer": "Palantir",
        "cusip": "69608A108", "reporting_person": "Peter Thiel",
        "percent_of_class": 7.5, "purpose": "strategic stake"})
    assert f.filing_type is FilingType.SC_13D
    assert f.percent_of_class == 7.5
    assert f.reporting_person == "Peter Thiel"


def test_parse_form_d_json_and_xml():
    j = parse_form_d({"issuer_name": "Newco AI", "total_offering_amount": 5e7,
                      "total_amount_sold": 2.5e7, "industry_group": "Technology",
                      "related_persons": ["Peter Thiel"]})
    assert j.filing_type is FilingType.FORM_D
    assert j.total_amount_sold == 2.5e7
    assert "Peter Thiel" in j.related_persons

    xml = """<?xml version='1.0'?>
    <edgarSubmission>
      <primaryIssuer><entityName>Frontier Compute LLC</entityName></primaryIssuer>
      <offeringData>
        <industryGroup><industryGroupType>Technology</industryGroupType></industryGroup>
        <offeringSalesAmounts>
          <totalOfferingAmount>100000000</totalOfferingAmount>
          <totalAmountSold>40000000</totalAmountSold>
        </offeringSalesAmounts>
        <dateOfFirstSale>2026-06-01</dateOfFirstSale>
      </offeringData>
      <relatedPersonsList>
        <relatedPersonInfo><firstName>Peter</firstName><lastName>Thiel</lastName></relatedPersonInfo>
      </relatedPersonsList>
    </edgarSubmission>"""
    f = parse_form_d_xml(xml)
    assert f is not None
    assert f.issuer_name == "Frontier Compute LLC"
    assert f.total_amount_sold == 40000000
    assert f.related_persons == ["Peter Thiel"]
    assert f.date_of_first_sale == "2026-06-01"


def test_adv_adapter_is_context_only():
    recs = ADVAdapter().parse_records([{"adviser_name": "Thiel Capital", "crd_number": "123"}])
    assert len(recs) == 1
    assert recs[0].source is SourceSystem.IAPD
    assert recs[0].needs_human_review is True


# ── D: CUSIP mapping ────────────────────────────────────────────────────────
def test_cusip_check_digit_validation():
    assert is_valid_cusip("67066G104") is True       # NVDA
    assert is_valid_cusip("67066G105") is False      # wrong check digit
    assert is_valid_cusip("123") is False
    assert cusip_check_digit("67066G10") == 4


def test_explicit_map_high_confidence():
    m = CusipMapper(explicit_map={"67066G104": {"ticker": "NVDA", "source": "x",
                                                 "confidence": 0.97}})
    r = m.map_cusip("67066G104", "NVIDIA CORP")
    assert r.mapped_ticker == "NVDA"
    assert r.confidence >= 0.9
    assert r.mapping_source == "explicit_map"
    assert r.needs_human_review is False


def test_name_heuristic_needs_review():
    m = CusipMapper(name_hints={"nvidia": "NVDA"})
    r = m.map_cusip("999999999", "NVIDIA CORP")
    assert r.mapped_ticker == "NVDA"
    assert r.mapping_source == "name_heuristic"
    assert r.needs_human_review is True
    assert 0 < r.confidence < 0.9


def test_no_match_returns_no_ticker_with_flag():
    m = CusipMapper()
    r = m.map_cusip("000000000", "Totally Unknown Co")
    assert r.mapped_ticker is None
    assert r.confidence == 0.0
    assert r.mapping_source == "none"
    assert r.needs_human_review is True          # never a silent guess


def test_invalid_cusip_in_map_is_downgraded():
    m = CusipMapper(explicit_map={"67066G105": {"ticker": "NVDA", "confidence": 0.97}})
    r = m.map_cusip("67066G105")                 # bad check digit
    assert r.mapped_ticker == "NVDA"
    assert r.confidence <= 0.55
    assert r.needs_human_review is True


def test_bundled_cusip_config_loads():
    m = load_mapper()
    r = m.map_cusip("67066G104", "NVIDIA CORP")
    assert r.mapped_ticker == "NVDA"
    assert r.needs_human_review is False


def test_widened_tier_a_confident():
    m = load_mapper()
    for cusip, ticker in [("573874104", "MRVL"), ("697435105", "PANW"),
                          ("46090E103", "QQQ")]:
        r = m.map_cusip(cusip)
        assert r.mapped_ticker == ticker
        assert r.needs_human_review is False        # >= mapping_floor


def test_widened_tier_b_flagged_for_review():
    # check-digit-valid but not authoritatively confirmed -> mapped yet flagged
    m = load_mapper()
    r = m.map_cusip("86800U104")                     # SMCI, confidence 0.55
    assert r.mapped_ticker == "SMCI"
    assert r.needs_human_review is True
    assert r.confidence < 0.6
