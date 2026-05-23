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
async def test_app_exports_six_tools() -> None:
    a = app()
    tools = await a.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_company_filings",
        "get_form4_insider_trades",
        "get_filing_text",
        "search_filings_full_text",
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
    assert len(payload["supported_tools"]) == 6


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
