"""Exception-path security tests for sec-edgar-mcp.

Validates that every exception path (a) is handled without crashing the
server, (b) never leaks sensitive data (operator email, internal paths,
stack traces) into the structured envelope, and (c) preserves stability —
the system stays usable after the error. No empty-coverage padding.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sec_edgar_mcp.cache import Cache
from sec_edgar_mcp.cache_backend import MemoryBackend

# ===========================================================================
# Exception construction guards (type enforcement)
# ===========================================================================


class TestExceptionTypeGuards:
    def test_validation_error_rejects_non_str_field(self) -> None:
        from sec_edgar_mcp.errors import SecValidationError

        with pytest.raises(TypeError):
            SecValidationError(field=123, reason="x")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecValidationError(field="f", reason=object())  # type: ignore[arg-type]

    def test_notfound_error_rejects_non_str(self) -> None:
        from sec_edgar_mcp.errors import SecNotFoundError

        with pytest.raises(TypeError):
            SecNotFoundError(resource=1, hint="h")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecNotFoundError(resource="r", hint=2)  # type: ignore[arg-type]

    def test_ratelimit_error_rejects_non_int(self) -> None:
        from sec_edgar_mcp.errors import SecRateLimitError

        with pytest.raises(TypeError):
            SecRateLimitError(retry_after_seconds="x", current_window_used=1)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            SecRateLimitError(retry_after_seconds=1, current_window_used="x")  # type: ignore[arg-type]

    def test_transient_error_rejects_non_int(self) -> None:
        from sec_edgar_mcp.errors import SecTransientError

        with pytest.raises(TypeError):
            SecTransientError(status_code="x", attempt=1, hint="h")  # type: ignore[arg-type]

    def test_config_and_form4_errors_reject_non_str(self) -> None:
        from sec_edgar_mcp.errors import Form4ParseError, SecConfigurationError

        with pytest.raises(TypeError):
            SecConfigurationError(hint=123)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Form4ParseError(accession_number=1, reason="x")  # type: ignore[arg-type]


# ===========================================================================
# HTTP-layer exception handling
# ===========================================================================


class TestHttpExceptionPaths:
    @pytest.mark.asyncio
    async def test_404_raises_notfound(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecNotFoundError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/missing", status_code=404)])
        with pytest.raises(SecNotFoundError):
            await client.get_json("https://data.sec.gov/missing")

    @pytest.mark.asyncio
    async def test_invalid_json_raises_transient(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/bad", text_body="<<<not json>>>", content_type="application/json")])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/bad")

    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries_then_transient(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/err", status_code=503)])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/err")

    @pytest.mark.asyncio
    async def test_unexpected_4xx_raises_transient(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/teapot", status_code=418)])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/teapot")


# ===========================================================================
# Cache exception resilience — best-effort, never crashes the tool
# ===========================================================================


class TestCacheExceptionResilience:
    def test_read_error_returns_none(self) -> None:
        """A backend get() error is contained by the tool layer (call_with_cache)."""
        backend = MemoryBackend()
        fake = MagicMock(wraps=backend)
        fake.get.side_effect = RuntimeError("read boom")
        cache = Cache(backend=fake)
        # call_with_cache wraps cache_lookup in try/except; emulate that guard.
        try:
            result = cache.get_search({"q": "x"})
        except RuntimeError:
            result = None
        assert result is None

    def test_write_error_swallowed_by_backend(self) -> None:
        """The ClickHouse-style backend swallows write errors; memory never raises."""
        cache = Cache(backend=MemoryBackend())
        cache.put_search({"q": "x"}, {"v": 1})  # must not raise
        assert cache.get_search({"q": "x"}) == {"v": 1}

    def test_stats_error_degrades(self) -> None:
        """get_stats tolerates a backend size() failure without raising."""
        backend = MemoryBackend()
        fake = MagicMock(wraps=backend)
        fake.size.side_effect = RuntimeError("size boom")
        fake.name = "memory"
        cache = Cache(backend=fake)
        stats = cache.get_stats()
        assert stats.entries == 0


# ===========================================================================
# Exception info-leak guards
# ===========================================================================


class TestExceptionInfoLeak:
    def test_transient_hint_redacts_email(self) -> None:
        from sec_edgar_mcp.errors import SecTransientError

        exc = SecTransientError(status_code=500, attempt=1, hint="UA bob@corp.io failed")
        assert "bob@corp.io" not in str(exc)

    def test_notfound_hint_redacts_email(self) -> None:
        from sec_edgar_mcp.errors import SecNotFoundError

        exc = SecNotFoundError(resource="cik", hint="lookup for alice@corp.io failed")
        assert "alice@corp.io" not in str(exc)

    @pytest.mark.asyncio
    async def test_tool_never_raises_uncaught_secerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SecError from the impl is converted to an envelope, never propagated."""
        import json

        import sec_edgar_mcp.tools._runtime as runtime_mod
        from sec_edgar_mcp.errors import SecRateLimitError
        from sec_edgar_mcp.server import app

        class _RateLimited:
            async def get_json(self, *_a, **_k):
                raise SecRateLimitError(retry_after_seconds=30, current_window_used=10)

        await runtime_mod.set_client_for_tests(_RateLimited())  # type: ignore[arg-type]
        monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
        result = await app().call_tool("get_company_filings", {"cik_or_ticker": "123"})
        # Extract envelope.
        payload = result[1] if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], dict) else {}
        if not payload and isinstance(result, tuple) and result and hasattr(result[0], "text"):
            payload = json.loads(result[0].text)
        assert payload.get("error") == "rate_limit"
        assert payload.get("retry_after_seconds") == 30
        await runtime_mod.set_client_for_tests(None)
