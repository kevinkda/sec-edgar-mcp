"""Tool-level tests for 13F + proxy tools (FakeTransport, no real SEC).

Covers :mod:`sec_edgar_mcp.tools.thirteenf` and
:mod:`sec_edgar_mcp.tools.proxy` end-to-end through the impl functions,
exercising the SEC fetch flow, quarter selection, value-unit cutover,
reverse-holder aggregation, and proxy DEF 14A selection — all against a
deterministic in-process FakeTransport.
"""

from __future__ import annotations

import json

import pytest

import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.errors import SecNotFoundError, ThirteenFParseError
from sec_edgar_mcp.models import (
    Get13FHoldingsInput,
    GetInstitutionalHoldersInput,
    GetProxyStatementInput,
)
from sec_edgar_mcp.tools.proxy import get_proxy_statement_impl
from sec_edgar_mcp.tools.thirteenf import (
    get_13f_holdings_impl,
    get_institutional_holders_impl,
)
from tests.conftest import FIXTURE_DIR, FakeRoute


def _f(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _j(name: str) -> dict:
    return json.loads(_f(name))


# ===========================================================================
# get_13f_holdings
# ===========================================================================


def _routes_13f() -> list[FakeRoute]:
    return [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=_j("submissions_13f_manager.json")),
        FakeRoute("-index.json", json_body=_j("index_13f.json")),
        FakeRoute("form13fInfoTable.xml", text_body=_f("form13f_info_table.xml"), content_type="text/xml"),
    ]


@pytest.mark.asyncio
async def test_13f_latest_quarter(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_13f()))
    out = await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983"))
    assert out["manager"]["cik"] == "0001067983"
    assert out["accession_number"] == "0001067983-24-000020"
    assert out["report_date"] == "2024-06-30"
    assert out["holding_count"] == 3
    # 2024-06-30 is after the 2023-06-30 cutover → whole dollars.
    assert out["value_units"] == "dollars"
    assert out["total_value_reported"] == "4250000"
    assert out["total_value_usd"] == "4250000"


@pytest.mark.asyncio
async def test_13f_specific_quarter_exact_match(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_13f()))
    out = await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983", quarter="2024Q1"))
    assert out["report_date"] == "2024-03-31"
    assert out["accession_number"] == "0001067983-24-000010"


@pytest.mark.asyncio
async def test_13f_thousands_cutover(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_13f()))
    out = await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983", quarter="2023Q2"))
    # 2023-06-30 <= cutover → reported in thousands; usd = reported * 1000.
    assert out["report_date"] == "2023-06-30"
    assert out["value_units"] == "thousands"
    assert out["total_value_usd"] == "4250000000"


@pytest.mark.asyncio
async def test_13f_quarter_not_found(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_13f()))
    with pytest.raises(SecNotFoundError):
        await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983", quarter="2020Q1"))


