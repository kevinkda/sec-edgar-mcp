"""Unit tests for the 4 business tools + 2 meta tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.errors import SecNotFoundError
from sec_edgar_mcp.models import (
    GetCompanyFilingsInput,
    GetFilingTextInput,
    GetForm4InsiderTradesInput,
    SearchFilingsFullTextInput,
)
from sec_edgar_mcp.tools.filings import get_company_filings_impl, get_filing_text_impl
from sec_edgar_mcp.tools.insider import get_form4_insider_trades_impl
from sec_edgar_mcp.tools.meta import (
    get_cache_stats_impl,
    get_server_info_impl,
    health_check_impl,
)
from sec_edgar_mcp.tools.search import search_filings_full_text_impl
from tests.conftest import FIXTURE_DIR, FakeRoute


def _seed_routes(fixture_dir: Path) -> list[FakeRoute]:
    """Build a complete route table that satisfies all 4 tools."""
    tickers = json.loads((fixture_dir / "company_tickers.json").read_text(encoding="utf-8"))
    submissions = json.loads((fixture_dir / "submissions_aapl.json").read_text(encoding="utf-8"))
    # Rewrite filing dates to be recent (within the last 60 days) so the
    # since_days windows in the tests are deterministic.
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    dates = [
        (today - timedelta(days=10)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=45)).isoformat(),
    ]
    submissions["filings"]["recent"]["filingDate"] = dates
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


# ---------------------------------------------------------------------------
# get_company_filings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_company_filings_normal(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_company_filings_impl(
        GetCompanyFilingsInput(cik_or_ticker="AAPL", limit=10),
    )
    assert out["company"]["name"] == "Apple Inc."
    assert out["company"]["cik"] == "0000320193"
    assert out["count"] == 4
    assert out["filings"][0]["form"] == "10-K"


@pytest.mark.asyncio
async def test_get_company_filings_form_filter(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_company_filings_impl(
        GetCompanyFilingsInput(cik_or_ticker="AAPL", form_types=["8-K"]),
    )
    forms = {f["form"] for f in out["filings"]}
    assert forms == {"8-K"}


@pytest.mark.asyncio
async def test_get_company_filings_404(make_client) -> None:
    client = make_client(
        [
            FakeRoute(
                "/files/company_tickers.json",
                json_body={"0": {"cik_str": 1, "ticker": "ZZZZ", "title": "Z"}},
            ),
            FakeRoute("/submissions/CIK", status_code=404, json_body={}),
        ]
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecNotFoundError):
        await get_company_filings_impl(
            GetCompanyFilingsInput(cik_or_ticker="ZZZZ"),
        )


@pytest.mark.asyncio
async def test_get_company_filings_cache_hit(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = await get_company_filings_impl(GetCompanyFilingsInput(cik_or_ticker="AAPL"))
    assert a["_cache_status"] == "miss"
    b = await get_company_filings_impl(GetCompanyFilingsInput(cik_or_ticker="AAPL"))
    assert b["_cache_status"] == "hit"


@pytest.mark.asyncio
async def test_get_company_filings_cache_bypass(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    a = await get_company_filings_impl(GetCompanyFilingsInput(cik_or_ticker="AAPL"))
    assert a["_cache_status"] == "miss"
    monkeypatch.setenv("SEC_EDGAR_CACHE_BYPASS", "1")
    b = await get_company_filings_impl(GetCompanyFilingsInput(cik_or_ticker="AAPL"))
    assert b["_cache_status"] == "bypass"


# ---------------------------------------------------------------------------
# get_form4_insider_trades
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_form4_normal(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_form4_insider_trades_impl(
        GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=365),
    )
    assert out["count"] == 1
    assert out["transactions"][0]["form"] == "4"
    assert out["issuer"]["cik"] == "0000320193"


@pytest.mark.asyncio
async def test_get_form4_filters_by_window(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    # since_days=1 cuts out the fixture's old 2024-04-30 Form 4.
    out = await get_form4_insider_trades_impl(
        GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=1),
    )
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_get_form4_404(make_client) -> None:
    client = make_client(
        [
            FakeRoute(
                "/files/company_tickers.json",
                json_body={"0": {"cik_str": 1, "ticker": "ZZZZ", "title": "Z"}},
            ),
            FakeRoute("/submissions/CIK", status_code=404, json_body={}),
        ]
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecNotFoundError):
        await get_form4_insider_trades_impl(
            GetForm4InsiderTradesInput(cik_or_ticker="ZZZZ"),
        )


# ---------------------------------------------------------------------------
# get_filing_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_filing_text_primary(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_filing_text_impl(
        GetFilingTextInput(accession_number="0000320193-24-000123"),
    )
    assert "Apple Inc." in out["text"]
    assert out["content_type"] == "text/html"
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_get_filing_text_complete(make_client) -> None:
    extra = [
        FakeRoute(
            "0000320193-24-000123-submission.txt",
            text_body="full submission text",
            content_type="text/plain",
        ),
    ]
    client = make_client([*_seed_routes(FIXTURE_DIR), *extra])
    await runtime_mod.set_client_for_tests(client)
    out = await get_filing_text_impl(
        GetFilingTextInput(
            accession_number="0000320193-24-000123",
            document_type="complete",
        ),
    )
    assert "submission" in out["text"]


@pytest.mark.asyncio
async def test_get_filing_text_404_index(make_client) -> None:
    client = make_client(
        [FakeRoute("-index.json", status_code=404, json_body={})],
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecNotFoundError):
        await get_filing_text_impl(
            GetFilingTextInput(accession_number="0000320193-24-000123"),
        )


@pytest.mark.asyncio
async def test_get_filing_text_no_primary_doc(make_client) -> None:
    # index.json without any .htm/.txt items.
    client = make_client(
        [
            FakeRoute(
                "-index.json",
                json_body={"directory": {"item": [{"name": "stuff.xml"}]}},
            ),
        ]
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecNotFoundError):
        await get_filing_text_impl(
            GetFilingTextInput(accession_number="0000320193-24-000123"),
        )


# ---------------------------------------------------------------------------
# search_filings_full_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_full_text_normal(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await search_filings_full_text_impl(
        SearchFilingsFullTextInput(query="cybersecurity"),
    )
    assert out["total_hits"] == 2
    assert out["returned"] == 2
    assert out["results"][0]["form"] == "8-K"


@pytest.mark.asyncio
async def test_search_full_text_form_filter(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await search_filings_full_text_impl(
        SearchFilingsFullTextInput(query="cybersecurity", form_types=["8-K"]),
    )
    assert out["total_hits"] == 2


@pytest.mark.asyncio
async def test_search_full_text_429(make_client) -> None:
    from sec_edgar_mcp.errors import SecRateLimitError

    client = make_client(
        [
            FakeRoute(
                "/LATEST/search-index",
                status_code=429,
                json_body={},
                headers={"Retry-After": "0"},
            ),
        ]
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecRateLimitError):
        await search_filings_full_text_impl(
            SearchFilingsFullTextInput(query="x"),
        )


@pytest.mark.asyncio
async def test_search_full_text_5xx(make_client) -> None:
    from sec_edgar_mcp.errors import SecTransientError

    client = make_client(
        [FakeRoute("/LATEST/search-index", status_code=503, json_body={})],
    )
    await runtime_mod.set_client_for_tests(client)
    with pytest.raises(SecTransientError):
        await search_filings_full_text_impl(
            SearchFilingsFullTextInput(query="x"),
        )


# ---------------------------------------------------------------------------
# meta tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_offline_safe() -> None:
    out = await health_check_impl()
    assert "user_agent_configured" in out
    assert "cache_enabled" in out
    assert out["platform_supported"] is True
    assert out["rate_limit_hard_cap"] == 10


@pytest.mark.asyncio
async def test_health_check_no_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    out = await health_check_impl()
    assert out["user_agent_configured"] is False
    assert out["rate_limit_per_sec"] is None


@pytest.mark.asyncio
async def test_get_server_info() -> None:
    out = await get_server_info_impl(server_version="9.9.9")
    assert out["server_version"] == "9.9.9"
    assert "supported_tools" in out
    assert len(out["supported_tools"]) == 6


@pytest.mark.asyncio
async def test_get_cache_stats_enabled() -> None:
    out = await get_cache_stats_impl()
    assert "rows_per_table" in out
    assert "hit_rate_24h" in out


@pytest.mark.asyncio
async def test_get_cache_stats_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import sec_edgar_mcp.cache as cache_mod

    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
    cache_mod.reset_cache_singleton()
    out = await get_cache_stats_impl()
    assert out["enabled"] is False
