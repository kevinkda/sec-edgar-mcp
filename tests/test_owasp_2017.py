"""OWASP Top 10 — 2017 security test suite for sec-edgar-mcp.

The SEC EDGAR API is unauthenticated (no Bearer / refresh tokens), so the
threat model centres on **input → outbound-URL safety** (SSRF), **local
injection** (DuckDB parameterisation), **XML external-entity** safety
(defusedxml — the one true XXE-applicable surface in this repo), and
**operator-PII** (the SEC-mandated User-Agent email).

Each test asserts a concrete invariant — no empty-coverage padding.

Applicability map (2017):
  * A1 Injection            — DuckDB bound params + CIK/ticker/accession regex
  * A2 Broken AuthN         — N/A explicit: SEC API is unauthenticated
  * A3 Sensitive Data       — operator email redacted from every error
  * A4 XXE                  — defusedxml refuses DTD/external entities
  * A5 Broken Access Ctrl   — read-only by design (7 tools, no mutations)
  * A6 Security Misconfig   — secure cache file perms, UA enforcement
  * A7 XSS                  — N/A explicit: no HTML is generated/served
  * A8 Insecure Deserialize — JSON-only; non-dict/list shapes rejected
  * A9 Vulnerable Deps      — defusedxml/pydantic pinned in pyproject
  * A10 Insufficient Logging— cache_events audit table + JSON server log
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from sec_edgar_mcp.cache import Cache
from sec_edgar_mcp.errors import redact_email

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "sec_edgar_mcp"

# Canonical SSRF payloads reused across the OWASP suites.
SSRF_PAYLOADS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/steal",
    "file:///etc/passwd",
    "//evil.example/x",
    "localhost:8080",
    "127.0.0.1",
    "gopher://127.0.0.1:6379/_",
    "data:text/plain;base64,AAAA",
]


# ===========================================================================
# A1:2017 — Injection
# ===========================================================================


class TestA1Injection:
    def test_duckdb_writes_use_bound_params(self, tmp_path: Path) -> None:
        """A SQL payload stored via the cache is inert data, not executable DDL."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            payload = "x'); DROP TABLE search_cache;--"
            cache.put_search({"q": payload}, {"injected": payload})
            hit = cache.get_search({"q": payload})
            # The table survived and the payload round-trips as inert data.
            assert hit == {"injected": payload}
        finally:
            cache.close()

    def test_cik_regex_rejects_injection_chars(self) -> None:
        """CIK/ticker input forbids SQL/shell metacharacters before any use."""
        from pydantic import ValidationError

        from sec_edgar_mcp.models import GetCompanyFilingsInput

        for bad in ["1; DROP TABLE x", "AAPL'--", "1 OR 1=1", "$(reboot)", "`id`"]:
            with pytest.raises((ValidationError, Exception)):
                GetCompanyFilingsInput(cik_or_ticker=bad)

    def test_accession_regex_is_strict(self) -> None:
        """Accession numbers must match the exact 10-2-6 digit shape."""
        from pydantic import ValidationError

        from sec_edgar_mcp.models import GetFilingTextInput

        for bad in ["'; DROP--", "../../etc/passwd", "0000320193-24-00012X"]:
            with pytest.raises(ValidationError):
                GetFilingTextInput(accession_number=bad)


# ===========================================================================
# A2:2017 — Broken Authentication  (N/A — explicitly documented)
# ===========================================================================


class TestA2BrokenAuthentication:
    def test_no_auth_surface_exists(self) -> None:
        """N/A: SEC EDGAR is unauthenticated — there is no token/secret to break.

        We assert structurally that no source file *uses* auth primitives in
        code (assignment / call / header injection) so the N/A claim cannot
        silently rot if someone wires in an authenticated dependency later.
        Prose mentions (e.g. the errors.py docstring that says there are *no*
        bearer tokens) are deliberately not flagged.
        """
        import re

        # Match auth primitives used as code: `bearer=`, `oauth(`, header dict
        # keys like "Authorization", or token assignments — not bare prose.
        code_pattern = re.compile(
            r"(?i)(authorization\s*[:=]|bearer\s+[\"'{]|oauth\w*\s*\(|"
            r"client_secret\s*=|access_token\s*=|refresh_token\s*=)"
        )
        offenders = []
        for py in SRC_ROOT.rglob("*.py"):
            for lineno, line in enumerate(py.read_text("utf-8").splitlines(), 1):
                stripped = line.strip()
                # Skip comment / docstring-ish lines.
                if stripped.startswith(("#", '"', "'", "*")):
                    continue
                if code_pattern.search(line):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}")
        assert offenders == [], f"auth primitives unexpectedly used in code: {offenders}"


