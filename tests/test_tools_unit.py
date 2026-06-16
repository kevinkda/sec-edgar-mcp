"""Unit tests for the 4 business tools + 2 meta tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.errors import SecNotFoundError
from sec_edgar_mcp.models import (
    Get8KWithItemsInput,
    GetCompanyFilingsInput,
    GetFilingTextInput,
    GetForm4InsiderTradesInput,
    SearchFilingsFullTextInput,
)
from sec_edgar_mcp.tools.filings import (
    get_8k_with_items_impl,
    get_company_filings_impl,
    get_filing_text_impl,
)
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
    form4_xml = (fixture_dir / "form4_aapl.xml").read_text(encoding="utf-8")
    return [
        FakeRoute("/files/company_tickers.json", json_body=tickers),
        FakeRoute("/submissions/CIK", json_body=submissions),
        FakeRoute("-index.json", json_body=index),
        FakeRoute("aapl-20240928.htm", text_body=body, content_type="text/html"),
        FakeRoute("/LATEST/search-index", json_body=search),
        FakeRoute(
            "wf-form4_171430123456.xml",
            text_body=form4_xml,
            content_type="application/xml",
        ),
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
    row = out["transactions"][0]
    assert row["form"] == "4"
    assert out["issuer"]["cik"] == "0000320193"
    # New v0.2 structured payload — fixture parsed without errors.
    assert row["parse_error"] is None
    assert isinstance(row["form4"], dict)
    assert row["form4"]["transaction_count"] == 3
    assert row["form4"]["reporting_owner_name"] == "COOK TIMOTHY D"
    assert out["summary"]["transaction_count"] == 3
    # The fixture has one priced sale at 100,000 shares * 170.50 = 17,050,000.
    assert out["summary"]["net_sell_value"] == "17050000.00"
    assert out["summary"]["net_buy_value"] == "0"
    assert out["summary"]["parse_failures"] == 0


@pytest.mark.asyncio
async def test_get_form4_xbrl_fetch_failure_does_not_break_tool(make_client) -> None:
    """A 404 on a single Form 4 body must surface as parse_error, not raise."""
    routes = _seed_routes(FIXTURE_DIR)
    # Override the form 4 body route with a 404.
    routes = [r for r in routes if "wf-form4" not in r.url_substring]
    routes.append(FakeRoute("wf-form4_171430123456.xml", status_code=404, json_body={}))
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    out = await get_form4_insider_trades_impl(
        GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=365),
    )
    assert out["count"] == 1
    row = out["transactions"][0]
    assert row["form4"] is None
    assert row["parse_error"] is not None
    assert "SecNotFoundError" in row["parse_error"]
    assert out["summary"]["parse_failures"] == 1
    assert out["summary"]["transaction_count"] == 0


@pytest.mark.asyncio
async def test_get_form4_xbrl_malformed_body_surfaces_parse_error(make_client) -> None:
    """A malformed XML body surfaces Form4ParseError reason, no crash."""
    routes = _seed_routes(FIXTURE_DIR)
    routes = [r for r in routes if "wf-form4" not in r.url_substring]
    routes.append(
        FakeRoute(
            "wf-form4_171430123456.xml",
            text_body="<not-an-ownership-doc/>",
            content_type="application/xml",
        ),
    )
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    out = await get_form4_insider_trades_impl(
        GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=365),
    )
    row = out["transactions"][0]
    assert row["form4"] is None
    assert row["parse_error"] is not None
    assert "expected root" in row["parse_error"]


@pytest.mark.asyncio
async def test_get_form4_missing_primary_document(make_client) -> None:
    """If the submissions index has no primaryDocument, tool returns
    parse_error without attempting an HTTP fetch."""
    routes = _seed_routes(FIXTURE_DIR)
    # Patch the submissions JSON to drop the primary document for the Form 4.
    submissions = json.loads(
        (FIXTURE_DIR / "submissions_aapl.json").read_text(encoding="utf-8"),
    )
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    submissions["filings"]["recent"]["filingDate"] = [
        (today - timedelta(days=10)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=45)).isoformat(),
    ]
    submissions["filings"]["recent"]["primaryDocument"][3] = ""
    routes = [
        r if "/submissions/CIK" not in r.url_substring else FakeRoute("/submissions/CIK", json_body=submissions)
        for r in routes
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    out = await get_form4_insider_trades_impl(
        GetForm4InsiderTradesInput(cik_or_ticker="AAPL", since_days=365),
    )
    row = out["transactions"][0]
    assert row["form4"] is None
    assert "no primary_document" in (row["parse_error"] or "")


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
# get_8k_with_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_8k_with_items_no_filter(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_8k_with_items_impl(
        Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=365),
    )
    assert out["count"] == 1
    assert out["item_codes_filter"] is None
    row = out["filings"][0]
    assert row["form"] == "8-K"
    assert row["accession_number"] == "0000320193-24-000050"
    assert row["items"] == ["5.02"]
    assert row["primary_doc_url"] is not None
    assert row["primary_doc_url"].endswith("aapl-20240515.htm")


@pytest.mark.asyncio
async def test_get_8k_with_items_filter_match(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_8k_with_items_impl(
        Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["5.02"], since_days=365),
    )
    assert out["count"] == 1
    assert out["item_codes_filter"] == ["5.02"]
    assert "5.02" in out["filings"][0]["items"]


@pytest.mark.asyncio
async def test_get_8k_with_items_filter_no_match(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_8k_with_items_impl(
        Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["1.01"], since_days=365),
    )
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_get_8k_with_items_window_filter(make_client) -> None:
    client = make_client(_seed_routes(FIXTURE_DIR))
    await runtime_mod.set_client_for_tests(client)
    out = await get_8k_with_items_impl(
        Get8KWithItemsInput(cik_or_ticker="AAPL", since_days=1),
    )
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_get_8k_with_items_invalid_item_code() -> None:
    """Pydantic validation rejects malformed item codes."""
    from pydantic import ValidationError

    from sec_edgar_mcp.errors import SecError

    with pytest.raises((ValidationError, SecError)):
        Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["not-a-code"])


@pytest.mark.asyncio
async def test_get_8k_with_items_404(make_client) -> None:
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
        await get_8k_with_items_impl(
            Get8KWithItemsInput(cik_or_ticker="ZZZZ"),
        )


@pytest.mark.asyncio
async def test_get_8k_with_items_multi_item_string(make_client) -> None:
    """Submissions index ``items`` is comma-joined; we must split correctly."""
    submissions = json.loads(
        (FIXTURE_DIR / "submissions_aapl.json").read_text(encoding="utf-8"),
    )
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    submissions["filings"]["recent"]["filingDate"] = [
        (today - timedelta(days=10)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=45)).isoformat(),
    ]
    submissions["filings"]["recent"]["items"][2] = "Item 1.01,Item 9.01"
    routes = [
        FakeRoute(
            "/files/company_tickers.json",
            json_body=json.loads((FIXTURE_DIR / "company_tickers.json").read_text(encoding="utf-8")),
        ),
        FakeRoute("/submissions/CIK", json_body=submissions),
    ]
    client = make_client(routes)
    await runtime_mod.set_client_for_tests(client)
    out = await get_8k_with_items_impl(
        Get8KWithItemsInput(cik_or_ticker="AAPL", item_codes=["9.01"], since_days=365),
    )
    assert out["count"] == 1
    assert "1.01" in out["filings"][0]["items"]
    assert "9.01" in out["filings"][0]["items"]


def test_filings_helpers_parse_items_edge_cases() -> None:
    """Direct unit tests for the small helpers in ``tools.filings``."""
    from sec_edgar_mcp.tools.filings import _parse_items

    assert _parse_items("") == []
    assert _parse_items(None) == []
    assert _parse_items(123) == []
    assert _parse_items("Item 1.01") == ["1.01"]
    assert _parse_items("Item 1.01, Item 5.02") == ["1.01", "5.02"]
    assert _parse_items("1.01,5.02") == ["1.01", "5.02"]
    # Stray "Item " prefix with weird casing.
    assert _parse_items("ITEM 5.02") == ["5.02"]
    # Empty token between commas is dropped.
    assert _parse_items("Item 1.01,,Item 5.02") == ["1.01", "5.02"]


def test_filings_helpers_build_primary_url() -> None:
    from sec_edgar_mcp.tools.filings import _build_primary_url

    url = _build_primary_url(320193, "0000320193-24-000050", "doc.htm")
    assert url is not None
    assert url.endswith("/Archives/edgar/data/320193/000032019324000050/doc.htm")
    assert _build_primary_url(None, "x", "doc.htm") is None
    assert _build_primary_url(320193, "x", None) is None  # type: ignore[arg-type]
    assert _build_primary_url(320193, "x", "") is None


def test_filings_helpers_filter_8k_non_dict_recent() -> None:
    from sec_edgar_mcp.tools.filings import _filter_8k

    assert _filter_8k(None, cik="0000320193", cutoff_iso="2020-01-01") == []
    assert _filter_8k([], cik="0000320193", cutoff_iso="2020-01-01") == []


def test_filings_helpers_filter_8k_skips_non_str_accession() -> None:
    from sec_edgar_mcp.tools.filings import _filter_8k

    recent = {
        "accessionNumber": [None, "0000320193-24-000050"],
        "form": ["8-K", "8-K"],
        "filingDate": ["2025-01-01", "2025-01-02"],
        "primaryDocument": ["a.htm", "b.htm"],
        "items": ["", "Item 1.01"],
    }
    rows = _filter_8k(recent, cik="0000320193", cutoff_iso="2020-01-01")
    assert len(rows) == 1
    assert rows[0]["accession_number"] == "0000320193-24-000050"


def test_insider_helpers_to_decimal_handles_garbage() -> None:
    from decimal import Decimal

    from sec_edgar_mcp.tools.insider import _to_decimal

    assert _to_decimal(None) == Decimal("0")
    assert _to_decimal("not-a-number") == Decimal("0")
    assert _to_decimal(object()) == Decimal("0")
    assert _to_decimal("3.14") == Decimal("3.14")


def test_insider_helpers_filter_form4_skips_non_str_accession() -> None:
    from sec_edgar_mcp.tools.insider import _filter_form4

    recent = {
        "accessionNumber": [None, "0000320193-24-000010"],
        "form": ["4", "4"],
        "filingDate": ["2099-01-01", "2099-01-02"],
        "primaryDocument": ["a.xml", "b.xml"],
    }
    rows = _filter_form4(recent, cik="0000320193", cutoff_iso="2000-01-01")
    assert len(rows) == 1
    assert rows[0]["accession_number"] == "0000320193-24-000010"


def test_insider_helpers_filter_form4_non_dict_recent() -> None:
    from sec_edgar_mcp.tools.insider import _filter_form4

    assert _filter_form4(None, cik="0000320193", cutoff_iso="2020-01-01") == []
    assert _filter_form4("oops", cik="0000320193", cutoff_iso="2020-01-01") == []


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
    assert len(out["supported_tools"]) == 10


@pytest.mark.asyncio
async def test_get_cache_stats_enabled() -> None:
    out = await get_cache_stats_impl()
    assert "backend" in out
    assert "entries" in out
    assert "enabled" in out


@pytest.mark.asyncio
async def test_get_cache_stats_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import sec_edgar_mcp.cache as cache_mod

    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
    cache_mod.reset_cache_singleton()
    out = await get_cache_stats_impl()
    assert out["enabled"] is False


# ---------------------------------------------------------------------------
# R8 hotfix — XSLT prefix stripping for Form 4 primary documents.
# ---------------------------------------------------------------------------


def test_strip_xslt_prefix_removes_xsl_subdirectory() -> None:
    """SEC submissions return ``xsl<style>/<doc>.xml`` for Form 4."""
    from sec_edgar_mcp.tools.insider import _strip_xslt_prefix

    assert _strip_xslt_prefix("xslF345X06/form4.xml") == "form4.xml"
    assert _strip_xslt_prefix("xslF345X05/wf-form4_123.xml") == "wf-form4_123.xml"


def test_strip_xslt_prefix_is_noop_for_raw_filename() -> None:
    """Plain filenames must pass through untouched."""
    from sec_edgar_mcp.tools.insider import _strip_xslt_prefix

    assert _strip_xslt_prefix("form4.xml") == "form4.xml"
    assert _strip_xslt_prefix("primary_doc.xml") == "primary_doc.xml"


def test_strip_xslt_prefix_ignores_unrelated_subdirectories() -> None:
    """A subdirectory that is *not* the SEC XSLT prefix must be preserved."""
    from sec_edgar_mcp.tools.insider import _strip_xslt_prefix

    assert _strip_xslt_prefix("foo/bar.xml") == "foo/bar.xml"
    assert _strip_xslt_prefix("XSLF345X06/form4.xml") == "XSLF345X06/form4.xml"
    # Empty rest after slash is treated as no-op (defensive).
    assert _strip_xslt_prefix("xsl/") == "xsl/"
