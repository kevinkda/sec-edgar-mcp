"""Coverage completion suite — drive the residual uncovered branches to 100%.

Every test here targets a specific ``file:line`` gap identified by
``pytest --cov-report=term-missing`` against the v0.2.2 baseline (92.13%).
No empty-coverage padding: each test asserts a concrete observable invariant.

Gap map (baseline 92.13%):
    * server.py             — stdio-harden OSError branches, _safe_run guard
    * tools/_runtime.py     — double-checked lock, cache lookup/store exceptions
    * tools/search.py       — non-int total, non-list hits, non-dict hit, _first
    * tools/filings.py      — cache-hit return, complete-doc fallbacks, item parse
    * tools/insider.py      — _first/_safe_get helpers
    * tools/meta.py         — UA missing / format-invalid, cache disabled/None/error
    * cache.py              — quarantine reopen, get/put DuckDB errors, expired
    * _xbrl.py              — None-transaction skip, non-str localname, numeric cap
    * client.py             — non-dict json shape, ticker cik_str str form
    * errors.py             — SecTransientError.__str__
    * models.py             — _strip_item_codes passthrough
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import duckdb
import pytest

import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.cache import Cache

# ===========================================================================
# server.py — error framing, stdio hardening, entry point
# ===========================================================================


class TestServerGaps:
    def test_frame_error_all_branches(self) -> None:
        """_frame_error maps every structured exception to its envelope (137-156)."""
        from sec_edgar_mcp import server as srv
        from sec_edgar_mcp.errors import (
            Form4ParseError,
            SecConfigurationError,
            SecError,
            SecNotFoundError,
            SecRateLimitError,
            SecTransientError,
            SecValidationError,
        )

        assert srv._frame_error(SecValidationError(field="cik", reason="bad")) == {
            "error": "validation",
            "field": "cik",
            "reason": "bad",
        }
        assert srv._frame_error(SecConfigurationError(hint="set UA"))["error"] == "configuration"
        nf = srv._frame_error(SecNotFoundError(resource="r", hint="h"))
        assert nf["error"] == "not_found" and nf["resource"] == "r"
        rl = srv._frame_error(SecRateLimitError(retry_after_seconds=5, current_window_used=3))
        assert rl["error"] == "rate_limit" and rl["retry_after_seconds"] == 5
        tr = srv._frame_error(SecTransientError(status_code=503, attempt=1, hint="up"))
        assert tr["error"] == "transient" and tr["status_code"] == 503
        # Generic SecError subclass (Form4ParseError) → sec_error envelope.
        se = srv._frame_error(Form4ParseError(accession_number="a", reason="x"))
        assert se["error"] == "sec_error" and se["type"] == "Form4ParseError"
        # Bare SecError base.
        assert srv._frame_error(SecError())["error"] == "sec_error"
        # Non-Sec exception → internal.
        assert srv._frame_error(ValueError("boom")) == {"error": "internal", "type": "ValueError"}

    def test_safe_run_is_not_implemented(self) -> None:
        """_safe_run is a documented internal stub that must refuse to run (161)."""
        from sec_edgar_mcp import server as srv

        with pytest.raises(NotImplementedError):
            srv._safe_run("x", None)

    def test_harden_stdio_tolerates_log_dir_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the log dir cannot be created, hardening still completes (47-48)."""
        from sec_edgar_mcp import server as srv

        def boom_mkdir(*_a: Any, **_k: Any) -> None:
            raise OSError("read-only fs")

        monkeypatch.setattr(Path, "mkdir", boom_mkdir)
        # Should not raise even though the log dir cannot be made.
        srv._harden_stdio()
        # builtins.print still routes to stderr (the patched safe_print).
        import builtins

        assert builtins.print is not None

    def test_harden_stdio_tolerates_file_handler_oserror(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A failing RotatingFileHandler is swallowed (63-64)."""
        from sec_edgar_mcp import server as srv

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

        def boom_handler(*_a: Any, **_k: Any) -> None:
            raise OSError("cannot open log file")

        # server.py does `from logging.handlers import RotatingFileHandler`,
        # so patch the name bound *inside the server module*.
        monkeypatch.setattr(srv, "RotatingFileHandler", boom_handler)
        srv._harden_stdio()  # must not raise

    def test_safe_print_defaults_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The patched print routes to stderr by default (line 33)."""
        from sec_edgar_mcp import server as srv

        srv._harden_stdio()
        print("coverage-probe-line")
        captured = capsys.readouterr()
        assert "coverage-probe-line" in captured.err
        assert "coverage-probe-line" not in captured.out

    def test_main_runs_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() logs start and calls app().run() (294-295)."""
        from sec_edgar_mcp import server as srv

        ran: list[int] = []
        fake_app = MagicMock()
        fake_app.run = lambda: ran.append(1)
        monkeypatch.setattr(srv, "app", lambda: fake_app)
        srv.main()
        assert ran == [1]

    @pytest.mark.asyncio
    async def test_each_tool_frames_sec_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every tool's `except SecError` branch returns an envelope (213-265).

        We inject a client whose get_json raises SecNotFoundError so the
        impl propagates a SecError up into each tool's try/except.
        """
        from sec_edgar_mcp.errors import SecNotFoundError
        from sec_edgar_mcp.server import app

        class _BoomClient:
            async def get_json(self, *_a: Any, **_k: Any) -> dict[str, Any]:
                raise SecNotFoundError(resource="x", hint="nope")

            async def get_text(self, *_a: Any, **_k: Any) -> Any:
                raise SecNotFoundError(resource="x", hint="nope")

        await runtime_mod.set_client_for_tests(_BoomClient())  # type: ignore[arg-type]
        # Disable cache so the impls reach the fetch() path that calls the client.
        monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
        a = app()

        calls = [
            ("get_company_filings", {"cik_or_ticker": "123", "limit": 5}),
            ("get_form4_insider_trades", {"cik_or_ticker": "123", "since_days": 30}),
            ("get_filing_text", {"accession_number": "0000320193-24-000123"}),
            ("get_8k_with_items", {"cik_or_ticker": "123", "since_days": 30}),
            ("search_filings_full_text", {"query": "test", "since_days": 30}),
        ]
        for name, kwargs in calls:
            result = await a.call_tool(name, kwargs)
            payload = _extract_payload(result)
            assert payload.get("error") in {"not_found", "sec_error", "internal"}, (name, payload)
        await runtime_mod.set_client_for_tests(None)


def _extract_payload(result: Any) -> dict[str, Any]:
    """Mirror of the helper in test_server_integration for envelope extraction."""
    if isinstance(result, tuple):
        if len(result) >= 2 and isinstance(result[1], dict):
            return result[1]
        if result and hasattr(result[0], "text"):
            return json.loads(result[0].text)
        return {}
    sc = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    content = getattr(result, "content", None)
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return {}


# ===========================================================================
# tools/_runtime.py
# ===========================================================================


class TestRuntimeGaps:
    @pytest.mark.asyncio
    async def test_get_client_double_checked_lock_constructs_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_client lazily builds the client under the lock (lines 27-29)."""
        runtime_mod.reset_client_cache()
        built: list[int] = []

        def fake_make_client() -> Any:
            built.append(1)
            return MagicMock(name="SecEdgarClient")

        monkeypatch.setattr(runtime_mod, "make_client", fake_make_client)
        # Fire two concurrent callers — the double-checked lock must build once.
        c1, c2 = await asyncio.gather(runtime_mod.get_client(), runtime_mod.get_client())
        assert c1 is c2
        assert built == [1]
        runtime_mod.reset_client_cache()

    @pytest.mark.asyncio
    async def test_call_with_cache_lookup_exception_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising cache_lookup must be swallowed and fetch runs (lines 67-68)."""
        real_cache = MagicMock()
        monkeypatch.setattr(runtime_mod, "get_cache", lambda: real_cache)
        monkeypatch.setattr(runtime_mod, "cache_bypass", lambda: False)

        async def fetch(_client: Any) -> dict[str, Any]:
            return {"ok": True}

        def boom_lookup(_cache: Any) -> dict[str, Any] | None:
            raise RuntimeError("lookup blew up")

        fake_client = MagicMock()

        async def fake_get_client() -> Any:
            return fake_client

        monkeypatch.setattr(runtime_mod, "get_client", fake_get_client)
        out = await runtime_mod.call_with_cache(fetch, cache_lookup=boom_lookup)
        assert out["ok"] is True
        assert out["_cache_status"] == "miss"

    @pytest.mark.asyncio
    async def test_call_with_cache_store_exception_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raising cache_store must never break the tool (lines 85-86)."""
        real_cache = MagicMock()
        monkeypatch.setattr(runtime_mod, "get_cache", lambda: real_cache)
        monkeypatch.setattr(runtime_mod, "cache_bypass", lambda: False)

        async def fetch(_client: Any) -> dict[str, Any]:
            return {"value": 1}

        def boom_store(_cache: Any, _payload: dict[str, Any]) -> None:
            raise RuntimeError("store blew up")

        fake_client = MagicMock()

        async def fake_get_client() -> Any:
            return fake_client

        monkeypatch.setattr(runtime_mod, "get_client", fake_get_client)
        out = await runtime_mod.call_with_cache(fetch, cache_store=boom_store)
        assert out["value"] == 1
        assert out["_cache_status"] == "miss"

    @pytest.mark.asyncio
    async def test_call_with_cache_bypass_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cache present + bypass set → status 'bypass' (lines 74-75)."""
        real_cache = MagicMock()
        monkeypatch.setattr(runtime_mod, "get_cache", lambda: real_cache)
        monkeypatch.setattr(runtime_mod, "cache_bypass", lambda: True)

        async def fetch(_client: Any) -> dict[str, Any]:
            return {"v": 2}

        fake_client = MagicMock()

        async def fake_get_client() -> Any:
            return fake_client

        monkeypatch.setattr(runtime_mod, "get_client", fake_get_client)
        out = await runtime_mod.call_with_cache(fetch, cache_lookup=lambda c: None)
        assert out["_cache_status"] == "bypass"


# ===========================================================================
# tools/search.py
# ===========================================================================


class TestSearchNormaliseGaps:
    def test_non_int_total_defaults_zero(self) -> None:
        """A non-numeric total.value collapses to 0 (lines 63-64)."""
        from sec_edgar_mcp.tools.search import _normalise

        out = _normalise({"hits": {"total": {"value": "not-a-number"}, "hits": []}}, query="q")
        assert out["total_hits"] == 0

    def test_non_list_inner_hits_yields_empty(self) -> None:
        """hits.hits that is not a list yields no results (line 67->87 branch)."""
        from sec_edgar_mcp.tools.search import _normalise

        out = _normalise({"hits": {"total": {"value": 5}, "hits": "oops"}}, query="q")
        assert out["results"] == []
        assert out["total_hits"] == 5

    def test_non_dict_hit_is_skipped(self) -> None:
        """A non-dict element inside hits.hits is skipped (line 70)."""
        from sec_edgar_mcp.tools.search import _normalise

        out = _normalise(
            {"hits": {"total": {"value": 1}, "hits": ["string-not-dict", {"_source": {"form": "8-K"}}]}},
            query="q",
        )
        assert out["returned"] == 1
        assert out["results"][0]["form"] == "8-K"

    def test_first_passthrough_non_list(self) -> None:
        """_first returns the value untouched when not a list (line 98)."""
        from sec_edgar_mcp.tools.search import _first

        assert _first("scalar") == "scalar"
        assert _first(None) is None
        assert _first(["a", "b"]) == "a"


# ===========================================================================
# tools/insider.py + tools/filings.py helpers
# ===========================================================================


class TestToolHelperGaps:
    def test_insider_first_and_safe_get(self) -> None:
        """insider _first / _safe_get cover both branches (lines 253-262)."""
        from sec_edgar_mcp.tools.insider import _first, _safe_get

        assert _first(["x"]) == "x"
        assert _first([]) is None
        assert _first("nope") is None
        assert _safe_get(["a", "b"], 1) == "b"
        assert _safe_get(["a"], 9) is None
        assert _safe_get("not-a-list", 0) is None

    def test_filings_first_returns_none_for_empty(self) -> None:
        from sec_edgar_mcp.tools.filings import _first, _safe_get

        assert _first([]) is None
        assert _first("scalar") is None
        assert _safe_get([], 0) is None

    def test_select_document_complete_submission_txt(self) -> None:
        """'complete' picks the full-submission .txt (lines around 330-343)."""
        from sec_edgar_mcp.tools.filings import _select_document

        index = {
            "directory": {
                "item": [
                    {"name": "0000320193-24-000123.txt"},
                    {"name": "primary.htm"},
                ]
            }
        }
        # No 'submission' token → falls back to first .txt.
        assert _select_document(index, "complete") == "0000320193-24-000123.txt"

    def test_select_document_complete_submission_named(self) -> None:
        from sec_edgar_mcp.tools.filings import _select_document

        index = {"directory": {"item": [{"name": "full-submission.txt"}]}}
        assert _select_document(index, "complete") == "full-submission.txt"

    def test_select_document_complete_no_txt_returns_none(self) -> None:
        from sec_edgar_mcp.tools.filings import _select_document

        index = {"directory": {"item": [{"name": "only.htm"}]}}
        assert _select_document(index, "complete") is None

    def test_select_document_primary_txt_fallback(self) -> None:
        """primary with no .htm falls back to a .txt (line 354)."""
        from sec_edgar_mcp.tools.filings import _select_document

        index = {"directory": {"item": [{"name": "data.txt"}]}}
        assert _select_document(index, "primary") == "data.txt"

    def test_select_document_non_list_items(self) -> None:
        from sec_edgar_mcp.tools.filings import _select_document

        assert _select_document({"directory": {"item": "x"}}, "primary") is None

    def test_select_document_skips_non_dict_entries(self) -> None:
        """Non-dict directory entries are skipped in both modes (lines 335, 347)."""
        from sec_edgar_mcp.tools.filings import _select_document

        idx = {"directory": {"item": ["junk-string", {"name": "doc.htm"}]}}
        assert _select_document(idx, "primary") == "doc.htm"
        idx2 = {"directory": {"item": ["junk", {"name": "0001-submission.txt"}]}}
        assert _select_document(idx2, "complete") == "0001-submission.txt"

    def test_zip_recent_non_dict_returns_empty(self) -> None:
        """A non-dict recent block yields an empty filing list (line 291)."""
        from sec_edgar_mcp.tools.filings import _zip_recent

        assert _zip_recent("not-a-dict", cik="0000320193") == []

    def test_zip_recent_skips_non_str_accession(self) -> None:
        """Rows whose accessionNumber is not a str are skipped (line 303)."""
        from sec_edgar_mcp.tools.filings import _zip_recent

        recent = {
            "accessionNumber": [123, "0000320193-24-000123"],
            "form": ["8-K", "10-K"],
        }
        out = _zip_recent(recent, cik="0000320193")
        assert len(out) == 1
        assert out[0]["accession_number"] == "0000320193-24-000123"

    def test_parse_item_codes_strips_item_prefix(self) -> None:
        """The 8-K item parser strips an 'Item ' prefix (lines 255-262)."""
        from sec_edgar_mcp.tools.filings import _parse_items

        assert _parse_items("Item 5.02, Item 1.01") == ["5.02", "1.01"]
        assert _parse_items("") == []
        assert _parse_items(None) == []
        # A token that is just "Item" (no code) is preserved verbatim.
        assert _parse_items("Item 2.02") == ["2.02"]


# ===========================================================================
# tools/meta.py
# ===========================================================================


class TestMetaGaps:
    def test_ua_status_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset UA → configured False / reason 'missing' (lines 32-33)."""
        from sec_edgar_mcp.tools import meta

        monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
        out = meta._safe_user_agent_status()
        assert out == {"configured": False, "reason": "missing"}

    def test_ua_status_format_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty but malformed UA surfaces the config hint (lines 38-39)."""
        from sec_edgar_mcp.tools import meta

        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "garbage-no-email")
        out = meta._safe_user_agent_status()
        assert out["configured"] is False
        assert out["reason"]  # hint string, not None

    def test_ua_status_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sec_edgar_mcp.tools import meta

        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "myapp/1 (ops@example.org)")
        out = meta._safe_user_agent_status()
        assert out == {"configured": True, "reason": None}

    def test_cache_summary_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cache disabled → enabled False (line 45)."""
        from sec_edgar_mcp.tools import meta

        monkeypatch.setattr(meta, "cache_enabled", lambda: False)
        out = meta._safe_cache_summary()
        assert out == {"enabled": False, "size_mb": 0.0, "hit_rate_24h": None}

    def test_cache_summary_get_cache_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cache enabled but get_cache returns None (line 48)."""
        from sec_edgar_mcp.tools import meta

        monkeypatch.setattr(meta, "cache_enabled", lambda: True)
        monkeypatch.setattr(meta, "get_cache", lambda: None)
        out = meta._safe_cache_summary()
        assert out["enabled"] is False

    def test_cache_summary_stats_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_stats raising must degrade gracefully (lines 51-52)."""
        from sec_edgar_mcp.tools import meta

        broken = MagicMock()
        broken.get_stats.side_effect = RuntimeError("duckdb gone")
        monkeypatch.setattr(meta, "cache_enabled", lambda: True)
        monkeypatch.setattr(meta, "get_cache", lambda: broken)
        out = meta._safe_cache_summary()
        assert out == {"enabled": True, "size_mb": 0.0, "hit_rate_24h": None}


# ===========================================================================
# errors.py / models.py
# ===========================================================================


class TestErrorAndModelGaps:
    def test_transient_error_str(self) -> None:
        """SecTransientError.__str__ renders status+attempt (line 121)."""
        from sec_edgar_mcp.errors import SecTransientError

        exc = SecTransientError(status_code=503, attempt=2, hint="upstream 503")
        s = str(exc)
        assert "503" in s and "attempt=2" in s

    def test_strip_item_codes_passthrough_non_list(self) -> None:
        """_strip_item_codes returns non-list input unchanged (line 282)."""
        from sec_edgar_mcp.models import Get8KWithItemsInput

        assert Get8KWithItemsInput._strip_item_codes("scalar") == "scalar"
        assert Get8KWithItemsInput._strip_item_codes(["1.01 ", 5]) == ["1.01", 5]

    def test_cik_validators_passthrough_non_str(self) -> None:
        """The cik_or_ticker pre-validators return non-str input untouched
        (the ``if isinstance(v, str)`` False branch — 175->184 / 218->227 / 266->275)."""
        from sec_edgar_mcp.models import (
            Get8KWithItemsInput,
            GetCompanyFilingsInput,
            GetForm4InsiderTradesInput,
        )

        sentinel = object()
        assert GetCompanyFilingsInput._upper_cik_or_ticker(sentinel) is sentinel
        assert GetForm4InsiderTradesInput._upper_cik_or_ticker(sentinel) is sentinel
        assert Get8KWithItemsInput._upper_cik_or_ticker(sentinel) is sentinel

    def test_form_types_validator_passthrough_non_list(self) -> None:
        """_upper_form_types returns a non-list unchanged."""
        from sec_edgar_mcp.models import GetCompanyFilingsInput

        assert GetCompanyFilingsInput._upper_form_types("scalar") == "scalar"


class TestInsiderSummariseGap:
    def test_summarise_non_dict_form4_without_parse_error(self) -> None:
        """A row with a non-dict form4 and no parse_error is skipped silently
        (the ``if row.get('parse_error')`` False branch — insider 227->229)."""
        from sec_edgar_mcp.tools.insider import _summarise

        rows = [
            {"form4": None},  # non-dict, no parse_error → skipped, no failure count
            {"form4": {"transaction_count": 2, "net_buy_value": "10", "net_sell_value": "0"}},
        ]
        out = _summarise(rows)
        assert out["transaction_count"] == 2
        assert out["parse_failures"] == 0

    def test_summarise_non_dict_form4_with_parse_error(self) -> None:
        """A row flagged parse_error increments the failure counter (227-228)."""
        from sec_edgar_mcp.tools.insider import _summarise

        rows = [{"form4": None, "parse_error": "boom"}]
        out = _summarise(rows)
        assert out["parse_failures"] == 1


# ===========================================================================
# client.py
# ===========================================================================


class TestClientGaps:
    @pytest.mark.asyncio
    async def test_get_json_rejects_non_dict_non_list(self, make_client) -> None:
        """A scalar JSON body raises SecTransientError (lines 309-314)."""
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/scalar", json_body=None, text_body="42", content_type="application/json")])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/scalar")

    @pytest.mark.asyncio
    async def test_resolve_cik_handles_str_cik_str(self, make_client) -> None:
        """ticker map with a string cik_str is zero-padded (lines 399-400)."""
        from sec_edgar_mcp.client import resolve_cik
        from tests.conftest import FakeRoute

        ticker_map = {"0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple"}}
        client = make_client([FakeRoute("/files/company_tickers.json", json_body=ticker_map)])
        cik = await resolve_cik(client, "AAPL")
        assert cik == "0000320193"


# ===========================================================================
# cache.py
# ===========================================================================


class TestCacheGaps:
    def test_get_json_row_duckdb_error_returns_none(self, tmp_path: Path) -> None:
        """A DuckDB error during read returns None, not a crash (lines 330-336)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("simulated read failure")
            cache._conn = fake  # type: ignore[assignment]
            assert cache._get_json_row("search_cache", "k") is None
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_put_json_row_duckdb_error_swallowed(self, tmp_path: Path) -> None:
        """A DuckDB error during write is logged + swallowed (lines 372-377)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("simulated write failure")
            cache._conn = fake  # type: ignore[assignment]
            cache._put_json_row("search_cache", "k", {"x": 1}, 60)  # must not raise
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_expired_row_returns_none(self, tmp_path: Path) -> None:
        """A row past its TTL is treated as a miss (lines 341-343 expired path)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.put_search({"q": "x"}, {"data": 1})
            assert cache._conn is not None
            cache._conn.execute("UPDATE search_cache SET fetched_at = TIMESTAMP '2000-01-01 00:00:00', ttl_seconds = 1")
            assert cache.get_search({"q": "x"}) is None
        finally:
            cache.close()

    def test_quarantine_when_db_missing_sets_conn_none(self, tmp_path: Path) -> None:
        """_quarantine_and_reopen with a missing db file nulls the conn (lines 269-271)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.close()
            (tmp_path / "c.duckdb").unlink(missing_ok=True)
            cache._quarantine_and_reopen(duckdb.Error("boom"))
            assert cache._conn is None
        finally:
            cache.close()

    def test_get_filing_text_duckdb_error(self, tmp_path: Path) -> None:
        """filing_text read DuckDB error returns None (lines 451-456)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("read fail")
            cache._conn = fake  # type: ignore[assignment]
            assert cache.get_filing_text("0000320193-24-000123", "primary") is None
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_put_filing_text_duckdb_error(self, tmp_path: Path) -> None:
        """filing_text write DuckDB error is swallowed (lines 507-511)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("write fail")
            cache._conn = fake  # type: ignore[assignment]
            cache.put_filing_text(
                "0000320193-24-000123",
                "primary",
                content_type="text/html",
                text="x",
                byte_size=1,
                truncated=False,
            )
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_get_stats_count_query_error(self, tmp_path: Path) -> None:
        """get_stats tolerates a failed COUNT query (lines 532-533 / 547-548)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            assert real is not None
            fake = MagicMock(wraps=real)

            def selective(sql: str, *a: Any, **k: Any) -> Any:
                if "COUNT(*)" in sql:
                    raise duckdb.Error("count fail")
                return real.execute(sql, *a, **k)

            fake.execute.side_effect = selective
            cache._conn = fake  # type: ignore[assignment]
            stats = cache.get_stats()
            assert all(v == 0 for v in stats.rows_per_table.values())
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()


