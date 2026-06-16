"""Unit + boundary + exception tests for the DEF 14A proxy extractor.

Covers :mod:`sec_edgar_mcp._proxy`:

* happy-path extraction of meeting/record dates, auditor, proposals, comp;
* tolerant handling of missing sections (warnings, never raise);
* boundary cases (empty body, oversized body, list caps, money digit cap);
* the bounded tag-stripper + entity unescape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sec_edgar_mcp._proxy import (
    MAX_FIELD_CHARS,
    MAX_INPUT_CHARS,
    MAX_MONEY_DIGITS,
    ProxyProposal,
    ProxyStatementData,
    extract_proxy_statement,
)
from sec_edgar_mcp.errors import SecValidationError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "seed"


def _load_proxy() -> str:
    return (FIXTURE_DIR / "def14a_apple.htm").read_text(encoding="utf-8")


# ===========================================================================
# Happy path
# ===========================================================================


def test_extract_full_proxy() -> None:
    data = extract_proxy_statement(_load_proxy(), accession_number="0000320193-26-000004")
    assert isinstance(data, ProxyStatementData)
    assert data.meeting_date == "May 15, 2026"
    assert data.record_date == "March 20, 2026"
    assert data.fiscal_year == "2025"
    assert data.auditor is not None
    assert "Ernst" in data.auditor
    assert data.proposal_count == 4
    assert data.proposals[0] == ProxyProposal(number=1, title="Election of Directors")
    assert data.max_total_compensation == "$63,209,845"
    assert "$27,180,000" in data.compensation_figures


def test_proposals_sorted_and_deduped() -> None:
    data = extract_proxy_statement(_load_proxy())
    numbers = [p.number for p in data.proposals]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


def test_to_dict_shape() -> None:
    out = extract_proxy_statement(_load_proxy()).to_dict()
    assert out["meeting_date"] == "May 15, 2026"
    assert out["proposal_count"] == 4
    assert isinstance(out["proposals"], list)


# ===========================================================================
# Validation failures
# ===========================================================================


def test_non_string_body_raises() -> None:
    with pytest.raises(SecValidationError) as ei:
        extract_proxy_statement(b"bytes")  # type: ignore[arg-type]
    assert ei.value.field == "body"


def test_empty_body_raises() -> None:
    with pytest.raises(SecValidationError) as ei:
        extract_proxy_statement("")
    assert "empty" in ei.value.reason


# ===========================================================================
# Tolerant handling of missing sections
# ===========================================================================


def test_missing_everything_warns() -> None:
    data = extract_proxy_statement("<html><body>nothing useful here</body></html>")
    assert data.meeting_date is None
    assert data.proposal_count == 0
    assert data.max_total_compensation is None
    assert "missing_meeting_date" in data.warnings
    assert "missing_proposals" in data.warnings
    assert "missing_compensation" in data.warnings


def test_auditor_none_when_absent() -> None:
    data = extract_proxy_statement("Annual Meeting on May 1, 2026. Proposal 1: Foo")
    assert data.auditor is None


def test_special_meeting_matches() -> None:
    data = extract_proxy_statement("Special Meeting will be held on June 2, 2027.")
    assert data.meeting_date == "June 2, 2027"


# ===========================================================================
# Boundary
# ===========================================================================


def test_oversized_body_truncated() -> None:
    body = "Annual Meeting on May 1, 2026. " + ("x" * (MAX_INPUT_CHARS + 10))
    data = extract_proxy_statement(body)
    assert any(w.startswith("input_truncated:") for w in data.warnings)


def test_proposal_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sec_edgar_mcp._proxy.MAX_LIST_ITEMS", 2)
    body = " ".join(f"Proposal {i}: Item number {i}" for i in range(1, 6))
    data = extract_proxy_statement(body)
    assert data.proposal_count == 2
    assert any("proposal_cap_reached:2" in w for w in data.warnings)


def test_compensation_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sec_edgar_mcp._proxy.MAX_LIST_ITEMS", 2)
    body = " ".join(f"Total Compensation $ {i}00,000" for i in range(1, 6))
    data = extract_proxy_statement(body)
    assert len(data.compensation_figures) == 2


def test_money_digit_cap_skips_huge() -> None:
    huge = "9" * (MAX_MONEY_DIGITS + 5)
    body = f"Total Compensation $ {huge}"
    data = extract_proxy_statement(body)
    assert data.compensation_figures == ()
    assert data.max_total_compensation is None


def test_field_length_cap() -> None:
    long_title = "A" * (MAX_FIELD_CHARS + 50)
    body = f"Proposal 1: {long_title}"
    data = extract_proxy_statement(body)
    assert len(data.proposals[0].title) <= MAX_FIELD_CHARS


def test_entity_unescape() -> None:
    body = "Annual Meeting on May 1, 2026. Auditor independent registered public accounting firm, Smith &amp; Co"
    data = extract_proxy_statement(body)
    assert data.auditor is not None
    assert "&" in data.auditor


def test_duplicate_compensation_deduped() -> None:
    body = "Total Compensation $100,000 and again Total Compensation $100,000"
    data = extract_proxy_statement(body)
    assert data.compensation_figures == ("$100,000",)


def test_blank_money_group_skipped() -> None:
    # "Total Compensation $ ," with no digits should not crash / yield a figure.
    body = "Total Compensation: see table. Total Compensation $1,000"
    data = extract_proxy_statement(body)
    assert data.compensation_figures == ("$1,000",)
