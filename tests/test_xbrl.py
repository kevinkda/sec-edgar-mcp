"""Unit tests for :mod:`sec_edgar_mcp._xbrl` — Form 4 XBRL parser.

Covers:

* normal multi-transaction filings (mix of non-derivative + derivative);
* gift / indirect-ownership filings;
* missing optional fields (period_of_report, price_per_share, …);
* malformed XML → :class:`Form4ParseError`;
* size-cap & empty-input rejection;
* unknown transaction codes → ``raw_warnings`` (no exception);
* namespaced documents are tolerated (localname stripped);
* JSON round-trip via ``Form4Data.to_dict``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sec_edgar_mcp._xbrl import (
    KNOWN_TRANSACTION_CODES,
    MAX_INPUT_BYTES,
    Form4Data,
    Form4Transaction,
    parse_form4,
)
from sec_edgar_mcp.errors import Form4ParseError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "seed"


def _read(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_parse_aapl_multi_transaction() -> None:
    """3 transactions: 2 non-derivative (S sell, A acquire) + 1 derivative (M)."""
    data = parse_form4(_read("form4_aapl.xml"), accession_number="0000320193-24-000010")
    assert isinstance(data, Form4Data)
    assert data.issuer_cik == "0000320193"
    assert data.issuer_name == "Apple Inc."
    assert data.issuer_ticker == "AAPL"
    assert data.reporting_owner_name == "COOK TIMOTHY D"
    assert data.is_officer is True
    assert data.is_director is False
    assert data.is_ten_percent_owner is False
    assert data.officer_title == "CEO"
    assert data.period_of_report == date(2024, 4, 30)
    assert data.transaction_count == 3
    assert data.raw_warnings == ()
    # net values: only the S sale at 170.50 contributes (A grant has price=0,
    # derivative M has price=0).
    assert data.net_sell_value == Decimal("17050000.00")
    assert data.net_buy_value == Decimal("0")


def test_parse_aapl_transaction_breakdown() -> None:
    data = parse_form4(_read("form4_aapl.xml"))
    sell = data.transactions[0]
    grant = data.transactions[1]
    deriv = data.transactions[2]
    assert sell.code == "S"
    assert sell.shares == Decimal("100000")
    assert sell.price_per_share == Decimal("170.50")
    assert sell.acquired_or_disposed == "D"
    assert sell.direct_or_indirect == "D"
    assert sell.is_derivative is False
    assert sell.security_title == "Common Stock"
    assert grant.code == "A"
    assert grant.acquired_or_disposed == "A"
    assert deriv.is_derivative is True
    assert deriv.derivative_security_title == "Common Stock"


def test_parse_msft_gift_indirect_ownership() -> None:
    """Gift (G) transactions carry no price; indirect ownership (I) recorded."""
    data = parse_form4(_read("form4_msft_gift.xml"), accession_number="msft-test")
    assert data.is_director is True
    assert data.is_officer is True
    assert data.transaction_count == 1
    tx = data.transactions[0]
    assert tx.code == "G"
    assert tx.direct_or_indirect == "I"
    assert tx.price_per_share is None
    assert tx.shares == Decimal("5000")
    # Gift carries no value → net_sell stays 0 even though A/D = D.
    assert data.net_buy_value == Decimal("0")
    assert data.net_sell_value == Decimal("0")


# ---------------------------------------------------------------------------
# Tolerant parsing — missing optional fields
# ---------------------------------------------------------------------------


_MINIMAL_DOC = b"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer><issuerCik>0000123456</issuerCik></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerCik>0000999999</rptOwnerCik></reportingOwnerId>
    </reportingOwner>
</ownershipDocument>
"""


def test_parse_minimal_no_transactions() -> None:
    data = parse_form4(_MINIMAL_DOC, accession_number="empty")
    assert data.transaction_count == 0
    assert data.transactions == ()
    assert data.issuer_cik == "0000123456"
    assert data.reporting_owner_cik == "0000999999"
    assert data.is_officer is False
    assert data.officer_title is None
    assert data.net_buy_value == Decimal("0")
    assert data.net_sell_value == Decimal("0")
    assert data.period_of_report is None