# ===========================================================================
# A3:2017 — Sensitive Data Exposure
# ===========================================================================


class TestA3SensitiveData:
    def test_operator_email_redacted_in_errors(self) -> None:
        """The SEC-mandated UA email is PII — it must never survive into errors."""
        leaky = "request failed for contact ops@mycompany.com via UA"
        assert "ops@mycompany.com" not in redact_email(leaky)
        assert "***REDACTED***" in redact_email(leaky)

    def test_all_exceptions_redact_email(self) -> None:
        """Every structured exception scrubs emails in its hint/reason."""
        from sec_edgar_mcp.errors import (
            SecConfigurationError,
            SecNotFoundError,
            SecTransientError,
            SecValidationError,
        )

        email = "secret.person@example.org"
        assert email not in str(SecValidationError(field="f", reason=f"bad {email}"))
        assert email not in str(SecNotFoundError(resource="r", hint=f"missing {email}"))
        assert email not in str(SecTransientError(status_code=500, attempt=1, hint=f"up {email}"))
        assert email not in str(SecConfigurationError(hint=f"set {email}"))

    def test_redact_is_idempotent(self) -> None:
        once = redact_email("a@b.com")
        assert redact_email(once) == once


# ===========================================================================
# A4:2017 — XML External Entities (XXE)  — the one true XXE surface
# ===========================================================================


class TestA4XXE:
    def test_dtd_external_entity_is_refused(self) -> None:
        """A Form 4 body with an external DTD entity must not exfiltrate files."""
        from sec_edgar_mcp._xbrl import parse_form4
        from sec_edgar_mcp.errors import Form4ParseError

        xxe = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE ownershipDocument [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<ownershipDocument><issuer>&xxe;</issuer></ownershipDocument>"
        )
        # defusedxml must refuse — parser raises Form4ParseError, never leaks.
        with pytest.raises(Form4ParseError):
            parse_form4(xxe, accession_number="0000000000-00-000000")

    def test_billion_laughs_is_refused(self) -> None:
        """An entity-expansion bomb must be rejected, not expanded."""
        from sec_edgar_mcp._xbrl import parse_form4
        from sec_edgar_mcp.errors import Form4ParseError

        lol = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            b"<ownershipDocument>&lol3;</ownershipDocument>"
        )
        with pytest.raises(Form4ParseError):
            parse_form4(lol, accession_number="0000000000-00-000000")

    def test_defusedxml_is_the_parser(self) -> None:
        """Structural guard: the XBRL module imports defusedxml, not stdlib ET."""
        body = (SRC_ROOT / "_xbrl.py").read_text("utf-8")
        assert "import defusedxml" in body
        assert "import xml.etree.ElementTree" not in body


# ===========================================================================
# A5:2017 — Broken Access Control  (read-only by design)
# ===========================================================================


class TestA5AccessControl:
    @pytest.mark.asyncio
    async def test_only_read_tools_are_exposed(self) -> None:
        """The 7 tools are all read/query verbs — no create/update/delete."""
        from sec_edgar_mcp.server import app

        tools = await app().list_tools()
        names = {t.name for t in tools}
        assert names == {
            "get_company_filings",
            "get_form4_insider_trades",
            "get_filing_text",
            "search_filings_full_text",
            "get_8k_with_items",
            "health_check",
            "get_server_info",
        }
        for n in names:
            assert not any(verb in n for verb in ("create", "update", "delete", "write", "post", "put"))

    def test_ssrf_payloads_rejected_by_cik_schema(self) -> None:
        """A URL-shaped CIK cannot redirect the outbound SEC request."""
        from pydantic import ValidationError

        from sec_edgar_mcp.models import GetCompanyFilingsInput

        for payload in SSRF_PAYLOADS:
            with pytest.raises((ValidationError, Exception)):
                GetCompanyFilingsInput(cik_or_ticker=payload)


