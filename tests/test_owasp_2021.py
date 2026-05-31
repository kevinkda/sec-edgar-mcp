"""OWASP Top 10 — 2021 security test suite for sec-edgar-mcp.

The 2021 edition reorders 2017 and adds A04 Insecure Design and A10 SSRF.
Each test asserts a concrete invariant on the read-only, unauthenticated SEC
EDGAR attack surface — no empty-coverage padding.

Applicability map (2021):
  * A01 Broken Access Control — read-only tool surface; SSRF-shaped CIK rejected
  * A02 Cryptographic Failures — secure cache perms; operator-email redaction
  * A03 Injection             — DuckDB bound params + strict input regex
  * A04 Insecure Design       — fail-closed UA, bounded response size, allow-list forms
  * A05 Security Misconfig     — explicit cache defaults, structured logging
  * A06 Vulnerable Components  — defusedxml/pydantic declared
  * A07 Identification/AuthN   — N/A explicit: unauthenticated API
  * A08 Software/Data Integrity— JSON shape validation; cache round-trip integrity
  * A09 Logging & Monitoring   — cache_events audit + JSON server log
  * A10 SSRF                   — CIK/ticker/accession cannot redirect outbound host
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from sec_edgar_mcp.cache import Cache

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "sec_edgar_mcp"

SSRF_PAYLOADS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/steal",
    "file:///etc/passwd",
    "//evil.example/x",
    "localhost:8080",
    "127.0.0.1",
    "gopher://127.0.0.1:6379/_",
]


# ===========================================================================
# A01:2021 — Broken Access Control
# ===========================================================================


class TestA01AccessControl:
    @pytest.mark.asyncio
    async def test_tool_surface_is_read_only(self) -> None:
        from sec_edgar_mcp.server import app

        tools = await app().list_tools()
        for t in tools:
            assert not any(v in t.name for v in ("create", "update", "delete", "write", "submit", "post"))

    def test_no_src_file_performs_http_write(self) -> None:
        """No source file issues a mutating HTTP verb (POST/PUT/DELETE/PATCH)."""
        import re

        pattern = re.compile(r"\.(post|put|delete|patch)\s*\(", re.IGNORECASE)
        offenders = [
            str(py.relative_to(REPO_ROOT)) for py in SRC_ROOT.rglob("*.py") if pattern.search(py.read_text("utf-8"))
        ]
        assert offenders == [], f"mutating HTTP verb present: {offenders}"


# ===========================================================================
# A02:2021 — Cryptographic Failures
# ===========================================================================


class TestA02CryptographicFailures:
    def test_cache_file_owner_only_on_posix(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX-only perm semantics")
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            mode = stat.S_IMODE(os.stat(tmp_path / "c.duckdb").st_mode)
            assert mode == 0o600
        finally:
            cache.close()

    def test_operator_email_never_logged_plaintext(self, caplog: pytest.LogCaptureFixture) -> None:
        """An email passed through an exception hint is redacted in logs."""
        import logging

        from sec_edgar_mcp.errors import SecConfigurationError

        log = logging.getLogger("sec_edgar_mcp.test")
        with caplog.at_level(logging.WARNING, logger="sec_edgar_mcp.test"):
            exc = SecConfigurationError(hint="UA invalid for ceo@bigcorp.com")
            log.warning("config error: %s", exc)
        joined = " ".join(r.getMessage() for r in caplog.records) + " ".join(str(r.args) for r in caplog.records)
        assert "ceo@bigcorp.com" not in str(exc)
        assert "ceo@bigcorp.com" not in joined


# ===========================================================================
# A03:2021 — Injection
# ===========================================================================


class TestA03Injection:
    def test_duckdb_bound_params_block_sql_injection(self, tmp_path: Path) -> None:
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            payload = "'); DELETE FROM filings_index_cache;--"
            cache.put_filings_index({"k": payload}, {"v": payload})
            assert cache.get_filings_index({"k": payload}) == {"v": payload}
        finally:
            cache.close()

    def test_form_types_constrained_to_allowlist(self) -> None:
        """Arbitrary form types are rejected by the allow-list validator."""
        from sec_edgar_mcp.errors import SecValidationError
        from sec_edgar_mcp.models import GetCompanyFilingsInput

        with pytest.raises(SecValidationError):
            GetCompanyFilingsInput(cik_or_ticker="AAPL", form_types=["'; DROP--"])

    def test_item_codes_regex_constrained(self) -> None:
        from pydantic import ValidationError

        from sec_edgar_mcp.models import Get8KWithItemsInput

        with pytest.raises(ValidationError):
            Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["5.02", "$(id)"])


# ===========================================================================
# A04:2021 — Insecure Design
# ===========================================================================


class TestA04InsecureDesign:
    def test_fail_closed_without_user_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """By design the client refuses to call SEC without a valid UA."""
        from sec_edgar_mcp.client import resolve_user_agent
        from sec_edgar_mcp.errors import SecConfigurationError

        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "no-at-sign-here")
        with pytest.raises(SecConfigurationError):
            resolve_user_agent()

    @pytest.mark.asyncio
    async def test_response_size_is_bounded(self, make_client) -> None:
        """get_text caps the body so a giant filing cannot blow up MCP frames."""
        from tests.conftest import FakeRoute

        big = "A" * (200 * 1024)
        client = make_client([FakeRoute("/huge", text_body=big, content_type="text/html")])
        text, ctype, size, truncated = await client.get_text("https://www.sec.gov/huge", max_bytes=1024)
        assert truncated is True
        assert len(text) <= 1024

    def test_rate_limit_hard_cap_cannot_be_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even an operator override is clamped to the SEC fair-use hard cap."""
        from sec_edgar_mcp.client import SEC_HARD_RATE_LIMIT_PER_SEC, resolve_rate_limit

        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "10000")
        assert resolve_rate_limit() <= SEC_HARD_RATE_LIMIT_PER_SEC


