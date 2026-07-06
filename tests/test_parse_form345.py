"""Offline unit tests for the Form 3/4/5 parser (no network required).

Fixtures are the real SHAZ (SharonAI Holdings) Form 3 and Form 4 filed by
Situational Awareness LP in June 2026.

Run: python -m pytest tests/  (or simply: python tests/test_parse_form345.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parsers import parse_form345

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_form4_transactions():
    parsed = parse_form345.parse_document((FIXTURES / "form4_shaz.xml").read_text())
    assert parsed is not None
    assert parsed["document_type"] == "4"
    assert parsed["issuer_ticker"] == "SHAZ"
    assert parsed["issuer_name"] == "SharonAI Holdings Inc."
    assert parsed["is_ten_percent_owner"] is True
    assert len(parsed["transactions"]) >= 1
    t = parsed["transactions"][0]
    assert t["code"] == "X"
    assert t["shares"] == 3_700_000
    assert t["acquired_disposed"] == "A"
    assert t["post_shares"] == 5_396_127


def test_form4_summary():
    parsed = parse_form345.parse_document((FIXTURES / "form4_shaz.xml").read_text())
    s = parse_form345.summarize(parsed)
    assert "Form 4" in s
    assert "SHAZ" in s
    assert "ACQUIRED 3,700,000" in s
    assert "10% owner" in s


def test_form3_initial_statement():
    parsed = parse_form345.parse_document((FIXTURES / "form3_shaz.xml").read_text())
    assert parsed is not None
    assert parsed["document_type"] == "3"
    assert parsed["issuer_ticker"] == "SHAZ"
    s = parse_form345.summarize(parsed)
    assert "Form 3" in s
    assert "initial ownership" in s


def test_non_ownership_xml_returns_none():
    assert parse_form345.parse_document("<html><body>not xml we want</body></html>") is None
    assert parse_form345.parse_document("no xml at all") is None


if __name__ == "__main__":
    test_form4_transactions()
    test_form4_summary()
    test_form3_initial_statement()
    test_non_ownership_xml_returns_none()
    print("All tests passed.")
