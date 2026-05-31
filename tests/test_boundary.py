"""Boundary-value security tests for sec-edgar-mcp.

Probes the edges of every numeric and string input: minimum, maximum, just
below/above the limit, empty, single-char, and max-length. Boundary handling
is where off-by-one validation bugs hide and where an attacker probes for an
unbounded fetch. No empty-coverage padding.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sec_edgar_mcp.models import (
    Get8KWithItemsInput,
    GetCompanyFilingsInput,
    GetFilingTextInput,
    GetForm4InsiderTradesInput,
    SearchFilingsFullTextInput,
)

# ===========================================================================
# limit boundaries (ge=1, le=200)
# ===========================================================================


class TestLimitBoundaries:
    def test_limit_min_accepted(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=1).limit == 1

    def test_limit_max_accepted(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=200).limit == 200

    def test_limit_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=0)

    def test_limit_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=201)

    def test_limit_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=-1)


# ===========================================================================
# since_days boundaries (form4 ge=1 le=365; 8-K ge=1 le=3650)
# ===========================================================================


class TestSinceDaysBoundaries:
    def test_form4_min_max(self) -> None:
        assert GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=1).since_days == 1
        assert GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=365).since_days == 365

    def test_form4_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=366)

    def test_form4_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=0)

    def test_8k_max_3650(self) -> None:
        assert Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=3650).since_days == 3650

    def test_8k_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=3651)


# ===========================================================================
# CIK / ticker length boundaries (1-10 chars)
# ===========================================================================


class TestCikTickerBoundaries:
    def test_single_digit_cik(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="1").cik_or_ticker == "1"

    def test_ten_digit_cik(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="1234567890").cik_or_ticker == "1234567890"

    def test_eleven_digit_cik_rejected(self) -> None:
        from sec_edgar_mcp.errors import SecValidationError

        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="12345678901")

    def test_empty_cik_rejected(self) -> None:
        from sec_edgar_mcp.errors import SecValidationError

        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="")

    def test_single_char_ticker(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="F").cik_or_ticker == "F"

    def test_max_len_ticker(self) -> None:
        # 'A' + 9 more = 10 chars, matches TICKER_RE.
        assert GetCompanyFilingsInput(cik_or_ticker="ABCDEFGHIJ").cik_or_ticker == "ABCDEFGHIJ"

    def test_overlength_ticker_rejected(self) -> None:
        from sec_edgar_mcp.errors import SecValidationError

        with pytest.raises((ValidationError, SecValidationError)):
            GetCompanyFilingsInput(cik_or_ticker="ABCDEFGHIJK")  # 11 chars

    def test_ticker_lowercase_is_upcased(self) -> None:
        assert GetCompanyFilingsInput(cik_or_ticker="aapl").cik_or_ticker == "AAPL"


# ===========================================================================
# accession number boundaries (exact 20 chars, 10-2-6 digit shape)
# ===========================================================================


class TestAccessionBoundaries:
    def test_exact_format_accepted(self) -> None:
        a = GetFilingTextInput(accession_number="0000320193-24-000123")
        assert a.accession_number == "0000320193-24-000123"

    def test_too_short_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="0000320193-24-00012")

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="0000320193-24-0001234")

    def test_wrong_separators_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetFilingTextInput(accession_number="0000320193/24/000123")


# ===========================================================================
# search query boundaries (1-200 chars)
# ===========================================================================


class TestSearchQueryBoundaries:
    def test_single_char_query(self) -> None:
        assert SearchFilingsFullTextInput(query="a").query == "a"

    def test_max_len_query(self) -> None:
        q = "x" * 200
        assert SearchFilingsFullTextInput(query=q).query == q

    def test_overlength_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="x" * 201)

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SearchFilingsFullTextInput(query="")

    def test_since_days_min(self) -> None:
        assert SearchFilingsFullTextInput(query="q", since_days=1).since_days == 1


# ===========================================================================
# item code boundaries (regex \d{1,2}\.\d{1,2}, list max 20)
# ===========================================================================


class TestItemCodeBoundaries:
    def test_valid_item_codes(self) -> None:
        codes = Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["1.01", "5.02", "9.99"]).item_codes
        assert codes == ["1.01", "5.02", "9.99"]

    def test_malformed_item_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["101"])

    def test_item_code_list_max(self) -> None:
        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["1.01"] * 50)


# ===========================================================================
# extra-field rejection (extra="forbid")
# ===========================================================================


class TestExtraFieldRejection:
    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", evil_field="x")  # type: ignore[call-arg]
