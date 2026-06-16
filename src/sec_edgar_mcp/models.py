"""Pydantic v2 input schemas for every outward-facing tool.

The 4 business tools accept either a CIK (numeric, 1-10 digits zero-padded)
or a US-style stock ticker.  We validate ticker input strictly because SEC
EDGAR's ticker→CIK lookup is silently case-folded but otherwise unforgiving
of stray characters.

Coverage target: **100 %**.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# ---------------------------------------------------------------------------
# Regexes — anchored to prevent partial-match Pydantic search semantics.
# ---------------------------------------------------------------------------

#: 1-10 digit CIK (SEC zero-pads to 10, but accepts shorter forms).
CIK_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,10}$")

#: US stock ticker — uppercase letters, dot, dash, slash; 1-10 chars.
TICKER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,9}$")

#: SEC accession number canonical form, e.g. "0000320193-24-000123".
ACCESSION_RE: Final[re.Pattern[str]] = re.compile(r"^\d{10}-\d{2}-\d{6}$")

#: Combined CIK-or-ticker validator (we re-validate downstream because we
#: want ticker→CIK lookup to drive the single source of truth, but the
#: Pydantic gate rejects obvious garbage early).
CIK_OR_TICKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:\d{1,10}|[A-Z][A-Z0-9.\-/]{0,9})$",
)


# ---------------------------------------------------------------------------
# Form-type allowlist (SEC publishes hundreds; we expose the common ones).
# ---------------------------------------------------------------------------

#: Subset of SEC form codes considered safe to pass through.  This list is
#: descriptive (not normative) — operators can extend it via env var if they
#: need a niche form like NT 10-K.
ALLOWED_FORM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "8-K",
        "8-K/A",
        "20-F",
        "20-F/A",
        "S-1",
        "S-1/A",
        "S-3",
        "S-3/A",
        "S-4",
        "S-4/A",
        "DEF 14A",
        "DEFA14A",
        "PRE 14A",
        "424B1",
        "424B2",
        "424B3",
        "424B4",
        "424B5",
        "3",
        "3/A",
        "4",
        "4/A",
        "5",
        "5/A",
        "13F-HR",
        "13F-HR/A",
        "13F-NT",
        "SC 13D",
        "SC 13D/A",
        "SC 13G",
        "SC 13G/A",
        "6-K",
        "6-K/A",
        "11-K",
        "11-K/A",
        "F-1",
        "F-1/A",
        "F-3",
        "F-3/A",
        "F-4",
        "F-4/A",
        "144",
        "CORRESP",
        "EFFECT",
        "RW",
        "UPLOAD",
    },
)


DocumentType = Literal["primary", "complete"]


# ---------------------------------------------------------------------------
# Constrained string types
# ---------------------------------------------------------------------------

CikOrTicker = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=10,
    ),
]

AccessionNumber = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=20,
        max_length=20,
        pattern=ACCESSION_RE.pattern,
    ),
]

SearchQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=200,
    ),
]

FormType = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=20,
    ),
]


# ---------------------------------------------------------------------------
# Base — strict-by-default mixin
# ---------------------------------------------------------------------------


class _BaseInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


# ---------------------------------------------------------------------------
# Concrete schemas — one per tool.
# ---------------------------------------------------------------------------


class GetCompanyFilingsInput(_BaseInput):
    """Input for ``get_company_filings``."""

    cik_or_ticker: CikOrTicker
    form_types: list[FormType] | None = Field(default=None, max_length=20)
    limit: int = Field(default=20, ge=1, le=200)

    @field_validator("cik_or_ticker", mode="before")
    @classmethod
    def _upper_cik_or_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="cik_or_ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v

    @field_validator("form_types", mode="before")
    @classmethod
    def _upper_form_types(cls, v: object) -> object:
        if isinstance(v, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in v]
        return v

    @field_validator("form_types")
    @classmethod
    def _validate_form_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        bad = [f for f in v if f not in ALLOWED_FORM_TYPES]
        if bad:
            from .errors import SecValidationError

            raise SecValidationError(
                field="form_types",
                reason=f"unsupported form types: {bad}; allowed subset in ALLOWED_FORM_TYPES",
            )
        return v


class GetForm4InsiderTradesInput(_BaseInput):
    """Input for ``get_form4_insider_trades``."""

    cik_or_ticker: CikOrTicker
    since_days: int = Field(default=30, ge=1, le=365)

    @field_validator("cik_or_ticker", mode="before")
    @classmethod
    def _upper_cik_or_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="cik_or_ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v


#: SEC 8-K item code regex (e.g. ``"1.01"``, ``"5.02"``, ``"9.01"``).
ITEM_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,2}\.\d{1,2}$")


ItemCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=6,
        pattern=ITEM_CODE_RE.pattern,
    ),
]


class Get8KWithItemsInput(_BaseInput):
    """Input for ``get_8k_with_items``.

    Filters the issuer's 8-K filings (and 8-K/A amendments) by the SEC
    item codes the filing reports.  Common codes:

    * 1.01 - Entry into a Material Definitive Agreement
    * 2.02 - Results of Operations and Financial Condition
    * 5.02 - Departure / Election of Directors / Officers
    * 7.01 - Regulation FD Disclosure
    * 9.01 - Financial Statements and Exhibits
    """

    cik_or_ticker: CikOrTicker
    item_codes: list[ItemCode] | None = Field(default=None, max_length=20)
    since_days: int = Field(default=30, ge=1, le=3650)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("cik_or_ticker", mode="before")
    @classmethod
    def _upper_cik_or_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="cik_or_ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v

    @field_validator("item_codes", mode="before")
    @classmethod
    def _strip_item_codes(cls, v: object) -> object:
        if isinstance(v, list):
            return [item.strip() if isinstance(item, str) else item for item in v]
        return v


#: SEC fiscal quarter token, e.g. ``"2024Q3"``.  Year 1993 is the first
#: year EDGAR holds electronic 13F filings; we cap at 2099 to keep the
#: regex bounded.
QUARTER_RE: Final[re.Pattern[str]] = re.compile(r"^(?:19|20)\d{2}Q[1-4]$")


Quarter = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_upper=True,
        min_length=6,
        max_length=6,
        pattern=QUARTER_RE.pattern,
    ),
]


class Get13FHoldingsInput(_BaseInput):
    """Input for ``get_13f_holdings``.

    ``cik_or_ticker`` identifies the institutional **manager** (the 13F
    filer, e.g. Berkshire Hathaway CIK 1067983), not a held company.
    ``quarter`` optionally pins a specific reporting period (``YYYYQN``);
    when omitted the most-recent 13F-HR in the submissions window is used.
    """

    cik_or_ticker: CikOrTicker
    quarter: Quarter | None = None

    @field_validator("quarter", mode="before")
    @classmethod
    def _upper_quarter(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    @field_validator("cik_or_ticker", mode="before")
    @classmethod
    def _upper_cik_or_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="cik_or_ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v


class GetInstitutionalHoldersInput(_BaseInput):
    """Input for ``get_institutional_holders``.

    ``ticker`` identifies the **held company** — we reverse-look-up which
    13F managers report a position in it by full-text-searching recent
    13F-HR filings.  ``since_days`` bounds the search window; ``limit``
    caps the number of distinct holders returned.
    """

    ticker: CikOrTicker
    since_days: int = Field(default=120, ge=1, le=550)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("ticker", mode="before")
    @classmethod
    def _upper_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v


class GetProxyStatementInput(_BaseInput):
    """Input for ``get_proxy_statement`` (DEF 14A)."""

    cik_or_ticker: CikOrTicker

    @field_validator("cik_or_ticker", mode="before")
    @classmethod
    def _upper_cik_or_ticker(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().upper()
            if not CIK_OR_TICKER_RE.match(v):
                from .errors import SecValidationError

                raise SecValidationError(
                    field="cik_or_ticker",
                    reason=f"must match {CIK_OR_TICKER_RE.pattern}",
                )
        return v


class GetFilingTextInput(_BaseInput):
    """Input for ``get_filing_text``."""

    accession_number: AccessionNumber
    document_type: DocumentType = "primary"


class SearchFilingsFullTextInput(_BaseInput):
    """Input for ``search_filings_full_text``."""

    query: SearchQuery
    form_types: list[FormType] | None = Field(default=None, max_length=20)
    since_days: int = Field(default=90, ge=1, le=3650)

    @field_validator("form_types", mode="before")
    @classmethod
    def _upper_form_types(cls, v: object) -> object:
        if isinstance(v, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in v]
        return v

    @field_validator("form_types")
    @classmethod
    def _validate_form_types(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        bad = [f for f in v if f not in ALLOWED_FORM_TYPES]
        if bad:
            from .errors import SecValidationError

            raise SecValidationError(
                field="form_types",
                reason=f"unsupported form types: {bad}",
            )
        return v


class HealthCheckInput(_BaseInput):
    """Input for ``health_check`` — empty."""


class GetServerInfoInput(_BaseInput):
    """Input for ``get_server_info`` — empty."""


# ---------------------------------------------------------------------------
# Tool registry — lets ``get_server_info`` enumerate tools without importing
# the server module (avoids a circular import in __init__).
# ---------------------------------------------------------------------------

_SUPPORTED_TOOLS: Final[tuple[str, ...]] = (
    "get_company_filings",
    "get_form4_insider_trades",
    "get_filing_text",
    "search_filings_full_text",
    "get_8k_with_items",
    "get_13f_holdings",
    "get_institutional_holders",
    "get_proxy_statement",
    "health_check",
    "get_server_info",
)


def supported_tool_names() -> list[str]:
    """Stable list of tool names the server exposes."""
    return list(_SUPPORTED_TOOLS)


__all__ = [
    "ACCESSION_RE",
    "ALLOWED_FORM_TYPES",
    "CIK_OR_TICKER_RE",
    "CIK_RE",
    "ITEM_CODE_RE",
    "QUARTER_RE",
    "TICKER_RE",
    "AccessionNumber",
    "CikOrTicker",
    "DocumentType",
    "FormType",
    "Get8KWithItemsInput",
    "Get13FHoldingsInput",
    "GetCompanyFilingsInput",
    "GetFilingTextInput",
    "GetForm4InsiderTradesInput",
    "GetInstitutionalHoldersInput",
    "GetProxyStatementInput",
    "GetServerInfoInput",
    "HealthCheckInput",
    "ItemCode",
    "Quarter",
    "SearchFilingsFullTextInput",
    "SearchQuery",
    "supported_tool_names",
]
