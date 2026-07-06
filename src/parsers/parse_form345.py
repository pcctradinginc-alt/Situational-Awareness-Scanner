"""Parse SEC Forms 3, 4 and 5 (Section 16 ownership filings).

These are the earliest legal per-trade disclosure that exists: once a fund is
a >10% beneficial owner (as Situational Awareness LP is in e.g. SharonAI/SHAZ),
every single transaction must be reported on Form 4 within 2 business days —
versus 45 days for the quarterly 13F.

The primary document is a structured ``ownershipDocument`` XML, so parsing is
exact (no regex fallbacks needed).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

# Section 16 transaction codes (SEC Form 4 instructions, table I/II)
TRANSACTION_CODES: dict[str, str] = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant/award",
    "D": "disposition to issuer",
    "F": "tax withholding",
    "G": "gift",
    "M": "option exercise",
    "X": "exercise of derivative",
    "C": "conversion of derivative",
    "V": "voluntary early report",
    "J": "other",
}


def _txt(el: ET.Element | None, path: str = "") -> str:
    """Text of a (possibly nested) element, '' if missing."""
    if el is None:
        return ""
    target = el.find(path) if path else el
    return (target.text or "").strip() if target is not None and target.text else ""


def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_transaction(node: ET.Element) -> dict:
    code = _txt(node, "transactionCoding/transactionCode")
    return {
        "security_title": _txt(node, "securityTitle/value"),
        "date": _txt(node, "transactionDate/value"),
        "code": code,
        "code_label": TRANSACTION_CODES.get(code, f"code {code}"),
        "shares": _num(_txt(node, "transactionAmounts/transactionShares/value")),
        "price": _num(_txt(node, "transactionAmounts/transactionPricePerShare/value")),
        "acquired_disposed": _txt(node, "transactionAmounts/transactionAcquiredDisposedCode/value"),
        "post_shares": _num(_txt(node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
        "direct": _txt(node, "ownershipNature/directOrIndirectOwnership/value") == "D",
    }


def _parse_holding(node: ET.Element) -> dict:
    return {
        "security_title": _txt(node, "securityTitle/value"),
        "shares": _num(
            _txt(node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        ),
        "direct": _txt(node, "ownershipNature/directOrIndirectOwnership/value") == "D",
    }


def parse_document(xml_text: str) -> dict | None:
    """Parse one ownershipDocument XML. Returns None if it is not one."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        return None

    rel = root.find("reportingOwner/reportingOwnerRelationship")
    parsed = {
        "document_type": _txt(root, "documentType"),
        "period": _txt(root, "periodOfReport"),
        "issuer_cik": _txt(root, "issuer/issuerCik"),
        "issuer_name": _txt(root, "issuer/issuerName"),
        "issuer_ticker": _txt(root, "issuer/issuerTradingSymbol"),
        "owner_name": _txt(root, "reportingOwner/reportingOwnerId/rptOwnerName"),
        "is_ten_percent_owner": _txt(rel, "isTenPercentOwner") in ("1", "true"),
        "is_director": _txt(rel, "isDirector") in ("1", "true"),
        "is_officer": _txt(rel, "isOfficer") in ("1", "true"),
        "transactions": [
            _parse_transaction(n)
            for n in root.findall("nonDerivativeTable/nonDerivativeTransaction")
        ],
        "holdings": [
            _parse_holding(n)
            for n in root.findall("nonDerivativeTable/nonDerivativeHolding")
        ],
        "derivative_transactions": [
            _parse_transaction(n)
            for n in root.findall("derivativeTable/derivativeTransaction")
        ],
        "derivative_holdings": [
            _parse_holding(n)
            for n in root.findall("derivativeTable/derivativeHolding")
        ],
    }
    return parsed


def parse_filing(filing_dir: Path) -> dict | None:
    """Locate and parse the ownershipDocument XML inside a filing directory."""
    for xml_path in sorted(filing_dir.glob("*.xml")):
        try:
            text = xml_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        parsed = parse_document(text)
        if parsed:
            return parsed
    return None


def _fmt_shares(n: float | None) -> str:
    return f"{int(n):,}" if n is not None else "?"


def summarize(parsed: dict) -> str:
    """One-line, self-contained summary used as the alert event text."""
    issuer = parsed["issuer_name"] or "unknown issuer"
    ticker = f" ({parsed['issuer_ticker']})" if parsed["issuer_ticker"] else ""
    role = " [10% owner]" if parsed["is_ten_percent_owner"] else ""
    doc = parsed["document_type"]

    txns = parsed["transactions"] or parsed["derivative_transactions"]
    if txns:
        parts = []
        for t in txns[:3]:
            verb = "ACQUIRED" if t["acquired_disposed"] == "A" else "DISPOSED"
            price = f" @ ${t['price']:,.4g}" if t["price"] is not None else ""
            parts.append(
                f"{verb} {_fmt_shares(t['shares'])} shares{price}"
                f" ({t['code_label']}) on {t['date']}"
            )
        if len(txns) > 3:
            parts.append(f"+{len(txns) - 3} more transaction(s)")
        post = next((t["post_shares"] for t in reversed(txns) if t["post_shares"] is not None), None)
        post_str = f"; now holds {_fmt_shares(post)} shares" if post is not None else ""
        return f"Form {doc}: {issuer}{ticker} — {'; '.join(parts)}{post_str}.{role}"

    # Form 3 (initial statement) — report holdings instead of transactions
    total = sum(h["shares"] or 0 for h in parsed["holdings"])
    deriv = len(parsed["derivative_holdings"])
    deriv_str = f" + {deriv} derivative holding(s)" if deriv else ""
    return (
        f"Form {doc}: initial ownership statement for {issuer}{ticker} — "
        f"{_fmt_shares(total)} shares{deriv_str} as of {parsed['period']}.{role}"
    )
