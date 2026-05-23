"""Unit tests for sec_edgar_mcp.models — Pydantic v2 input schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sec_edgar_mcp.errors import SecValidationError
from sec_edgar_mcp.models import (
    ALLOWED_FORM_TYPES,
    Get8KWithItemsInput,
    GetCompanyFilingsInput,
    GetFilingTextInput,
    GetForm4InsiderTradesInput,
    SearchFilingsFullTextInput,
    supported_tool_names,
)


class TestGetCompanyFilingsInput:
    def test_ticker_uppercased(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="aapl")
        assert v.cik_or_ticker == "AAPL"

    def test_cik_passes(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="0000320193")
        assert v.cik_or_ticker == "0000320193"

    def test_short_cik_ok(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="320193")
        assert v.cik_or_ticker == "320193"

    def test_garbage_rejected(self) -> None:
        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="aap!l")

    def test_too_long_rejected(self) -> None:
        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="A" * 11)

    def test_empty_rejected(self) -> None:
        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="")

    def test_default_limit(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="AAPL")
        assert v.limit == 20
        assert v.form_types is None

    def test_limit_bounds(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=0)
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=201)

    def test_form_types_allowlist(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="AAPL", form_types=["10-K", "10-Q"])
        assert v.form_types == ["10-K", "10-Q"]

    def test_form_types_lowercase_normalised(self) -> None:
        v = GetCompanyFilingsInput(cik_or_ticker="AAPL", form_types=["10-k"])
        assert v.form_types == ["10-K"]

    def test_form_types_unknown_rejected(self) -> None:
        with pytest.raises(SecValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", form_types=["BOGUS"])

    def test_form_types_max_length(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(
                cik_or_ticker="AAPL",
                form_types=["10-K"] * 21,
            )

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", extra="x")  # type: ignore[call-arg]


class TestGetForm4InsiderTradesInput:
    def test_default_since_days(self) -> None:
        v = GetForm4InsiderTradesInput(cik_or_ticker="MSFT")
        assert v.since_days == 30

    def test_since_days_bounds(self) -> None:
        with pytest.raises(ValidationError):
            GetForm4InsiderTradesInput(cik_or_ticker="MSFT", since_days=0)
        with pytest.raises(ValidationError):
            GetForm4InsiderTradesInput(cik_or_ticker="MSFT", since_days=366)

    def test_garbage_ticker_rejected(self) -> None:
        with pytest.raises((ValidationError, SecValidationError)):
            GetForm4InsiderTradesInput(cik_or_ticker="!!!")


class TestGetFilingTextInput:
    def test_canonical_accession(self) -> None:
        v = GetFilingTextInput(accession_number="0000320193-24-000123")
        assert v.accession_number == "0000320193-24-000123"
        assert v.document_type == "primary"

    def test_complete_doctype(self) -> None:
        v = GetFilingTextInput(
            accession_number="0000320193-24-000123",
            document_type="complete",
        )
        assert v.document_type == "complete"

    def test_doctype_invalid_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetFilingTextInput(
                accession_number="0000320193-24-000123",
                document_type="bogus",  # type: ignore[arg-type]
            )

    def test_accession_format_strict(self) -> None:
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="000032019324000123")
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="bogus")
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="0000320193-99-99")


class TestSearchFilingsFullTextInput:
    def test_minimal(self) -> None:
        v = SearchFilingsFullTextInput(query="cybersecurity")
        assert v.query == "cybersecurity"
        assert v.since_days == 90
        assert v.form_types is None

    def test_query_strip(self) -> None:
        v = SearchFilingsFullTextInput(query="  fraud  ")
        assert v.query == "fraud"

    def test_query_too_long(self) -> None:
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="x" * 201)

    def test_query_empty(self) -> None:
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="")

    def test_since_days_bounds(self) -> None:
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="x", since_days=0)
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="x", since_days=3651)

    def test_form_types_validated(self) -> None:
        with pytest.raises(SecValidationError):
            SearchFilingsFullTextInput(query="x", form_types=["BOGUS"])


def test_supported_tool_names_stable() -> None:
    names = supported_tool_names()
    assert "get_company_filings" in names
    assert "get_form4_insider_trades" in names
    assert "get_filing_text" in names
    assert "search_filings_full_text" in names
    assert "get_8k_with_items" in names
    assert "health_check" in names
    assert "get_server_info" in names
    assert len(set(names)) == len(names)


class TestGet8KWithItemsInput:
    def test_minimal(self) -> None:
        v = Get8KWithItemsInput(cik_or_ticker="AAPL")
        assert v.cik_or_ticker == "AAPL"
        assert v.item_codes is None
        assert v.since_days == 30
        assert v.limit == 50

    def test_ticker_uppercased(self) -> None:
        v = Get8KWithItemsInput(cik_or_ticker="aapl", item_codes=["1.01", "5.02"])
        assert v.cik_or_ticker == "AAPL"
        assert v.item_codes == ["1.01", "5.02"]

    def test_garbage_ticker_rejected(self) -> None:
        with pytest.raises((ValidationError, SecValidationError)):
            Get8KWithItemsInput(cik_or_ticker="!!!")

    def test_item_code_format_strict(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["abc"])
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["10101"])
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["1.01.01"])

    def test_item_codes_strip(self) -> None:
        v = Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["  1.01 "])
        assert v.item_codes == ["1.01"]

    def test_item_codes_max_length(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["1.01"] * 21)

    def test_since_days_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=0)
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=3651)

    def test_limit_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", limit=0)
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", limit=201)


def test_allowed_form_types_includes_essentials() -> None:
    for f in ("10-K", "10-Q", "8-K", "S-1", "4", "13F-HR", "DEF 14A"):
        assert f in ALLOWED_FORM_TYPES


def test_models_are_frozen() -> None:
    v = GetCompanyFilingsInput(cik_or_ticker="AAPL")
    with pytest.raises(ValidationError):
        v.limit = 5  # type: ignore[misc]