# ===========================================================================
# A05:2021 — Security Misconfiguration
# ===========================================================================


class TestA05Misconfiguration:
    def test_cache_defaults_are_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sec_edgar_mcp.cache import cache_bypass, cache_enabled

        monkeypatch.delenv("SEC_EDGAR_CACHE_ENABLED", raising=False)
        monkeypatch.delenv("SEC_EDGAR_CACHE_BYPASS", raising=False)
        assert cache_enabled() is True
        assert cache_bypass() is False


# ===========================================================================
# A06:2021 — Vulnerable and Outdated Components
# ===========================================================================


class TestA06Components:
    def test_security_deps_declared(self) -> None:
        body = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        assert "defusedxml" in body and "pydantic" in body


# ===========================================================================
# A07:2021 — Identification and Authentication Failures  (N/A)
# ===========================================================================


class TestA07AuthFailures:
    @pytest.mark.asyncio
    async def test_na_unauthenticated_api(self) -> None:
        """N/A: SEC EDGAR requires no identity — there is no authn flow to fail.

        health_check reports UA-config state (the only "identity" concept) from
        env only, never claiming readiness without it.
        """
        from sec_edgar_mcp.tools.meta import health_check_impl

        out = await health_check_impl()
        assert "user_agent_configured" in out


# ===========================================================================
# A08:2021 — Software and Data Integrity Failures
# ===========================================================================


class TestA08DataIntegrity:
    @pytest.mark.asyncio
    async def test_unexpected_json_shape_does_not_propagate(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/scalar", text_body='"a-string"', content_type="application/json")])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/scalar")

    def test_cache_roundtrip_integrity(self, tmp_path: Path) -> None:
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            payload = {"company": {"name": "Apple Inc."}, "filings": [{"form": "10-K"}], "count": 1}
            cache.put_filings_index({"k": "AAPL"}, payload)
            assert cache.get_filings_index({"k": "AAPL"}) == payload
        finally:
            cache.close()


# ===========================================================================
# A09:2021 — Security Logging and Monitoring Failures
# ===========================================================================


class TestA09Logging:
    def test_cache_audit_events_recorded(self, tmp_path: Path) -> None:
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.put_search({"q": "x"}, {"v": 1})
            cache.get_search({"q": "x"})
            assert cache._conn is not None
            count = cache._conn.execute("SELECT COUNT(*) FROM cache_events").fetchone()[0]
            assert count >= 2
        finally:
            cache.close()


# ===========================================================================
# A10:2021 — Server-Side Request Forgery (SSRF)
# ===========================================================================


class TestA10SSRF:
    def test_cik_cannot_inject_arbitrary_url(self) -> None:
        """A URL/host-shaped CIK is rejected before any outbound request."""
        from sec_edgar_mcp.models import GetCompanyFilingsInput

        for payload in SSRF_PAYLOADS:
            with pytest.raises(Exception):
                GetCompanyFilingsInput(cik_or_ticker=payload)

    @pytest.mark.asyncio
    async def test_outbound_host_is_fixed_sec_domain(self) -> None:
        """The resolved CIK only ever fills a path segment on a fixed SEC host."""
        from sec_edgar_mcp.client import SecEdgarClient, resolve_cik
        from tests.conftest import FakeRoute, FakeTransport

        ticker_map = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}}
        transport = FakeTransport([FakeRoute("/files/company_tickers.json", json_body=ticker_map)])
        client = SecEdgarClient(
            user_agent="sec-edgar-mcp-tests/0 (test@example.com)",
            rate_limit_per_sec=10,
            transport=transport,
        )
        cik = await resolve_cik(client, "AAPL")
        assert cik == "0000320193"
        for url in transport.call_log:
            assert ".sec.gov/" in url
            assert "169.254" not in url and "evil.example" not in url

    def test_accession_cannot_smuggle_path_traversal(self) -> None:
        """A path-traversal accession is rejected by the strict regex."""
        from pydantic import ValidationError

        from sec_edgar_mcp.models import GetFilingTextInput

        for bad in ["../../../etc/passwd", "0000320193-24-000123/../../x"]:
            with pytest.raises(ValidationError):
                GetFilingTextInput(accession_number=bad)
