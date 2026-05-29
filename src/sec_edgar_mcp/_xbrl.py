"""Defused XBRL/XML parser for SEC Form 4 (insider transaction) filings.

Form 4 is the Section 16 disclosure that officers, directors, and ≥10 %
shareholders must file within 2 business days of every transaction in
the issuer's securities.  Unlike full XBRL financial reports (which use
the ``us-gaap`` taxonomy), Form 4 is published as a hand-rolled XML
schema (``X0306``) with a fixed shape rooted at ``<ownershipDocument>``.
We parse it with a small recursive descent over a defused element tree.

Security guarantees
-------------------

We use :mod:`defusedxml.ElementTree`, which:

* refuses external entity references (XXE);
* caps entity expansion depth (billion-laughs);
* disables DTD parsing entirely;
* refuses external general/parameter entity references.

We deliberately do **not** use ``lxml.objectify`` or ``xmltodict``: they
are faster but expose larger attack surfaces (lxml in particular reaches
into ``libxml2`` for entity resolution unless explicitly hardened).

We also impose process-local ceilings on top of defusedxml's defaults:

* ``MAX_INPUT_BYTES = 8 MiB`` — Form 4 filings are typically <100 kB; an
  8 MiB cap protects us against malicious oversized payloads even before
  the XML parser starts.
* ``MAX_TRANSACTIONS = 5_000`` — capped to keep the returned payload
  bounded for MCP frame budgets.
* ``MAX_INT_DIGITS = 30`` / ``MAX_DECIMAL_DIGITS = 30`` — refuses to
  materialise pathologically large numbers via ``Decimal``.

Tolerant parsing
----------------

Form 4 filings come from thousands of filers and a long tail of edge
cases (missing optional fields, blank ``<value>`` elements, unknown
``transactionCode`` values).  Per the spec, we **never raise** on a
missing or malformed *field*; we set the corresponding dataclass slot
to ``None`` (or zero) and append a structured warning to
``Form4Data.raw_warnings``.  Only a structurally broken document
(unparseable XML, wrong root element, ``DefusedXmlException``, …) raises
:class:`Form4ParseError`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, DecimalException
from typing import Any, Final, Literal
from xml.etree.ElementTree import ParseError as ETParseError

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException

from .errors import Form4ParseError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration & limits
# ---------------------------------------------------------------------------

#: Hard byte ceiling on a single Form 4 XML body.  Real filings are <100 kB.
MAX_INPUT_BYTES: Final[int] = 8 * 1024 * 1024

#: Cap on the number of transactions we ingest per filing.
MAX_TRANSACTIONS: Final[int] = 5_000

#: Reject Decimal/int strings longer than this many characters before parsing.
MAX_NUMERIC_DIGITS: Final[int] = 30

#: Known SEC transaction codes (Section 16 Form 4).
#: Reference: https://www.sec.gov/about/forms/form4data.pdf
KNOWN_TRANSACTION_CODES: Final[frozenset[str]] = frozenset(
    {
        "P",  # open-market or private purchase
        "S",  # open-market or private sale
        "V",  # transaction voluntarily reported earlier
        "A",  # grant, award, or other acquisition
        "D",  # disposition to issuer
        "F",  # payment of exercise price/tax via security delivery
        "I",  # discretionary transaction
        "M",  # exercise/conversion of derivative
        "C",  # conversion of derivative
        "E",  # expiration of short derivative
        "H",  # expiration of long derivative
        "O",  # exercise of out-of-the-money derivative
        "X",  # exercise of in-the-money or at-the-money derivative
        "G",  # bona fide gift
        "L",  # small acquisition under Rule 16a-6
        "W",  # acquisition / disposition by will or laws of descent
        "Z",  # deposit into / withdrawal from voting trust
        "J",  # other (described in footnote)
        "K",  # transaction in equity swap
        "U",  # disposition pursuant to a tender of shares
    }
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Form4Transaction:
    """A single non-derivative or derivative transaction row."""

    transaction_date: date | None  # None when the filing omits the date
    code: str  # transactionCode (raw, may be "" or unknown)
    shares: Decimal
    price_per_share: Decimal | None  # None for gifts (G), grants, etc.
    direct_or_indirect: Literal["D", "I", ""]
    acquired_or_disposed: Literal["A", "D", ""]
    post_transaction_shares: Decimal
    is_derivative: bool
    security_title: str
    derivative_security_title: str | None  # underlying for derivative rows


@dataclass(frozen=True)
class Form4Data:
    """Structured Form 4 payload returned by :func:`parse_form4`."""

    accession_number: str
    document_type: str  # e.g. "4" or "4/A"
    period_of_report: date | None
    issuer_cik: str
    issuer_name: str
    issuer_ticker: str
    reporting_owner_cik: str
    reporting_owner_name: str
    is_officer: bool
    is_director: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str | None
    transactions: tuple[Form4Transaction, ...]
    transaction_count: int
    net_buy_value: Decimal
    net_sell_value: Decimal
    raw_warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serialisable dict (Decimals → strings)."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            out[key] = _jsonable(value)
        return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_form4(xml_bytes: bytes, *, accession_number: str = "") -> Form4Data:
    """Parse a Form 4 XML body into a :class:`Form4Data`.

    Raises :class:`Form4ParseError` only for *structural* failures
    (unparseable XML, wrong root element, defusedxml refusal of an entity
    expansion, …).  Field-level issues are tolerated and surfaced via
    ``raw_warnings``.
    """
    if not isinstance(xml_bytes, (bytes, bytearray)):
        raise Form4ParseError(
            accession_number=accession_number,
            reason=f"input must be bytes, got {type(xml_bytes).__name__}",
        )
    if len(xml_bytes) == 0:
        raise Form4ParseError(
            accession_number=accession_number,
            reason="empty document",
        )
    if len(xml_bytes) > MAX_INPUT_BYTES:
        raise Form4ParseError(
            accession_number=accession_number,
            reason=(f"document exceeds maximum size of {MAX_INPUT_BYTES} bytes"),
        )

    # Reject SEC's XSLT-rendered HTML view (``xsl<style>/<doc>.xml``)
    # with a *structured* reason instead of a generic "mismatched tag"
    # ParseError.  This is a real-corpus contract guard: the v0.2.0
    # PB-3 incident (2026-05-25) showed every Form 4 returning
    # "mismatched tag: line 29, column 16" because the caller was
    # fetching the HTML rendering instead of the raw XML.  Surfacing
    # the cause makes that misuse self-diagnosing without weakening
    # ``defusedxml``'s secure parsing posture.
    if _looks_like_html_rendering(xml_bytes):
        raise Form4ParseError(
            accession_number=accession_number,
            reason="received SEC XSLT-rendered HTML, expected raw ownership XML",
        )

    try:
        root = DET.fromstring(xml_bytes)
    except DefusedXmlException as exc:
        raise Form4ParseError(
            accession_number=accession_number,
            reason=f"defused XML rejected document: {type(exc).__name__}",
        ) from exc
    except ETParseError as exc:
        raise Form4ParseError(
            accession_number=accession_number,
            reason=f"malformed XML: {exc}",
        ) from exc

    # Root may be ``ownershipDocument`` or any nested wrapper SEC has
    # historically used; the canonical Form 4 root is ``ownershipDocument``.
    if _localname(root.tag) != "ownershipDocument":
        raise Form4ParseError(
            accession_number=accession_number,
            reason=(f"expected root <ownershipDocument>, got <{_localname(root.tag)}>"),
        )

    warnings: list[str] = []

    document_type = _text(root, "documentType") or "4"
    period_of_report = _parse_date(_text(root, "periodOfReport"), warnings, "periodOfReport")

    issuer = _find_child(root, "issuer")
    issuer_cik = _text(issuer, "issuerCik") or ""
    issuer_name = _text(issuer, "issuerName") or ""
    issuer_ticker = _text(issuer, "issuerTradingSymbol") or ""

    owner = _find_child(root, "reportingOwner")
    owner_id = _find_child(owner, "reportingOwnerId") if owner is not None else None
    reporting_owner_cik = _text(owner_id, "rptOwnerCik") or ""
    reporting_owner_name = _text(owner_id, "rptOwnerName") or ""

    rel = _find_child(owner, "reportingOwnerRelationship") if owner is not None else None
    is_officer = _bool(_text(rel, "isOfficer"))
    is_director = _bool(_text(rel, "isDirector"))
    is_ten_percent_owner = _bool(_text(rel, "isTenPercentOwner"))
    is_other = _bool(_text(rel, "isOther"))
    officer_title = _text(rel, "officerTitle") or None

    transactions: list[Form4Transaction] = []
    net_buy_value = Decimal("0")
    net_sell_value = Decimal("0")

    for tx_elem in _iter_transactions(root):
        if len(transactions) >= MAX_TRANSACTIONS:
            warnings.append(f"transaction_cap_reached:{MAX_TRANSACTIONS}")
            break
        tx = _parse_transaction(tx_elem, warnings)
        if tx is None:
            continue
        transactions.append(tx)
        net_buy_value, net_sell_value = _accumulate_net_values(tx, net_buy_value, net_sell_value, warnings)

    return Form4Data(
        accession_number=accession_number,
        document_type=document_type,
        period_of_report=period_of_report,
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        issuer_ticker=issuer_ticker,
        reporting_owner_cik=reporting_owner_cik,
        reporting_owner_name=reporting_owner_name,
        is_officer=is_officer,
        is_director=is_director,
        is_ten_percent_owner=is_ten_percent_owner,
        is_other=is_other,
        officer_title=officer_title,
        transactions=tuple(transactions),
        transaction_count=len(transactions),
        net_buy_value=net_buy_value,
        net_sell_value=net_sell_value,
        raw_warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Per-transaction parsing
# ---------------------------------------------------------------------------


def _accumulate_net_values(
    tx: Form4Transaction,
    net_buy_value: Decimal,
    net_sell_value: Decimal,
    warnings: list[str],
) -> tuple[Decimal, Decimal]:
    """Fold *tx* into the running net buy / net sell totals.

    Skips:
        * transactions without a price (gifts / grants / option exercises);
        * transactions whose ``shares * price`` overflows the active
          ``decimal.Context`` (Hypothesis fuzz exposes this with
          scientific-notation Decimals).
    """
    if tx.price_per_share is None or tx.price_per_share == 0:
        return net_buy_value, net_sell_value
    try:
        value = tx.shares * tx.price_per_share
    except DecimalException:
        warnings.append("net_value_overflow")
        return net_buy_value, net_sell_value
    if tx.acquired_or_disposed == "A":
        try:
            net_buy_value += value
        except DecimalException:  # pragma: no cover - extreme overflow accumulation
            warnings.append("net_value_overflow")
    elif tx.acquired_or_disposed == "D":
        try:
            net_sell_value += value
        except DecimalException:  # pragma: no cover - extreme overflow accumulation
            warnings.append("net_value_overflow")
    return net_buy_value, net_sell_value


def _iter_transactions(root: Any) -> list[tuple[Any, bool]]:
    """Yield ``(element, is_derivative)`` pairs in document order."""
    out: list[tuple[Any, bool]] = []
    nd_table = _find_child(root, "nonDerivativeTable")
    if nd_table is not None:
        for elem in nd_table:
            if _localname(elem.tag) == "nonDerivativeTransaction":
                out.append((elem, False))
    d_table = _find_child(root, "derivativeTable")
    if d_table is not None:
        for elem in d_table:
            if _localname(elem.tag) == "derivativeTransaction":
                out.append((elem, True))
    return out


def _parse_transaction(
    pair: tuple[Any, bool],
    warnings: list[str],
) -> Form4Transaction | None:
    elem, is_derivative = pair

    security_title = _text_value(_find_child(elem, "securityTitle")) or ""
    transaction_date = _parse_date(
        _text_value(_find_child(elem, "transactionDate")),
        warnings,
        "transactionDate",
    )

    coding = _find_child(elem, "transactionCoding")
    code = (_text(coding, "transactionCode") or "").strip().upper()
    if code and code not in KNOWN_TRANSACTION_CODES:
        warnings.append(f"unknown_transaction_code:{code}")

    amounts = _find_child(elem, "transactionAmounts")
    shares = _parse_decimal(
        _text_value(_find_child(amounts, "transactionShares")),
        warnings,
        "transactionShares",
    )
    price = _parse_decimal_optional(
        _text_value(_find_child(amounts, "transactionPricePerShare")),
        warnings,
        "transactionPricePerShare",
    )
    acquired_or_disposed_raw = (
        (_text_value(_find_child(amounts, "transactionAcquiredDisposedCode")) or "").strip().upper()
    )
    if acquired_or_disposed_raw not in ("A", "D", ""):
        warnings.append(f"unknown_acquired_disposed_code:{acquired_or_disposed_raw}")
        acquired_or_disposed_raw = ""

    post_amt = _find_child(elem, "postTransactionAmounts")
    post_shares = _parse_decimal(
        _text_value(_find_child(post_amt, "sharesOwnedFollowingTransaction")),
        warnings,
        "sharesOwnedFollowingTransaction",
    )

    ownership = _find_child(elem, "ownershipNature")
    direct_or_indirect_raw = (_text_value(_find_child(ownership, "directOrIndirectOwnership")) or "").strip().upper()
    if direct_or_indirect_raw not in ("D", "I", ""):
        warnings.append(f"unknown_direct_or_indirect:{direct_or_indirect_raw}")
        direct_or_indirect_raw = ""

    derivative_underlying: str | None = None
    if is_derivative:
        underlying = _find_child(elem, "underlyingSecurity")
        derivative_underlying = _text_value(_find_child(underlying, "underlyingSecurityTitle")) or None

    return Form4Transaction(
        transaction_date=transaction_date,
        code=code,
        shares=shares,
        price_per_share=price,
        direct_or_indirect=direct_or_indirect_raw,  # type: ignore[arg-type]
        acquired_or_disposed=acquired_or_disposed_raw,  # type: ignore[arg-type]
        post_transaction_shares=post_shares,
        is_derivative=is_derivative,
        security_title=security_title,
        derivative_security_title=derivative_underlying,
    )


# ---------------------------------------------------------------------------
# Element-tree helpers — tolerant, never raise
# ---------------------------------------------------------------------------


def _localname(tag: Any) -> str:
    """Strip XML namespace to keep parser tolerant of namespaced docs."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _looks_like_html_rendering(xml_bytes: bytes) -> bool:
    """Heuristic: detect SEC's XSLT-rendered HTML view of a Form 4.

    SEC's submissions API returns ``xsl<style>/<doc>.xml`` as the
    ``primaryDocument`` for Form 4; that path serves the *XSLT-rendered
    HTML* view (with ``<!DOCTYPE html>`` + unclosed ``<br>`` / ``<meta>``
    / ``<hr>`` tags) which is fundamentally not parseable as XML.  The
    raw ownership XML lives at ``<doc>.xml`` (one directory up).

    We sniff the leading 1024 bytes after the XML prolog for either an
    HTML5 doctype or an opening ``<html>`` element.  We deliberately
    look only at the head of the document so a large XSLT-rendered
    payload is rejected before any defusedxml work happens.

    Returns ``False`` for raw ``<ownershipDocument>`` bodies even if
    their body text mentions ``html`` (e.g. inside a footnote).
    """
    if not xml_bytes:
        return False
    head = xml_bytes[:1024].lstrip()
    # Skip optional XML prolog and BOM.
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    if head.startswith(b"<?xml"):
        end = head.find(b"?>")
        if end != -1:
            head = head[end + 2 :].lstrip()
    # Lowercase for case-insensitive doctype / tag detection.
    head_lower = head.lower()
    if head_lower.startswith(b"<!doctype html"):
        return True
    return head_lower.startswith(b"<html")