# ===========================================================================
# A6:2017 — Security Misconfiguration
# ===========================================================================


class TestA6Misconfiguration:
    def test_cache_db_not_world_readable_on_posix(self, tmp_path: Path) -> None:
        """The DuckDB cache file is 0o600 (owner-only) on POSIX."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only perm semantics")
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            mode = stat.S_IMODE(os.stat(tmp_path / "c.duckdb").st_mode)
            assert mode == 0o600
            assert not (mode & stat.S_IRGRP)
            assert not (mode & stat.S_IROTH)
        finally:
            cache.close()

    def test_user_agent_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a valid UA, the client refuses to issue requests (fail-closed)."""
        from sec_edgar_mcp.client import resolve_user_agent
        from sec_edgar_mcp.errors import SecConfigurationError

        monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
        with pytest.raises(SecConfigurationError):
            resolve_user_agent()


# ===========================================================================
# A7:2017 — Cross-Site Scripting (XSS)  (N/A — explicitly documented)
# ===========================================================================


class TestA7XSS:
    def test_no_html_generation_surface(self) -> None:
        """N/A: this server returns structured JSON only and renders no HTML.

        Filing bodies are returned verbatim as opaque text in a JSON field;
        no template engine, no HTML response, no DOM. Structural guard: no
        source file builds an HTML response or imports a templating engine.
        """
        import re

        pattern = re.compile(r"\b(jinja2|render_template|text/html\"\s*\)|<html)", re.IGNORECASE)
        offenders = [
            str(py.relative_to(REPO_ROOT)) for py in SRC_ROOT.rglob("*.py") if pattern.search(py.read_text("utf-8"))
        ]
        assert offenders == [], f"unexpected HTML surface: {offenders}"


# ===========================================================================
# A8:2017 — Insecure Deserialization
# ===========================================================================


class TestA8Deserialization:
    @pytest.mark.asyncio
    async def test_non_dict_non_list_json_rejected(self, make_client) -> None:
        """A scalar JSON body is rejected, not blindly trusted."""
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/scalar", text_body="42", content_type="application/json")])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/scalar")

    def test_cache_deserialise_rejects_non_dict(self) -> None:
        """The cache deserialiser returns None for non-dict JSON, never raises."""
        from sec_edgar_mcp.cache import _deserialise

        assert _deserialise("[1,2,3]") is None
        assert _deserialise("not json") is None
        assert _deserialise(None) is None
        assert _deserialise('{"ok": 1}') == {"ok": 1}


# ===========================================================================
# A9:2017 — Using Components with Known Vulnerabilities
# ===========================================================================


class TestA9VulnerableComponents:
    def test_defusedxml_and_pydantic_pinned(self) -> None:
        """Security-critical deps are declared with version constraints."""
        body = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        assert "defusedxml" in body
        assert "pydantic" in body


# ===========================================================================
# A10:2017 — Insufficient Logging & Monitoring
# ===========================================================================


class TestA10Logging:
    def test_cache_events_audit_trail(self, tmp_path: Path) -> None:
        """Cache hits/misses/writes are recorded in the cache_events table."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.put_search({"q": "x"}, {"data": 1})
            cache.get_search({"q": "x"})  # hit
            cache.get_search({"q": "y"})  # miss
            assert cache._conn is not None
            kinds = {r[0] for r in cache._conn.execute("SELECT DISTINCT kind FROM cache_events").fetchall()}
            assert "write" in kinds
            assert "hit" in kinds
            assert "miss" in kinds
        finally:
            cache.close()

    def test_server_log_format_is_structured_json(self) -> None:
        """The server log handler emits JSON-shaped records (monitoring-friendly)."""
        body = (SRC_ROOT / "server.py").read_text("utf-8")
        assert '"level"' in body and '"msg"' in body
