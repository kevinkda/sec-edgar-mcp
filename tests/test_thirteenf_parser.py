"""Unit + boundary + exception tests for the 13F information-table parser.

Covers :mod:`sec_edgar_mcp._thirteenf`:

* happy-path parse of a real-shaped namespaced information table;
* boundary cases (empty doc, oversized doc, holding cap, numeric ceilings);
* tolerant field handling (blank values, unknown codes, malformed numbers);
* structural failures raising :class:`ThirteenFParseError`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sec_edgar_mcp._thirteenf import (
    MAX_HOLDINGS,
    MAX_INPUT_BYTES,
    MAX_NUMERIC_DIGITS,
    ThirteenFData,
    ThirteenFHolding,
    parse_13f,
)
from sec_edgar_mcp.errors import ThirteenFParseError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "seed"


def _load_info_table() -> bytes:
    return (FIXTURE_DIR / "form13f_info_table.xml").read_bytes()


# ===========================================================================
# Happy path
# ===========================================================================


def test_parse_three_holdings() -> None:
    data = parse_13f(_load_info_table(), accession_number="0001067983-24-000020")
    assert isinstance(data, ThirteenFData)
    assert data.holding_count == 3
    assert data.accession_number == "0001067983-24-000020"
    aapl, msft, treasury = data.holdings
    assert aapl.name_of_issuer == "APPLE INC"
    assert aapl.cusip == "037833100"
    assert aapl.value == Decimal("1500000")
    assert aapl.shares_or_principal_type == "SH"
    assert aapl.investment_discretion == "SOLE"
    assert msft.put_call == "Call"
    assert msft.other_manager == "2"
    assert treasury.shares_or_principal_type == "PRN"


def test_totals_value_and_shares() -> None:
    data = parse_13f(_load_info_table())
    # total value sums every row's raw value.
    assert data.total_value == Decimal("4250000")
    # total shares only sums SH-type rows (10000 + 5000), PRN excluded.
    assert data.total_shares == Decimal("15000")


def test_value_in_thousands_flag_roundtrips() -> None:
    assert parse_13f(_load_info_table(), value_in_thousands=True).value_reported_in_thousands is True
    assert parse_13f(_load_info_table(), value_in_thousands=False).value_reported_in_thousands is False


def test_to_dict_serialises_decimals_to_strings() -> None:
    out = parse_13f(_load_info_table()).to_dict()
    assert out["total_value"] == "4250000"
    first = out["holdings"][0]
    assert first["value"] == "1500000"
    assert isinstance(first["voting_authority_sole"], str)


def test_holding_to_dict_direct() -> None:
    h = ThirteenFHolding(
        name_of_issuer="X",
        title_of_class="COM",
        cusip="000000000",
        value=Decimal("1"),
        shares_or_principal_amount=Decimal("2"),
        shares_or_principal_type="SH",
        put_call="",
        investment_discretion="SOLE",
        other_manager="",
        voting_authority_sole=Decimal("2"),
        voting_authority_shared=Decimal("0"),
        voting_authority_none=Decimal("0"),
    )
    assert h.to_dict()["value"] == "1"


# ===========================================================================
# Structural failures → ThirteenFParseError
# ===========================================================================


def test_non_bytes_raises() -> None:
    with pytest.raises(ThirteenFParseError) as ei:
        parse_13f("not bytes")  # type: ignore[arg-type]
    assert "must be bytes" in ei.value.reason


def test_empty_doc_raises() -> None:
    with pytest.raises(ThirteenFParseError) as ei:
        parse_13f(b"")
    assert ei.value.reason == "empty document"


def test_oversized_doc_raises() -> None:
    payload = b"<informationTable>" + b"x" * (MAX_INPUT_BYTES + 1)
    with pytest.raises(ThirteenFParseError) as ei:
        parse_13f(payload)
    assert "exceeds maximum size" in ei.value.reason


def test_malformed_xml_raises() -> None:
    with pytest.raises(ThirteenFParseError) as ei:
        parse_13f(b"<informationTable><infoTable>")
    assert "malformed XML" in ei.value.reason


def test_wrong_root_raises() -> None:
    with pytest.raises(ThirteenFParseError) as ei:
        parse_13f(b"<ownershipDocument></ownershipDocument>")
    assert "expected root <informationTable>" in ei.value.reason


def test_bytearray_accepted() -> None:
    data = parse_13f(bytearray(_load_info_table()))
    assert data.holding_count == 3


# ===========================================================================
# Tolerant field handling
# ===========================================================================


def test_blank_and_missing_fields_tolerated() -> None:
    xml = b"""<informationTable>
        <infoTable>
            <nameOfIssuer></nameOfIssuer>
            <value></value>
            <shrsOrPrnAmt><sshPrnamt></sshPrnamt></shrsOrPrnAmt>
        </infoTable>
    </informationTable>"""
    data = parse_13f(xml)
    assert data.holding_count == 1
    h = data.holdings[0]
    assert h.name_of_issuer == ""
    assert h.value == Decimal("0")
    assert h.shares_or_principal_amount == Decimal("0")
    assert h.shares_or_principal_type == ""


def test_unknown_codes_warn_but_parse() -> None:
    xml = b"""<informationTable>
        <infoTable>
            <nameOfIssuer>FOO</nameOfIssuer>
            <value>100</value>
            <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>ZZ</sshPrnamtType></shrsOrPrnAmt>
            <putCall>Sideways</putCall>
            <investmentDiscretion>WAT</investmentDiscretion>
        </infoTable>
    </informationTable>"""
    data = parse_13f(xml)
    h = data.holdings[0]
    assert h.shares_or_principal_type == ""
    assert h.put_call == ""
    assert h.investment_discretion == "WAT"
    assert any("unknown_ssh_prn_type:ZZ" in w for w in data.raw_warnings)
    assert any("unknown_put_call:Sideways" in w for w in data.raw_warnings)
    assert any("unknown_discretion:WAT" in w for w in data.raw_warnings)


def test_put_value_normalised_capitalised() -> None:
    xml = b"""<informationTable>
        <infoTable>
            <nameOfIssuer>FOO</nameOfIssuer><value>1</value>
            <putCall>PUT</putCall>
        </infoTable>
    </informationTable>"""
    assert parse_13f(xml).holdings[0].put_call == "Put"


def test_non_infotable_children_skipped() -> None:
    xml = b"""<informationTable>
        <comment>ignore me</comment>
        <infoTable><nameOfIssuer>FOO</nameOfIssuer><value>1</value></infoTable>
    </informationTable>"""
    assert parse_13f(xml).holding_count == 1


def test_unparseable_decimal_warns() -> None:
    xml = b"""<informationTable>
        <infoTable><nameOfIssuer>FOO</nameOfIssuer><value>abc</value></infoTable>
    </informationTable>"""
    data = parse_13f(xml)
    assert data.holdings[0].value == Decimal("0")
    assert any("unparseable_decimal:value" in w for w in data.raw_warnings)


def test_numeric_too_long_warns() -> None:
    big = b"9" * (MAX_NUMERIC_DIGITS + 1)
    xml = b"<informationTable><infoTable><value>" + big + b"</value></infoTable></informationTable>"
    data = parse_13f(xml)
    assert data.holdings[0].value == Decimal("0")
    assert any("numeric_too_large:value" in w for w in data.raw_warnings)


def test_numeric_exponent_too_large_warns() -> None:
    xml = b"<informationTable><infoTable><value>1E40</value></infoTable></informationTable>"
    data = parse_13f(xml)
    assert data.holdings[0].value == Decimal("0")
    assert any("numeric_exponent_too_large:value" in w for w in data.raw_warnings)


def test_comma_grouped_numbers_parsed() -> None:
    xml = b"<informationTable><infoTable><value>1,234</value></infoTable></informationTable>"
    assert parse_13f(xml).holdings[0].value == Decimal("1234")


# ===========================================================================
# Boundary — holding cap
# ===========================================================================


def test_holding_cap_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sec_edgar_mcp._thirteenf.MAX_HOLDINGS", 2)
    rows = b"".join(b"<infoTable><nameOfIssuer>X</nameOfIssuer><value>1</value></infoTable>" for _ in range(5))
    data = parse_13f(b"<informationTable>" + rows + b"</informationTable>")
    assert data.holding_count == 2
    assert any("holding_cap_reached:2" in w for w in data.raw_warnings)


def test_max_holdings_constant_sane() -> None:
    assert MAX_HOLDINGS >= 1000


def test_empty_information_table() -> None:
    data = parse_13f(b"<informationTable></informationTable>")
    assert data.holding_count == 0
    assert data.total_value == Decimal("0")
    assert data.total_shares == Decimal("0")
