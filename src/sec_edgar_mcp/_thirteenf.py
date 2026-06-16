"""Defused parser for SEC Form 13F-HR information tables.

Form 13F is the quarterly disclosure that institutional investment
managers exercising investment discretion over **$100 million or more**
in Section 13(f) securities must file within 45 days of each calendar
quarter end (Securities Exchange Act § 13(f)).  The machine-readable
*information table* is published as a standalone XML document rooted at
``<informationTable>`` (namespace
``http://www.sec.gov/edgar/document/thirteenf/informationtable``) with
one ``<infoTable>`` row per holding.

Each row carries:

* ``nameOfIssuer`` / ``titleOfClass`` / ``cusip`` — security identity;
* ``value`` — market value.  Pre-2023Q3 filings report **thousands of
  dollars**; from 2023Q3 the SEC switched the units to **whole dollars**.
  We cannot disambiguate from the row alone, so we surface the raw value
  and let the caller normalise via :data:`VALUE_REPORTED_IN_THOUSANDS`;
* ``shrsOrPrnAmt/sshPrnamt`` + ``sshPrnamtType`` — share / principal
  amount and its type (``SH`` shares, ``PRN`` principal);
* ``investmentDiscretion`` — ``SOLE`` / ``DFND`` / ``OTR``;
* ``votingAuthority`` — sole / shared / none vote counts;
* ``putCall`` — optional ``Put`` / ``Call`` for option positions.

Security guarantees
-------------------

We reuse the exact :mod:`defusedxml.ElementTree` posture proven in
:mod:`sec_edgar_mcp._xbrl` (R8): external entity references (XXE) are
refused, entity-expansion depth is capped (billion-laughs), DTD parsing
is disabled, and external general/parameter entities are refused.  On
top of defusedxml's defaults we impose process-local ceilings:

* ``MAX_INPUT_BYTES = 16 MiB`` — large funds (e.g. Vanguard, BlackRock)
  publish information tables in the multi-MiB range; 16 MiB protects us
  from a maliciously oversized payload before the XML parser starts.
* ``MAX_HOLDINGS = 50_000`` — caps the materialised row count so a
  pathological filing cannot blow the MCP frame budget.
* ``MAX_NUMERIC_DIGITS = 30`` — refuses to materialise pathologically
  large numbers via ``Decimal``.

Tolerant parsing
----------------

13F filings come from thousands of managers with a long tail of edge
cases (blank values, missing optional fields, unknown discretion
codes).  We **never raise** on a missing or malformed *field* — we set
the slot to ``None`` / ``0`` and append a structured warning to
``ThirteenFData.raw_warnings``.  Only a structurally broken document
(unparseable XML, wrong root element, ``DefusedXmlException``) raises
:class:`ThirteenFParseError`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal, DecimalException
from typing import Any, Final, Literal
from xml.etree.ElementTree import ParseError as ETParseError

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from .errors import ThirteenFParseError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration & limits
# ---------------------------------------------------------------------------

#: Hard byte ceiling on a single 13F information-table XML body.
MAX_INPUT_BYTES: Final[int] = 16 * 1024 * 1024

#: Cap on the number of holdings we ingest per filing.
MAX_HOLDINGS: Final[int] = 50_000

#: Reject Decimal/int strings longer than this many characters before parsing.
MAX_NUMERIC_DIGITS: Final[int] = 30

#: Known SEC investment-discretion codes (13F information table).
KNOWN_DISCRETION_CODES: Final[frozenset[str]] = frozenset({"SOLE", "DFND", "OTR"})

#: Known share/principal-amount type codes.
KNOWN_SSH_PRN_TYPES: Final[frozenset[str]] = frozenset({"SH", "PRN"})

#: Known put/call codes (case-insensitive on the wire).
KNOWN_PUT_CALL: Final[frozenset[str]] = frozenset({"PUT", "CALL"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThirteenFHolding:
    """A single 13F information-table row (one reported position)."""

    name_of_issuer: str
    title_of_class: str
    cusip: str
    value: Decimal  # raw reported value (thousands pre-2023Q3, dollars after)
    shares_or_principal_amount: Decimal
    shares_or_principal_type: Literal["SH", "PRN", ""]
    put_call: Literal["Put", "Call", ""]
    investment_discretion: str
    other_manager: str
    voting_authority_sole: Decimal
    voting_authority_shared: Decimal
    voting_authority_none: Decimal

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serialisable dict (Decimals → strings)."""
        return {k: _jsonable(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class ThirteenFData:
    """Structured 13F information-table payload returned by :func:`parse_13f`."""

    accession_number: str
    holdings: tuple[ThirteenFHolding, ...]
    holding_count: int
    total_value: Decimal  # sum of raw ``value`` across all rows
    total_shares: Decimal  # sum of share-type rows only (PRN excluded)
    value_reported_in_thousands: bool
    raw_warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serialisable dict (Decimals → strings)."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            out[key] = _jsonable(value)
        return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_13f(
    xml_bytes: bytes,
    *,
    accession_number: str = "",
    value_in_thousands: bool = True,
) -> ThirteenFData:
    """Parse a 13F information-table XML body into a :class:`ThirteenFData`.

    ``value_in_thousands`` records the caller's unit assumption for the
    reported ``value`` column (SEC switched from thousands to whole
    dollars in 2023Q3).  It does not change the parsed numbers — it is
    surfaced verbatim so downstream tooling can normalise.

    Raises :class:`ThirteenFParseError` only for *structural* failures
    (unparseable XML, wrong root element, defusedxml refusal).  Field-level
    issues are tolerated and surfaced via ``raw_warnings``.
    """
    if not isinstance(xml_bytes, (bytes, bytearray)):
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason=f"input must be bytes, got {type(xml_bytes).__name__}",
        )
    if len(xml_bytes) == 0:
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason="empty document",
        )
    if len(xml_bytes) > MAX_INPUT_BYTES:
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason=f"document exceeds maximum size of {MAX_INPUT_BYTES} bytes",
        )

    try:
        root = DET.fromstring(bytes(xml_bytes))
    except DefusedXmlException as exc:
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason=f"defused XML rejected document: {type(exc).__name__}",
        ) from exc
    except ETParseError as exc:
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason=f"malformed XML: {exc}",
        ) from exc

    if _localname(root.tag) != "informationTable":
        raise ThirteenFParseError(
            accession_number=accession_number,
            reason=f"expected root <informationTable>, got <{_localname(root.tag)}>",
        )

    warnings: list[str] = []
    holdings: list[ThirteenFHolding] = []
    total_value = Decimal("0")
    total_shares = Decimal("0")

    for elem in root:
        if _localname(elem.tag) != "infoTable":
            continue
        if len(holdings) >= MAX_HOLDINGS:
            warnings.append(f"holding_cap_reached:{MAX_HOLDINGS}")
            break
        holding = _parse_info_table(elem, warnings)
        holdings.append(holding)
        total_value = _safe_add(total_value, holding.value, warnings)
        if holding.shares_or_principal_type == "SH":
            total_shares = _safe_add(total_shares, holding.shares_or_principal_amount, warnings)

    return ThirteenFData(
        accession_number=accession_number,
        holdings=tuple(holdings),
        holding_count=len(holdings),
        total_value=total_value,
        total_shares=total_shares,
        value_reported_in_thousands=value_in_thousands,
        raw_warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Per-row parsing
# ---------------------------------------------------------------------------


def _parse_info_table(elem: Any, warnings: list[str]) -> ThirteenFHolding:
    name_of_issuer = _text(elem, "nameOfIssuer") or ""
    title_of_class = _text(elem, "titleOfClass") or ""
    cusip = (_text(elem, "cusip") or "").strip().upper()

    value = _parse_decimal(_text(elem, "value"), warnings, "value")

    shrs = _find_child(elem, "shrsOrPrnAmt")
    shares = _parse_decimal(_text(shrs, "sshPrnamt"), warnings, "sshPrnamt")
    ssh_type_raw = (_text(shrs, "sshPrnamtType") or "").strip().upper()
    if ssh_type_raw not in KNOWN_SSH_PRN_TYPES and ssh_type_raw != "":
        warnings.append(f"unknown_ssh_prn_type:{ssh_type_raw}")
        ssh_type_raw = ""

    put_call_raw = (_text(elem, "putCall") or "").strip()
    put_call_norm: str = ""
    if put_call_raw:
        if put_call_raw.upper() in KNOWN_PUT_CALL:
            put_call_norm = put_call_raw.capitalize()
        else:
            warnings.append(f"unknown_put_call:{put_call_raw}")

    discretion = (_text(elem, "investmentDiscretion") or "").strip().upper()
    if discretion and discretion not in KNOWN_DISCRETION_CODES:
        warnings.append(f"unknown_discretion:{discretion}")

    other_manager = (_text(elem, "otherManager") or "").strip()

    voting = _find_child(elem, "votingAuthority")
    vote_sole = _parse_decimal(_text(voting, "Sole"), warnings, "votingAuthority.Sole")
    vote_shared = _parse_decimal(_text(voting, "Shared"), warnings, "votingAuthority.Shared")
    vote_none = _parse_decimal(_text(voting, "None"), warnings, "votingAuthority.None")

    return ThirteenFHolding(
        name_of_issuer=name_of_issuer,
        title_of_class=title_of_class,
        cusip=cusip,
        value=value,
        shares_or_principal_amount=shares,
        shares_or_principal_type=ssh_type_raw,  # type: ignore[arg-type]
        put_call=put_call_norm,  # type: ignore[arg-type]
        investment_discretion=discretion,
        other_manager=other_manager,
        voting_authority_sole=vote_sole,
        voting_authority_shared=vote_shared,
        voting_authority_none=vote_none,
    )


def _safe_add(running: Decimal, value: Decimal, warnings: list[str]) -> Decimal:
    """Fold *value* into *running*, tolerating Decimal overflow."""
    try:
        return running + value
    except DecimalException:  # pragma: no cover - extreme overflow accumulation
        warnings.append("total_overflow")
        return running


# ---------------------------------------------------------------------------
# Element-tree helpers — tolerant, never raise
# ---------------------------------------------------------------------------


def _localname(tag: Any) -> str:
    """Strip an XML namespace so the parser tolerates namespaced docs."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _find_child(parent: Any, name: str) -> Any:
    if parent is None:
        return None
    for child in parent:
        if _localname(child.tag) == name:
            return child
    return None


def _text(parent: Any, name: str) -> str | None:
    """Return the stripped text of *parent*'s child named *name*."""
    child = _find_child(parent, name)
    if child is None:
        return None
    if child.text is None:
        return None
    return str(child.text).strip() or None


def _parse_decimal(value: str | None, warnings: list[str], field_name: str) -> Decimal:
    """Parse a numeric *value* into a Decimal; returns ``0`` on failure."""
    if value is None:
        return Decimal("0")
    s = value.strip().replace(",", "")
    if s == "":
        return Decimal("0")
    if len(s) > MAX_NUMERIC_DIGITS:
        warnings.append(f"numeric_too_large:{field_name}")
        return Decimal("0")
    try:
        parsed = Decimal(str(s))
    except (DecimalException, ValueError, TypeError):
        warnings.append(f"unparseable_decimal:{field_name}")
        return Decimal("0")
    sign, _digits, exponent = parsed.as_tuple()
    del sign
    if isinstance(exponent, int) and abs(exponent) > MAX_NUMERIC_DIGITS:
        warnings.append(f"numeric_exponent_too_large:{field_name}")
        return Decimal("0")
    return parsed


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively convert Decimals to JSON-friendly strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


__all__ = [
    "KNOWN_DISCRETION_CODES",
    "KNOWN_PUT_CALL",
    "KNOWN_SSH_PRN_TYPES",
    "MAX_HOLDINGS",
    "MAX_INPUT_BYTES",
    "MAX_NUMERIC_DIGITS",
    "ThirteenFData",
    "ThirteenFHolding",
    "parse_13f",
]
