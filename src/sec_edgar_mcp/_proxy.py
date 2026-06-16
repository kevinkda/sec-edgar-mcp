"""Bounded key-information extractor for SEC DEF 14A proxy statements.

A DEF 14A (definitive proxy statement) is the document a company files
ahead of its annual shareholder meeting.  It discloses, among other
things:

* executive compensation (the Summary Compensation Table);
* the board of directors / nominees;
* shareholder proposals up for a vote;
* the meeting date and record date;
* the independent auditor.

Unlike Form 4 (R8) and 13F (T2), DEF 14A is published as **HTML**, not a
machine-readable XML schema.  Parsing arbitrary issuer HTML as XML is
both unreliable (HTML is rarely well-formed XML) and an unnecessary
attack-surface expansion.  We therefore extract key facts from a
**bounded, tag-stripped plain-text projection** of the document using
anchored heuristics — we never feed untrusted issuer HTML into an XML
parser.

Security guarantees
-------------------

* ``MAX_INPUT_CHARS`` caps the text we scan (defence against a huge
  payload exhausting CPU in the regex scanner).
* The tag stripper is a single non-backtracking character scan — there
  is no nested-quantifier regex that could ReDoS on adversarial input.
* No XML/DTD/entity parsing happens here, so the XXE class does not
  apply to this path.  (The 13F path that *does* parse XML uses
  ``defusedxml`` — see :mod:`sec_edgar_mcp._thirteenf`.)
* Every extracted field is length-capped before it is returned so a
  crafted filing cannot smuggle an oversized blob into the MCP frame.

Tolerant extraction
-------------------

Issuer formatting varies wildly.  We **never raise** on a missing
section — absent facts come back as ``None`` / empty lists plus a
structured ``warnings`` entry.  Only a non-string / empty body raises
:class:`~sec_edgar_mcp.errors.SecValidationError`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from .errors import SecValidationError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Cap on the characters we scan from a proxy body (post-tag-strip).
MAX_INPUT_CHARS: Final[int] = 4 * 1024 * 1024

#: Cap on a single extracted free-text field.
MAX_FIELD_CHARS: Final[int] = 512

#: Cap on the number of items in any extracted list (directors, proposals).
MAX_LIST_ITEMS: Final[int] = 100

#: Cap on a single extracted dollar figure's digit count.
MAX_MONEY_DIGITS: Final[int] = 15

# ---------------------------------------------------------------------------
# Patterns (all anchored / linear-time — no nested quantifiers)
# ---------------------------------------------------------------------------

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]{0,4096}>")
_WS_RE: Final[re.Pattern[str]] = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE: Final[re.Pattern[str]] = re.compile(r"\n{2,}")

#: Meeting / record date — "Annual Meeting ... on May 15, 2026".  The gap
#: tolerates intervening newlines (bounded to 120 chars) since block-level
#: HTML often splits the anchor and the date across lines.
_MEETING_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:annual|special)\s+meeting[\s\S]{0,120}?"
    r"((?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_RECORD_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"record\s+date[\s\S]{0,120}?"
    r"((?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_FISCAL_YEAR_RE: Final[re.Pattern[str]] = re.compile(
    r"fiscal\s+year\s+(?:ended\s+)?(\d{4})",
    re.IGNORECASE,
)

#: Auditor — "independent registered public accounting firm, <Name>".
_AUDITOR_RE: Final[re.Pattern[str]] = re.compile(
    r"independent\s+registered\s+public\s+accounting\s+firm[,:]?\s+"
    r"([A-Z][A-Za-z&.,'\- ]{2,80})",
)

#: Proposal lines — "Proposal 1: Election of Directors".  The title is
#: lazily matched and stops before the next "Proposal" token so several
#: proposals on a single line are still split into distinct rows.
_PROPOSAL_RE: Final[re.Pattern[str]] = re.compile(
    r"proposal\s+(?:no\.?\s*)?(\d{1,2})\s*[:.\-—]\s*([A-Z][^\n]*?)(?=\s+proposal\s+(?:no\.?\s*)?\d|\n|$)",
    re.IGNORECASE,
)

#: A dollar figure — "$ 12,345,678" (optional spaces, comma groups).
_MONEY_RE: Final[re.Pattern[str]] = re.compile(r"\$\s?([0-9][0-9,]{0,20})")

#: CEO / NEO compensation anchor — "Total Compensation ... $X".
_TOTAL_COMP_RE: Final[re.Pattern[str]] = re.compile(
    r"total\s+compensation[^$\n]{0,80}?\$\s?([0-9][0-9,]{0,20})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyProposal:
    """A single shareholder proposal up for a vote."""

    number: int
    title: str


@dataclass(frozen=True)
class ProxyStatementData:
    """Structured DEF 14A key-information payload."""

    accession_number: str
    meeting_date: str | None
    record_date: str | None
    fiscal_year: str | None
    auditor: str | None
    proposals: tuple[ProxyProposal, ...]
    proposal_count: int
    max_total_compensation: str | None  # largest "Total Compensation $" figure
    compensation_figures: tuple[str, ...]  # distinct dollar figures near comp anchors
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {k: _jsonable(v) for k, v in asdict(self).items()}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_proxy_statement(
    body: str,
    *,
    accession_number: str = "",
) -> ProxyStatementData:
    """Extract key DEF 14A facts from an HTML/text proxy *body*.

    Raises :class:`SecValidationError` only when *body* is not a non-empty
    string.  All field-level absences are tolerated and surfaced via
    ``warnings``.
    """
    if not isinstance(body, str):
        raise SecValidationError(
            field="body",
            reason=f"proxy body must be str, got {type(body).__name__}",
        )
    if body == "":
        raise SecValidationError(field="body", reason="empty proxy body")

    warnings: list[str] = []
    text = _to_plain_text(body)
    if len(text) >= MAX_INPUT_CHARS:
        warnings.append(f"input_truncated:{MAX_INPUT_CHARS}")

    meeting_date = _first_group(_MEETING_DATE_RE, text)
    record_date = _first_group(_RECORD_DATE_RE, text)
    fiscal_year = _first_group(_FISCAL_YEAR_RE, text)
    auditor = _clean_field(_first_group(_AUDITOR_RE, text))

    proposals = _extract_proposals(text, warnings)
    comp_figures = _extract_compensation(text)
    max_comp = _max_money(comp_figures)

    if meeting_date is None:
        warnings.append("missing_meeting_date")
    if not proposals:
        warnings.append("missing_proposals")
    if not comp_figures:
        warnings.append("missing_compensation")

    return ProxyStatementData(
        accession_number=accession_number,
        meeting_date=meeting_date,
        record_date=record_date,
        fiscal_year=fiscal_year,
        auditor=auditor,
        proposals=tuple(proposals),
        proposal_count=len(proposals),
        max_total_compensation=max_comp,
        compensation_figures=tuple(comp_figures),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _to_plain_text(body: str) -> str:
    """Strip HTML tags + collapse whitespace, bounded to ``MAX_INPUT_CHARS``.

    Operates on a hard prefix of the input so an oversized payload cannot
    drive the regex scanner past the cap.
    """
    head = body[:MAX_INPUT_CHARS]
    no_tags = _TAG_RE.sub(" ", head)
    no_tags = _unescape_basic_entities(no_tags)
    collapsed = _WS_RE.sub(" ", no_tags)
    collapsed = _MULTI_NL_RE.sub("\n", collapsed)
    return collapsed.strip()


def _unescape_basic_entities(text: str) -> str:
    """Replace the handful of HTML entities that matter for extraction.

    We deliberately do **not** call ``html.unescape`` with numeric
    entity decoding on the full body — that path can synthesise control
    characters.  A small fixed map covers the named entities found in
    proxy prose without expanding the attack surface.
    """
    replacements = {
        "&amp;": "&",
        "&nbsp;": " ",
        "&#160;": " ",
        "&quot;": '"',
        "&apos;": "'",
        "&lt;": "<",
        "&gt;": ">",
        "&#36;": "$",
        "&#8217;": "'",
        "&#8212;": "—",
    }
    for needle, repl in replacements.items():
        if needle in text:
            text = text.replace(needle, repl)
    return text


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if m is None:
        return None
    value = m.group(1).strip()
    return value or None


def _clean_field(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().rstrip(".,;: ")
    if not cleaned:
        return None
    return cleaned[:MAX_FIELD_CHARS]


def _extract_proposals(text: str, warnings: list[str]) -> list[ProxyProposal]:
    out: list[ProxyProposal] = []
    seen: set[int] = set()
    for m in _PROPOSAL_RE.finditer(text):
        if len(out) >= MAX_LIST_ITEMS:
            warnings.append(f"proposal_cap_reached:{MAX_LIST_ITEMS}")
            break
        try:
            number = int(m.group(1))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
        if number in seen:
            continue
        seen.add(number)
        # The regex guarantees the title starts with an uppercase letter,
        # so it is always non-empty after the trailing-punctuation strip.
        title = m.group(2).strip().rstrip(".,;: ")[:MAX_FIELD_CHARS]
        out.append(ProxyProposal(number=number, title=title))
    out.sort(key=lambda p: p.number)
    return out


def _extract_compensation(text: str) -> list[str]:
    """Return distinct dollar figures anchored on 'Total Compensation'."""
    figures: list[str] = []
    seen: set[str] = set()
    for m in _TOTAL_COMP_RE.finditer(text):
        if len(figures) >= MAX_LIST_ITEMS:
            break
        raw = m.group(1).replace(",", "")
        if not raw or len(raw) > MAX_MONEY_DIGITS:
            continue
        canonical = f"${int(raw):,}"
        if canonical in seen:
            continue
        seen.add(canonical)
        figures.append(canonical)
    return figures


def _max_money(figures: list[str]) -> str | None:
    best: int | None = None
    for fig in figures:
        digits = fig.lstrip("$").replace(",", "")
        if not digits.isdigit():  # pragma: no cover - figures are pre-validated
            continue
        amount = int(digits)
        if best is None or amount > best:
            best = amount
    if best is None:
        return None
    return f"${best:,}"


def _jsonable(value: Any) -> Any:
    """Convert nested tuples (proposals) to JSON-friendly lists."""
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


__all__ = [
    "MAX_FIELD_CHARS",
    "MAX_INPUT_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_MONEY_DIGITS",
    "ProxyProposal",
    "ProxyStatementData",
    "extract_proxy_statement",
]