_AMENDMENT_DOC = b"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4/A</documentType>
    <periodOfReport>not-a-date</periodOfReport>
    <issuer/>
    <reportingOwner/>
</ownershipDocument>
"""


def test_parse_amendment_with_bad_date_records_warning() -> None:
    data = parse_form4(_AMENDMENT_DOC)
    assert data.document_type == "4/A"
    assert data.period_of_report is None
    assert any(w.startswith("unparseable_date:") for w in data.raw_warnings)


_UNKNOWN_CODE_DOC = b"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer><issuerCik>0000111111</issuerCik></issuer>
    <reportingOwner>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding>
                <transactionCode>Q</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>not-a-number</value></transactionShares>
                <transactionPricePerShare><value/></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>?</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>X</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_unknown_codes_and_unparseable_decimal_warn_only() -> None:
    data = parse_form4(_UNKNOWN_CODE_DOC)
    assert data.transaction_count == 1
    tx = data.transactions[0]
    assert tx.code == "Q"
    assert tx.shares == Decimal("0")  # unparseable → 0
    assert tx.price_per_share is None
    assert tx.acquired_or_disposed == ""
    assert tx.direct_or_indirect == ""
    warnings = set(data.raw_warnings)
    assert any(w.startswith("unknown_transaction_code:") for w in warnings)
    assert any(w.startswith("unparseable_decimal:") for w in warnings)
    assert any(w.startswith("unknown_acquired_disposed_code:") for w in warnings)
    assert any(w.startswith("unknown_direct_or_indirect:") for w in warnings)


_NUMERIC_OVERFLOW_DOC = b"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer/>
    <reportingOwner/>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>9999999999999999999999999999999999</value></transactionShares>
                <transactionPricePerShare><value>1.0</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_numeric_too_large_warns_and_drops() -> None:
    data = parse_form4(_NUMERIC_OVERFLOW_DOC)
    assert data.transaction_count == 1
    assert data.transactions[0].shares == Decimal("0")
    assert any(w.startswith("numeric_too_large:") for w in data.raw_warnings)


_NAMESPACED_DOC = b"""<?xml version="1.0"?>
<ns:ownershipDocument xmlns:ns="http://www.sec.gov/edgar/ownership">
    <ns:documentType>4</ns:documentType>
    <ns:issuer><ns:issuerCik>0000222222</ns:issuerCik></ns:issuer>
    <ns:reportingOwner/>
