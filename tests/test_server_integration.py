"""Server integration tests — exercise the FastMCP wiring with a fake client.

We do NOT bring up real stdio here (that would require a subprocess); we
exercise the wired tool callables directly via the in-process FastMCP
app object.  Those callables are the same Python coroutines exposed
on stdio, so this catches the wiring layer without needing the JSON-RPC
framing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.server import SERVER_VERSION, app
from tests.conftest import FIXTURE_DIR, FakeRoute


def _seed_routes(fixture_dir: Path) -> list[FakeRoute]:
    tickers = json.loads((fixture_dir / "company_tickers.json").read_text(encoding="utf-8"))
    submissions = json.loads((fixture_dir / "submissions_aapl.json").read_text(encoding="utf-8"))
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    submissions["filings"]["recent"]["filingDate"] = [
        (today - timedelta(days=10)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=45)).isoformat(),
    ]
    index = json.loads((fixture_dir / "index_aapl_10k.json").read_text(encoding="utf-8"))
    body = (fixture_dir / "aapl_10k.htm").read_text(encoding="utf-8")
    search = json.loads((fixture_dir / "search_cybersecurity.json").read_text(encoding="utf-8"))
    return [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=submissions),
        FakeRoute("-index.json", json_body=index),
        FakeRoute("aapl-20240928.htm", text_body=body, content_type="text/html"),
        FakeRoute("/LATEST/search-index", json_body=search),
    ]


@pytest.mark.asyncio
async def test_app_exports_seven_tools() -> None:
    a = app()
    tools = await a.list_tools()
    names = {t.name for t in tools}
    assert names == {
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
    }


@pytest.mark.asyncio
async def test_call_get_company_filings_through_app(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool(
        "get_company_filings",
        {"cik_or_ticker": "AAPL", "limit": 5},
    )
    # FastMCP returns CallToolResult-like; the structured payload lives
    # in result[1] for the modern signature, or result.structuredContent
    # for older ones.  Be defensive.
    payload = _extract_payload(result)
    assert payload["company"]["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_call_health_check_through_app() -> None:
    a = app()
    result = await a.call_tool("health_check", {})
    payload = _extract_payload(result)
    assert "rate_limit_hard_cap" in payload


@pytest.mark.asyncio
async def test_call_get_server_info_through_app() -> None:
    a = app()
    result = await a.call_tool("get_server_info", {})
    payload = _extract_payload(result)
    assert payload["server_version"] == SERVER_VERSION
    assert len(payload["supported_tools"]) == 10


def test_initialize_reports_release_tag_version() -> None:
    """``serverInfo.version`` must report the project's release tag
    (``sec_edgar_mcp.__version__``), NOT the underlying mcp Python SDK
    framework version (e.g. ``1.27.1``).

    Regression test — FastMCP's ctor does not accept a ``version=`` kwarg,
    so the lowlevel ``Server.version`` defaults to ``None`` and the server
    falls back to ``importlib.metadata.version("mcp")``.  ``server.py``
    must explicitly assign ``mcp_app._mcp_server.version = SERVER_VERSION``
    so the ``initialize`` response carries the project tag.
    """
    from sec_edgar_mcp import __version__ as expected_version

    a = app()
    # Probe the lowlevel ``Server`` instance that backs FastMCP — this is
    # exactly the value the ``initialize`` handler reads when constructing
    # the ``serverInfo.version`` field of the JSON-RPC response.
    init_options = a._mcp_server.create_initialization_options()
    assert init_options.server_name == "sec-edgar-mcp"
    assert init_options.server_version == expected_version, (
        f"server_version={init_options.server_version!r} should equal "
        f"package __version__={expected_version!r}; if it equals the "
        "mcp SDK version, the FastMCP._mcp_server.version override "
        "in server.py was lost."
    )


@pytest.mark.asyncio
async def test_call_search_through_app(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool(
        "search_filings_full_text",
        {"query": "cybersecurity", "since_days": 30},
    )
    payload = _extract_payload(result)
    assert payload["total_hits"] == 2


@pytest.mark.asyncio
async def test_call_get_filing_text_through_app(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool(
        "get_filing_text",
        {"accession_number": "0000320193-24-000123"},
    )
    payload = _extract_payload(result)
    assert "Apple Inc." in payload["text"]


@pytest.mark.asyncio
async def test_call_get_form4_through_app(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool(
        "get_form4_insider_trades",
        {"cik_or_ticker": "AAPL", "since_days": 365},
    )
    payload = _extract_payload(result)
    assert payload["count"] == 1


@pytest.mark.asyncio
async def test_call_get_8k_with_items_through_app(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool(
        "get_8k_with_items",
        {"cik_or_ticker": "AAPL", "item_codes": ["5.02"], "since_days": 365},
    )
    payload = _extract_payload(result)
    assert payload["count"] == 1
    assert payload["filings"][0]["items"] == ["5.02"]


def _seed_13f_routes() -> list[FakeRoute]:
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    subs = json.loads((FIXTURE_DIR / "submissions_13f_manager.json").read_text(encoding="utf-8"))
    index = json.loads((FIXTURE_DIR / "index_13f.json").read_text(encoding="utf-8"))
    info_table = (FIXTURE_DIR / "form13f_info_table.xml").read_text(encoding="utf-8")
    return [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=subs),
        FakeRoute("-index.json", json_body=index),
        FakeRoute("form13fInfoTable.xml", text_body=info_table, content_type="text/xml"),
    ]


@pytest.mark.asyncio
async def test_call_get_13f_holdings_through_app(make_client) -> None:
    client = make_client(_seed_13f_routes())
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_13f_holdings", {"cik_or_ticker": "1067983"})
    payload = _extract_payload(result)
    assert payload["holding_count"] == 3
    assert payload["manager"]["cik"] == "0001067983"


@pytest.mark.asyncio
async def test_call_get_institutional_holders_through_app(make_client) -> None:
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    subs = json.loads((FIXTURE_DIR / "submissions_aapl.json").read_text(encoding="utf-8"))
    search = json.loads((FIXTURE_DIR / "search_13f_holders.json").read_text(encoding="utf-8"))
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=subs),
        FakeRoute("/LATEST/search-index", json_body=search),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_institutional_holders", {"ticker": "AAPL"})
    payload = _extract_payload(result)
    assert payload["holder_count"] == 2


@pytest.mark.asyncio
async def test_call_get_proxy_statement_through_app(make_client) -> None:
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    subs = json.loads((FIXTURE_DIR / "submissions_proxy.json").read_text(encoding="utf-8"))
    body = (FIXTURE_DIR / "def14a_apple.htm").read_text(encoding="utf-8")
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=subs),
        FakeRoute("def14a-2026.htm", text_body=body, content_type="text/html"),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_proxy_statement", {"cik_or_ticker": "AAPL"})
    payload = _extract_payload(result)
    assert payload["proxy"]["meeting_date"] == "May 15, 2026"


@pytest.mark.asyncio
async def test_13f_not_found_framed(make_client) -> None:
    """A 13F manager with no 13F filings surfaces a not_found error frame."""
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body={"name": "X", "filings": {"recent": {}}}),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_13f_holdings", {"cik_or_ticker": "AAPL"})
    payload = _extract_payload(result)
    assert payload.get("error") == "not_found"


@pytest.mark.asyncio
async def test_proxy_not_found_framed(make_client) -> None:
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body={"name": "X", "filings": {"recent": {}}}),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_proxy_statement", {"cik_or_ticker": "AAPL"})
    payload = _extract_payload(result)
    assert payload.get("error") == "not_found"


@pytest.mark.asyncio
async def test_institutional_holders_search_error_framed(make_client) -> None:
    """A search transport failure surfaces as a SecError frame, not a crash."""
    tickers = json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    subs = json.loads((FIXTURE_DIR / "submissions_aapl.json").read_text(encoding="utf-8"))
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=subs),
        FakeRoute("/LATEST/search-index", status_code=404),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    a = app()
    result = await a.call_tool("get_institutional_holders", {"ticker": "AAPL"})
    payload = _extract_payload(result)
    assert payload.get("error") == "not_found"


@pytest.mark.asyncio
async def test_validation_error_framed(make_client) -> None:
    """SecValidationError from form_types must surface as a SecError frame
    (not raise an unhandled exception out of the tool)."""
    client = make_client([])
    await runtime_mod.set_client_for_tests(client)
    a = app()
    # The tool catches SecError but NOT pydantic.ValidationError; so a
    # bad form_types (which raises SecValidationError) lands in the
    # try/except and returns the structured error envelope.
    result = await a.call_tool(
        "get_company_filings",
        {"cik_or_ticker": "AAPL", "form_types": ["BOGUS"]},
    )
    payload = _extract_payload(result)
    assert payload.get("error") == "validation"
    assert payload.get("field") == "form_types"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_payload(result):  # type: ignore[no-untyped-def]
    """Best-effort extraction of the structured payload from a CallToolResult.

    FastMCP across versions returns either a ``(content, structured_content)``
    tuple or an object with ``.structuredContent`` / ``.content`` attrs;
    we accept both shapes.
    """
    # Tuple shape
    if isinstance(result, tuple):
        # (list[ContentBlock], dict | None)
        if len(result) >= 2 and isinstance(result[1], dict):
            return result[1]
        if result and hasattr(result[0], "text"):
            return json.loads(result[0].text)
        return {}
    # Object shape
    sc = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    content = getattr(result, "content", None)
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return {}