@pytest.mark.asyncio
async def test_13f_no_filings(make_client) -> None:
    routes = [
        FakeRoute("/submissions/CIK", json_body={"name": "X", "filings": {"recent": {}}}),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    with pytest.raises(SecNotFoundError):
        await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983"))


@pytest.mark.asyncio
async def test_13f_no_info_table_in_index(make_client) -> None:
    routes = [
        FakeRoute("/submissions/CIK", json_body=_j("submissions_13f_manager.json")),
        FakeRoute("-index.json", json_body={"directory": {"item": [{"name": "primary_doc.xml"}]}}),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    with pytest.raises(ThirteenFParseError):
        await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983"))


@pytest.mark.asyncio
async def test_13f_holdings_truncated(make_client, monkeypatch) -> None:
    monkeypatch.setattr("sec_edgar_mcp.tools.thirteenf._MAX_HOLDINGS_RETURNED", 1)
    await runtime_mod.set_client_for_tests(make_client(_routes_13f()))
    out = await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983"))
    assert out["holdings_truncated"] is True
    assert len(out["holdings"]) == 1
    assert out["holding_count"] == 3  # full count retained in summary


@pytest.mark.asyncio
async def test_13f_info_table_fallback_doc(make_client) -> None:
    """When no name matches the info-table heuristics, fall back to first xml."""
    index = {"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "weird_name.xml"}]}}
    routes = [
        FakeRoute("/submissions/CIK", json_body=_j("submissions_13f_manager.json")),
        FakeRoute("-index.json", json_body=index),
        FakeRoute("weird_name.xml", text_body=_f("form13f_info_table.xml"), content_type="text/xml"),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    out = await get_13f_holdings_impl(Get13FHoldingsInput(cik_or_ticker="1067983"))
    assert out["holding_count"] == 3


# ===========================================================================
# get_institutional_holders
# ===========================================================================


def _routes_holders() -> list[FakeRoute]:
    return [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=_j("submissions_aapl.json")),
        FakeRoute("/LATEST/search-index", json_body=_j("search_13f_holders.json")),
    ]


@pytest.mark.asyncio
async def test_institutional_holders_aggregates(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_holders()))
    out = await get_institutional_holders_impl(GetInstitutionalHoldersInput(ticker="AAPL"))
    assert out["ticker"] == "AAPL"
    assert out["company"] == "Apple Inc."
    assert out["search_total_hits"] == 3
    # Berkshire appears twice → deduped to one holder with filings=2.
    assert out["holder_count"] == 2
    berkshire = next(h for h in out["holders"] if h["cik"] == "0001067983")
    assert berkshire["filings"] == 2
    assert berkshire["latest_filing_date"] == "2024-08-14"


@pytest.mark.asyncio
async def test_institutional_holders_limit(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_holders()))
    out = await get_institutional_holders_impl(GetInstitutionalHoldersInput(ticker="AAPL", limit=1))
    assert out["holder_count"] == 1


@pytest.mark.asyncio
async def test_institutional_holders_company_lookup_tolerates_failure(make_client) -> None:
    """If the ticker→CIK resolution fails, the search still runs (company=None)."""
    routes = [
        FakeRoute("/files/company_tickers.json", json_body={}),  # ticker not found
        FakeRoute("/LATEST/search-index", json_body=_j("search_13f_holders.json")),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    out = await get_institutional_holders_impl(GetInstitutionalHoldersInput(ticker="AAPL"))
    assert out["company"] is None
    assert out["holder_count"] == 2


@pytest.mark.asyncio
async def test_institutional_holders_empty_results(make_client) -> None:
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=_j("submissions_aapl.json")),
        FakeRoute("/LATEST/search-index", json_body={"hits": {"total": {"value": 0}, "hits": []}}),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    out = await get_institutional_holders_impl(GetInstitutionalHoldersInput(ticker="AAPL"))
    assert out["holder_count"] == 0
    assert "no_13f_holders_found_in_window" in out["warnings"]


@pytest.mark.asyncio
async def test_institutional_holders_malformed_hits(make_client) -> None:
    """Non-dict hits / missing ciks are skipped without crashing."""
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=_j("submissions_aapl.json")),
        FakeRoute(
            "/LATEST/search-index",
            json_body={"hits": {"total": "nan", "hits": ["bad", {"_source": {}}, {"_source": {"ciks": []}}]}},
        ),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    out = await get_institutional_holders_impl(GetInstitutionalHoldersInput(ticker="AAPL"))
    assert out["holder_count"] == 0
    assert out["search_total_hits"] == 0


# ===========================================================================
# get_proxy_statement
# ===========================================================================


def _routes_proxy() -> list[FakeRoute]:
    return [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=_j("submissions_proxy.json")),
        FakeRoute("def14a-2026.htm", text_body=_f("def14a_apple.htm"), content_type="text/html"),
    ]


@pytest.mark.asyncio
async def test_proxy_statement_extracts(make_client) -> None:
    await runtime_mod.set_client_for_tests(make_client(_routes_proxy()))
    out = await get_proxy_statement_impl(GetProxyStatementInput(cik_or_ticker="AAPL"))
    assert out["company"]["name"] == "Apple Inc."
    # DEF 14A preferred over the DEFA14A even though DEFA is filed later.
    assert out["form"] == "DEF 14A"
    assert out["accession_number"] == "0000320193-26-000004"
    assert out["proxy"]["meeting_date"] == "May 15, 2026"
    assert out["proxy"]["max_total_compensation"] == "$63,209,845"


@pytest.mark.asyncio
async def test_proxy_statement_none_found(make_client) -> None:
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body={"name": "X", "filings": {"recent": {}}}),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    with pytest.raises(SecNotFoundError):
        await get_proxy_statement_impl(GetProxyStatementInput(cik_or_ticker="AAPL"))


@pytest.mark.asyncio
async def test_proxy_statement_no_primary_doc(make_client) -> None:
    subs = {
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000004"],
                "form": ["DEF 14A"],
                "filingDate": ["2026-01-10"],
                "primaryDocument": [""],
            }
        },
    }
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=subs),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    with pytest.raises(SecNotFoundError):
        await get_proxy_statement_impl(GetProxyStatementInput(cik_or_ticker="AAPL"))


@pytest.mark.asyncio
async def test_proxy_prefers_def14a_over_defa(make_client) -> None:
    """DEFA14A-only filing is still selectable when no DEF 14A exists."""
    subs = {
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000005"],
                "form": ["DEFA14A"],
                "filingDate": ["2026-01-12"],
                "primaryDocument": ["defa14a-2026.htm"],
            }
        },
    }
    routes = [
        FakeRoute("/files/company_tickers.json", json_body=_j("company_tickers.json")),
        FakeRoute("/submissions/CIK", json_body=subs),
        FakeRoute("defa14a-2026.htm", text_body=_f("def14a_apple.htm"), content_type="text/html"),
    ]
    await runtime_mod.set_client_for_tests(make_client(routes))
    out = await get_proxy_statement_impl(GetProxyStatementInput(cik_or_ticker="AAPL"))
    assert out["form"] == "DEFA14A"