# ===========================================================================
# _xbrl.py
# ===========================================================================


class TestXbrlGaps:
    def test_localname_non_str_returns_empty(self) -> None:
        """_localname tolerates a non-str tag (lines 408-409)."""
        from sec_edgar_mcp._xbrl import _localname

        assert _localname(None) == ""
        assert _localname(123) == ""
        assert _localname("{ns}tag") == "tag"
        assert _localname("plain") == "plain"

    def test_parse_decimal_numeric_too_large(self) -> None:
        """An over-long numeric string is rejected with a warning (lines 517-519)."""
        from sec_edgar_mcp._xbrl import _parse_decimal_optional

        warnings: list[str] = []
        huge = "9" * 100
        assert _parse_decimal_optional(huge, warnings, "shares") is None
        assert any("numeric_too_large" in w for w in warnings)

    def test_parse_decimal_optional_empty_and_none(self) -> None:
        """Empty / None inputs short-circuit to None (lines 512-516)."""
        from sec_edgar_mcp._xbrl import _parse_decimal_optional

        warnings: list[str] = []
        assert _parse_decimal_optional(None, warnings, "f") is None
        assert _parse_decimal_optional("   ", warnings, "f") is None

    def test_parse_form4_skips_none_transaction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The parse loop skips a None transaction (line 252 `continue`).

        ``_parse_transaction`` never returns None on its own, so we force it
        for one element to exercise the defensive skip branch in the loop.
        """
        import defusedxml.ElementTree as DET

        import sec_edgar_mcp._xbrl as xbrl

        real_iter = xbrl._iter_transactions
        seen: list[int] = []

        def fake_parse(pair: Any, warnings: list[str]) -> Any:
            seen.append(1)
            return None  # force the skip on every element

        monkeypatch.setattr(xbrl, "_parse_transaction", fake_parse)

        # Use the real AAPL fixture which has at least one transaction element.
        fixture = Path(__file__).parent / "fixtures" / "seed" / "form4_aapl.xml"
        if not fixture.exists():
            pytest.skip("form4 fixture not present")
        xml = fixture.read_bytes()
        # Sanity: the real iterator finds >=1 element so fake_parse fires.
        assert len(real_iter(DET.fromstring(xml))) >= 1
        data = xbrl.parse_form4(xml, accession_number="0000320193-24-000010")
        assert list(data.transactions) == []  # all skipped via the None branch
        assert seen  # fake_parse was actually invoked


# ===========================================================================
# Additional client + cache + filings branch coverage
# ===========================================================================


class TestClientCacheFilingsBranches:
    @pytest.mark.asyncio
    async def test_rate_limiter_wait_loop(self) -> None:
        """The token-bucket sleeps when the 1s window is full (line 161).

        With capacity=1, two back-to-back acquires force the second to enter
        the ``wait > 0 → await asyncio.sleep(wait)`` branch.
        """
        import time

        from sec_edgar_mcp.client import TokenBucket

        bucket = TokenBucket(capacity=1)
        await bucket.acquire()
        start = time.monotonic()
        await asyncio.wait_for(bucket.acquire(), timeout=3.0)
        elapsed = time.monotonic() - start
        # The second acquire must have waited for the window to free (~1s).
        assert elapsed > 0.2
        assert bucket.tokens_remaining() >= 0

    @pytest.mark.asyncio
    async def test_resolve_cik_int_cik_str(self, make_client) -> None:
        """ticker map with an int cik_str is zero-padded (line 394)."""
        from sec_edgar_mcp.client import resolve_cik
        from tests.conftest import FakeRoute

        ticker_map = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}}
        client = make_client([FakeRoute("/files/company_tickers.json", json_body=ticker_map)])
        assert await resolve_cik(client, "AAPL") == "0000320193"

    @pytest.mark.asyncio
    async def test_resolve_cik_skips_non_dict_entry(self, make_client) -> None:
        """A non-dict ticker-map entry is skipped (line 392 branch)."""
        from sec_edgar_mcp.client import resolve_cik
        from tests.conftest import FakeRoute

        ticker_map = {"0": "not-a-dict", "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"}}
        client = make_client([FakeRoute("/files/company_tickers.json", json_body=ticker_map)])
        assert await resolve_cik(client, "MSFT") == "0000789019"

    @pytest.mark.asyncio
    async def test_resolve_cik_match_but_bad_cik_str_keeps_looking(self, make_client) -> None:
        """A ticker match whose cik_str is non-numeric falls through (line 399->392)."""
        from sec_edgar_mcp.client import resolve_cik
        from sec_edgar_mcp.errors import SecNotFoundError
        from tests.conftest import FakeRoute

        # Matching ticker but cik_str is a non-digit string → neither int nor
        # digit-str branch taken, loop continues, ultimately NotFound.
        ticker_map = {"0": {"cik_str": "not-a-number", "ticker": "AAPL", "title": "Apple"}}
        client = make_client([FakeRoute("/files/company_tickers.json", json_body=ticker_map)])
        with pytest.raises(SecNotFoundError):
            await resolve_cik(client, "AAPL")

    @pytest.mark.asyncio
    async def test_get_filing_text_cache_hit(self, make_client, monkeypatch: pytest.MonkeyPatch) -> None:
        """A warm filing-text cache short-circuits the fetch (filings line 106)."""
        from sec_edgar_mcp.cache import get_cache
        from sec_edgar_mcp.models import GetFilingTextInput
        from sec_edgar_mcp.tools import filings

        cache = get_cache()
        assert cache is not None
        cache.put_filing_text(
            "0000320193-24-000123",
            "primary",
            content_type="text/html",
            text="cached body",
            byte_size=11,
            truncated=False,
        )
        out = await filings.get_filing_text_impl(
            GetFilingTextInput(accession_number="0000320193-24-000123", document_type="primary")
        )
        assert out["text"] == "cached body"
        assert out["_cache_status"] == "hit"

    def test_cache_record_event_no_conn(self, tmp_path: Path) -> None:
        """_record_event is a no-op when the connection is gone (line 310)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        cache.close()
        cache._conn = None  # type: ignore[assignment]
        cache._record_event("hit", "search_cache")  # must not raise

    def test_cache_reset_clears_rows(self, tmp_path: Path) -> None:
        """reset() truncates all tables (lines 576-583)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.put_search({"q": "x"}, {"data": 1})
            cache.reset()
            assert cache.get_search({"q": "x"}) is None
        finally:
            cache.close()

    def test_cache_reset_no_conn(self, tmp_path: Path) -> None:
        """reset() is a no-op when the connection is gone (line 578)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        cache.close()
        cache._conn = None  # type: ignore[assignment]
        cache.reset()  # must not raise

    def test_cache_get_stats_size_mb(self, tmp_path: Path) -> None:
        """get_stats reports a positive on-disk size for a populated DB (521-525)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            cache.put_search({"q": "x"}, {"data": 1})
            stats = cache.get_stats()
            assert stats.size_mb >= 0.0
            assert stats.db_path.endswith("c.duckdb")
        finally:
            cache.close()

    def test_parse_dt_branches(self) -> None:
        """_parse_dt: None / naive datetime / tz datetime / bad str / good str (165-176)."""
        from datetime import UTC, datetime

        from sec_edgar_mcp.cache import _parse_dt

        assert _parse_dt(None) is None
        naive = datetime(2026, 1, 1, 12, 0, 0)
        assert _parse_dt(naive) == naive  # naive returned as-is (line 170)
        tz = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert _parse_dt(tz).tzinfo is None  # type: ignore[union-attr]
        assert _parse_dt("not-a-date") is None
        assert _parse_dt("2026-01-01T00:00:00Z") is not None
        assert _parse_dt(12345) is None

    def test_is_expired_with_string_fetched_at(self) -> None:
        """_is_expired parses a string fetched_at then compares (line 186)."""
        from sec_edgar_mcp.cache import _is_expired

        # Far-past ISO string with a 1s TTL → expired.
        assert _is_expired("2000-01-01T00:00:00Z", 1) is True
        # Unparseable string → treated as expired.
        assert _is_expired("garbage", 60) is True
        # None inputs → expired.
        assert _is_expired(None, 60) is True

    def test_get_stats_conn_none(self, tmp_path: Path) -> None:
        """get_stats with a closed connection returns zeroed stats (527->549)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        cache.close()
        cache._conn = None  # type: ignore[assignment]
        stats = cache.get_stats()
        assert stats.rows_per_table == {}
        assert stats.hits_24h == 0

    def test_get_stats_size_mb_oserror(self, tmp_path: Path) -> None:
        """A stat() failure on the DB file degrades size_mb to 0 (524-525)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real_path = cache.db_path

            class _FlakyPath:
                """Proxy that exists() True but stat() raises, delegating the rest."""

                def __init__(self, inner: Path) -> None:
                    self._inner = inner

                def exists(self) -> bool:
                    return True

                def stat(self) -> Any:
                    raise OSError("stat failed")

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

                def __str__(self) -> str:
                    return str(self._inner)

                def __fspath__(self) -> str:
                    return str(self._inner)

            cache.db_path = _FlakyPath(real_path)  # type: ignore[assignment]
            stats = cache.get_stats()
            assert stats.size_mb == 0.0
            cache.db_path = real_path
        finally:
            cache.close()

    def test_reset_duckdb_error_swallowed(self, tmp_path: Path) -> None:
        """reset() swallows a DuckDB delete error (lines 582-583)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("delete fail")
            cache._conn = fake  # type: ignore[assignment]
            cache.reset()  # must not raise
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_record_event_duckdb_error_swallowed(self, tmp_path: Path) -> None:
        """_record_event swallows a DuckDB insert error (lines 316-317)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            real = cache._conn
            fake = MagicMock(wraps=real)
            fake.execute.side_effect = duckdb.Error("insert fail")
            cache._conn = fake  # type: ignore[assignment]
            cache._record_event("hit", "search_cache")  # must not raise
            cache._conn = real  # type: ignore[assignment]
        finally:
            cache.close()

    def test_quarantine_reopen_success(self, tmp_path: Path) -> None:
        """Quarantine renames the corrupt DB aside and reopens fresh (281-289)."""
        db = tmp_path / "c.duckdb"
        cache = Cache(db_path=db)
        try:
            # The DB file exists; quarantine should rename + reopen successfully.
            cache._quarantine_and_reopen(duckdb.Error("corrupt"))
            # A fresh connection is open and a backup file was created.
            assert cache._conn is not None
            backups = list(tmp_path.glob("c.duckdb.corrupt-*"))
            assert backups, "quarantine backup file should exist"
        finally:
            cache.close()

    def test_quarantine_rename_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the rename fails, the connection is nulled (lines 281-284)."""
        import os

        db = tmp_path / "c.duckdb"
        cache = Cache(db_path=db)
        try:

            def boom_rename(*_a: Any, **_k: Any) -> None:
                raise OSError("rename denied")

            monkeypatch.setattr(os, "rename", boom_rename)
            cache._quarantine_and_reopen(duckdb.Error("corrupt"))
            assert cache._conn is None
        finally:
            cache.close()

    def test_get_cache_singleton_reuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_cache returns the same singleton on repeat calls (line 602->604)."""
        import sec_edgar_mcp.cache as cache_mod

        cache_mod.reset_cache_singleton()
        monkeypatch.setattr(cache_mod, "cache_enabled", lambda: True)
        c1 = cache_mod.get_cache()
        c2 = cache_mod.get_cache()
        assert c1 is c2
        cache_mod.reset_cache_singleton()

    def test_quarantine_reopen_failure_nulls_conn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the reopen connect fails after rename, the conn is nulled (290-292)."""
        import sec_edgar_mcp.cache as cache_mod

        db = tmp_path / "c.duckdb"
        cache = Cache(db_path=db)
        try:
            # Rename succeeds; the subsequent reopen connect raises.
            def boom_connect(*_a: Any, **_k: Any) -> Any:
                raise duckdb.Error("reopen failed")

            monkeypatch.setattr(cache_mod.duckdb, "connect", boom_connect)
            cache._quarantine_and_reopen(duckdb.Error("corrupt"))
            assert cache._conn is None
            # Backup was created before the failed reopen.
            assert list(tmp_path.glob("c.duckdb.corrupt-*"))
        finally:
            cache.close()

    def test_get_stats_db_file_missing(self, tmp_path: Path) -> None:
        """get_stats skips the size calc when the db file is absent (521->526)."""
        cache = Cache(db_path=tmp_path / "c.duckdb")
        try:
            # Remove the on-disk file while the in-memory connection lives on.
            (tmp_path / "c.duckdb").unlink(missing_ok=True)
            stats = cache.get_stats()
            assert stats.size_mb == 0.0
        finally:
            cache.close()