def _find_child(parent: Any, name: str) -> Any:
    if parent is None:
        return None
    for child in parent:
        if _localname(child.tag) == name:
            return child
    return None


def _text(parent: Any, name: str) -> str | None:
    """Return the text of *parent*'s child named *name* (no <value> wrapper)."""
    child = _find_child(parent, name)
    if child is None:
        return None
    text = child.text
    if text is None:
        return None
    return str(text).strip() or None


def _text_value(parent: Any) -> str | None:
    """Return ``parent/value`` text, falling back to ``parent`` text.

    Form 4 wraps most leaf values inside a ``<value>`` child to leave room
    for footnotes; we collapse that here.
    """
    if parent is None:
        return None
    value_child = _find_child(parent, "value")
    if value_child is not None and value_child.text is not None:
        return str(value_child.text).strip() or None
    if parent.text is not None:
        return str(parent.text).strip() or None
    return None


def _bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes"}


def _parse_date(value: str | None, warnings: list[str], field_name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        warnings.append(f"unparseable_date:{field_name}")
        return None


def _parse_decimal(value: str | None, warnings: list[str], field_name: str) -> Decimal:
    """Parse a numeric *value* into a Decimal; returns ``0`` on failure."""
    parsed = _parse_decimal_optional(value, warnings, field_name)
    return parsed if parsed is not None else Decimal("0")


def _parse_decimal_optional(
    value: str | None,
    warnings: list[str],
    field_name: str,
) -> Decimal | None:
    if value is None:
        return None
    s = value.strip()
    if s == "":
        return None
    if len(s) > MAX_NUMERIC_DIGITS:
        warnings.append(f"numeric_too_large:{field_name}")
        return None
    try:
        # Decimal(str(...)) avoids any binary-float-precision detour.
        parsed = Decimal(str(s))
    except (DecimalException, ValueError, TypeError):
        warnings.append(f"unparseable_decimal:{field_name}")
        return None
    # Reject Decimals with extreme exponents — multiplying two such
    # values can trigger ``decimal.Overflow`` when we compute net buy /
    # net sell totals.  ``MAX_NUMERIC_DIGITS`` doubles as the absolute
    # exponent ceiling so a fuzz-generated ``"1E1000000"`` is dropped.
    sign, _digits, exponent = parsed.as_tuple()
    del sign
    if isinstance(exponent, int) and abs(exponent) > MAX_NUMERIC_DIGITS:
        warnings.append(f"numeric_exponent_too_large:{field_name}")
        return None
    return parsed


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively convert Decimals / dates to JSON-friendly primitives."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


__all__ = [
    "KNOWN_TRANSACTION_CODES",
    "MAX_INPUT_BYTES",
    "MAX_NUMERIC_DIGITS",
    "MAX_TRANSACTIONS",
    "Form4Data",
    "Form4Transaction",
    "parse_form4",
]
