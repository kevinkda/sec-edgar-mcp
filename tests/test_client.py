"""Unit tests for sec_edgar_mcp.client."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from sec_edgar_mcp.client import (
    DATA_HOST,
    SEC_HARD_RATE_LIMIT_PER_SEC,
    WWW_HOST,
    SecEdgarClient,
    TokenBucket,
    _backoff_delay,
    _parse_retry_after,
    make_client,
    resolve_cik,
    resolve_rate_limit,
    resolve_user_agent,
    server_user_agent_default,
)
from sec_edgar_mcp.errors import (
    SecConfigurationError,
    SecNotFoundError,
    SecRateLimitError,
    SecTransientError,
)
from tests.conftest import FakeRoute

# ---------------------------------------------------------------------------
# resolve_user_agent / resolve_rate_limit
# ---------------------------------------------------------------------------


class TestResolveUserAgent:
    def test_present_and_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "MyApp/1.0 (me@example.com)")
        assert resolve_user_agent() == "MyApp/1.0 (me@example.com)"

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
        with pytest.raises(SecConfigurationError):
            resolve_user_agent()

    def test_empty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "   ")
        with pytest.raises(SecConfigurationError):
            resolve_user_agent()

    def test_bad_format_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "no-email-here")
        with pytest.raises(SecConfigurationError):
            resolve_user_agent()


class TestResolveRateLimit:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", raising=False)
        assert resolve_rate_limit() == 8

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "5")
        assert resolve_rate_limit() == 5

    def test_clamped_to_hard_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "999")
        assert resolve_rate_limit() == SEC_HARD_RATE_LIMIT_PER_SEC

    def test_negative_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "-3")
        assert resolve_rate_limit() == 1

    def test_garbage_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "abc")
        assert resolve_rate_limit() == 8


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_admits_under_capacity(self) -> None:
        b = TokenBucket(capacity=3)
        await b.acquire()
        await b.acquire()
        await b.acquire()
        assert b.tokens_remaining() == 0

    @pytest.mark.asyncio
    async def test_blocks_over_capacity(self) -> None:
        b = TokenBucket(capacity=2)
        await b.acquire()
        await b.acquire()
        # The 3rd call should block briefly until one of the prior tokens
        # ages out.  We bound the wait at ~1.2 s.
        before = asyncio.get_event_loop().time()
        await b.acquire()
        after = asyncio.get_event_loop().time()
        assert (after - before) > 0.5  # had to wait for token replenishment

    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0)


# ---------------------------------------------------------------------------
# Backoff helpers
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_backoff_grows(self) -> None:
        a = _backoff_delay(0)
        b = _backoff_delay(2)
        assert b > a
        assert isinstance(a, float)

    def test_parse_retry_after_int(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "7"})
        assert _parse_retry_after(resp) == 7

    def test_parse_retry_after_clamped(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "9999"})
        assert _parse_retry_after(resp) == 60

    def test_parse_retry_after_garbage(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "later"})
        assert _parse_retry_after(resp) == 1

    def test_parse_retry_after_missing(self) -> None:
        resp = httpx.Response(429)
        assert _parse_retry_after(resp) == 1


# ---------------------------------------------------------------------------
# Client request paths — normal / 404 / 429 / 5xx + json/text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_normal_path(make_client) -> None:
    client = make_client(
        [
            FakeRoute(
                "/submissions/CIK",
                json_body={"name": "Apple Inc.", "tickers": ["AAPL"]},
            ),
        ]
    )
    data = await client.get_json(f"{DATA_HOST}/submissions/CIK0000320193.json")
    assert data["name"] == "Apple Inc."
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_list_wrapped(make_client) -> None:
    client = make_client(
        [FakeRoute("/x.json", json_body=[{"a": 1}, {"b": 2}])],
    )
    data = await client.get_json(f"{DATA_HOST}/x.json")
    assert data == {"items": [{"a": 1}, {"b": 2}]}
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_404(make_client) -> None:
    client = make_client(
        [FakeRoute("/missing", status_code=404, json_body={"error": "nope"})],
    )
    with pytest.raises(SecNotFoundError):
        await client.get_json(f"{DATA_HOST}/missing.json")
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_429_eventually_raises(make_client) -> None:
    client = make_client(
        [FakeRoute("/throttle", status_code=429, json_body={}, headers={"Retry-After": "0"})],
    )
    with pytest.raises(SecRateLimitError):
        await client.get_json(f"{DATA_HOST}/throttle.json")
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_5xx_eventually_raises(make_client) -> None:
    client = make_client(
        [FakeRoute("/boom", status_code=503, json_body={})],
    )
    with pytest.raises(SecTransientError) as excinfo:
        await client.get_json(f"{DATA_HOST}/boom.json")
    assert excinfo.value.status_code == 503
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_unexpected_4xx(make_client) -> None:
    client = make_client(
        [FakeRoute("/teapot", status_code=418, json_body={})],
    )
    with pytest.raises(SecTransientError):
        await client.get_json(f"{DATA_HOST}/teapot.json")
    await client.aclose()


@pytest.mark.asyncio
async def test_get_json_invalid_json(make_client) -> None:
    routes = [FakeRoute("/badjson", text_body="<not-json>", content_type="application/json")]
    client = make_client(routes)
    with pytest.raises(SecTransientError):
        await client.get_json(f"{DATA_HOST}/badjson.json")
    await client.aclose()


@pytest.mark.asyncio
async def test_get_text_truncation(make_client) -> None:
    big = "A" * 1000
    client = make_client(
        [FakeRoute("/big.htm", text_body=big, content_type="text/html")],
    )
    text, ctype, byte_size, truncated = await client.get_text(f"{WWW_HOST}/big.htm", max_bytes=100)
    assert truncated is True
    assert len(text) == 100
    assert byte_size == 1000
    assert ctype == "text/html"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_text_no_truncation(make_client) -> None:
    client = make_client(
        [FakeRoute("/small.htm", text_body="hi", content_type="text/html")],
    )
    text, ctype, byte_size, truncated = await client.get_text(f"{WWW_HOST}/small.htm", max_bytes=1000)
    assert truncated is False
    assert text == "hi"
    assert byte_size == 2
    await client.aclose()


# ---------------------------------------------------------------------------
# resolve_cik
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_cik_numeric(make_client) -> None:
    client = make_client([])
    out = await resolve_cik(client, "320193")
    assert out == "0000320193"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_cik_ticker_hit(make_client) -> None:
    routes = [
        FakeRoute(
            "/files/company_tickers.json",
            json_body={
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MSFT CORP"},
            },
        ),
    ]
    client = make_client(routes)
    out = await resolve_cik(client, "msft")
    assert out == "0000789019"
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_cik_ticker_miss(make_client) -> None:
    routes = [
        FakeRoute(
            "/files/company_tickers.json",
            json_body={"0": {"cik_str": 1, "ticker": "X", "title": "X"}},
        ),
    ]
    client = make_client(routes)
    with pytest.raises(SecNotFoundError):
        await resolve_cik(client, "ZZZZ")
    await client.aclose()


# ---------------------------------------------------------------------------
# make_client + helpers
# ---------------------------------------------------------------------------


def test_make_client_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "X/1 (a@b.com)")
    c = make_client()
    assert isinstance(c, SecEdgarClient)


def test_server_user_agent_default_includes_version() -> None:
    ua = server_user_agent_default()
    assert "sec-edgar-mcp" in ua
    assert "@" in ua


@pytest.mark.asyncio
async def test_client_async_context(make_client) -> None:
    client = make_client([FakeRoute("/x", json_body={"ok": True})])
    async with client as c:
        d = await c.get_json(f"{DATA_HOST}/x.json")
    assert d == {"ok": True}


# ---------------------------------------------------------------------------
# Network errors → SecTransientError after retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_eventually_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated dns failure")

    transport = httpx.MockTransport(handler)
    client = SecEdgarClient(
        user_agent="X/1 (a@b.com)",
        rate_limit_per_sec=10,
        transport=transport,
    )
    with pytest.raises(SecTransientError) as excinfo:
        await client.get_json(f"{DATA_HOST}/x.json")
    assert excinfo.value.status_code == 0
    await client.aclose()


# Ensure a stray env var doesn't leak
def test_env_isolation() -> None:
    assert "SEC_EDGAR_USER_AGENT" in os.environ