</ns:ownershipDocument>
"""


def test_parse_namespaced_root_is_tolerated() -> None:
    data = parse_form4(_NAMESPACED_DOC)
    assert data.issuer_cik == "0000222222"


# ---------------------------------------------------------------------------
# Structural failures → Form4ParseError
# ---------------------------------------------------------------------------


def test_parse_empty_bytes_raises() -> None:
    with pytest.raises(Form4ParseError) as excinfo:
        parse_form4(b"", accession_number="empty-acc")
    assert "empty document" in str(excinfo.value)
    assert excinfo.value.accession_number == "empty-acc"


def test_parse_non_bytes_raises() -> None:
    with pytest.raises(Form4ParseError):
        parse_form4("<not bytes/>")  # type: ignore[arg-type]


def test_parse_oversized_input_raises() -> None:
    big = b"<x>" + (b"a" * (MAX_INPUT_BYTES + 1)) + b"</x>"
    with pytest.raises(Form4ParseError) as excinfo:
        parse_form4(big)
    assert "exceeds maximum size" in str(excinfo.value)


def test_parse_malformed_xml_raises() -> None:
    with pytest.raises(Form4ParseError) as excinfo:
        parse_form4(b"<ownershipDocument><unclosed>")
    assert "malformed XML" in str(excinfo.value)


def test_parse_wrong_root_raises() -> None:
    with pytest.raises(Form4ParseError) as excinfo:
        parse_form4(b"<other-root><stuff/></other-root>")
    assert "expected root" in str(excinfo.value)


def test_parse_dtd_with_external_entity_is_refused() -> None:
    """defusedxml MUST refuse external entity declarations (XXE)."""
    payload = (
        b"<?xml version='1.0'?>"
        b"<!DOCTYPE ownershipDocument ["
        b"  <!ENTITY xxe SYSTEM 'file:///etc/passwd'>"
        b"]>"
        b"<ownershipDocument><issuer>&xxe;</issuer></ownershipDocument>"
    )
    with pytest.raises(Form4ParseError):
        parse_form4(payload)


def test_parse_billion_laughs_is_refused() -> None:
    """defusedxml MUST refuse exponential entity-expansion attacks."""
    payload = (
        b"<?xml version='1.0'?>"
        b"<!DOCTYPE lolz ["
        b"  <!ENTITY lol 'lol'>"
        b"  <!ENTITY lol2 '&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;'>"
        b"  <!ENTITY lol3 '&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;'>"
        b"]>"
        b"<ownershipDocument>&lol3;</ownershipDocument>"
    )
    with pytest.raises(Form4ParseError):
        parse_form4(payload)


# ---------------------------------------------------------------------------
# Form4Data / Form4Transaction surface
# ---------------------------------------------------------------------------


def test_to_dict_round_trip_is_json_serialisable() -> None:
    import json

    data = parse_form4(_read("form4_aapl.xml"))
    d = data.to_dict()
    blob = json.dumps(d)
    revived = json.loads(blob)
    assert revived["issuer_cik"] == "0000320193"
    assert revived["transaction_count"] == 3
    # Decimals serialise as strings.
    assert revived["net_sell_value"] == "17050000.00"
    # Dates serialise as ISO strings.
    assert revived["period_of_report"] == "2024-04-30"
    # Transactions are list-of-dicts in the JSON form.
    assert isinstance(revived["transactions"], list)
    assert revived["transactions"][0]["code"] == "S"


def test_form4_transaction_is_frozen() -> None:
    tx = Form4Transaction(
        transaction_date=date(2024, 1, 1),
        code="P",
        shares=Decimal("1"),
        price_per_share=Decimal("100"),
        direct_or_indirect="D",
        acquired_or_disposed="A",
        post_transaction_shares=Decimal("1"),
        is_derivative=False,
        security_title="Common Stock",
        derivative_security_title=None,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        tx.code = "S"  # type: ignore[misc]


def test_known_transaction_codes_includes_canonical_forms() -> None:
    # Spot-check a representative subset to guard against accidental edits.
    for code in ("P", "S", "A", "D", "F", "G", "M", "X"):
        assert code in KNOWN_TRANSACTION_CODES


# ---------------------------------------------------------------------------
# Transaction-cap behaviour
# ---------------------------------------------------------------------------


def test_transaction_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm the per-filing transaction ceiling caps + warns."""
    monkeypatch.setattr("sec_edgar_mcp._xbrl.MAX_TRANSACTIONS", 2)
    repeated_tx = (
        b"<nonDerivativeTransaction>"
        b"  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        b"  <transactionAmounts>"
        b"    <transactionShares><value>1</value></transactionShares>"
        b"    <transactionPricePerShare><value>1</value></transactionPricePerShare>"
        b"    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
        b"  </transactionAmounts>"
        b"  <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>"
        b"</nonDerivativeTransaction>"
    )
    payload = (
        b"<?xml version='1.0'?>"
        b"<ownershipDocument><documentType>4</documentType>"
        b"<issuer/><reportingOwner/>"
        b"<nonDerivativeTable>" + repeated_tx * 5 + b"</nonDerivativeTable>"
        b"</ownershipDocument>"
    )
    data = parse_form4(payload)
    assert data.transaction_count == 2
    assert any(w.startswith("transaction_cap_reached:") for w in data.raw_warnings)
