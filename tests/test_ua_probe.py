"""Tests for `sec_edgar_mcp._ua_probe`.

Covers all 5 statuses (ACCEPTED / REJECTED_HTML_403 / TIMEOUT /
NETWORK_ERROR / UNCONFIGURED), cache behaviour (TTL hit, TTL miss after
expiry, per-UA invalidation), and unconfigured short-circuits.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from sec_edgar_mcp import _ua_probe
from sec_edgar_mcp._ua_probe import (
    PROBE_URL,
    UaProbeResult,
    probe_ua_reachability,
    reset_cache,
)

VALID_UA = "sec-edgar-mcp-r7-tests/0 (kevin@kdacorp.test)"


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> None:
    reset_cache()
    yield
    reset_cache()


def _fake_now() -> datetime:
    return datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC)


class _MonotonicClock:
    """Mutable monotonic source the tests advance manually."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value: float = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _make_response(status_code: int, *, content_type: str = "text/html", body: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body,
        headers={"Content-Type": content_type},
    )


def test_accepted_status_when_sec_returns_200() -> None:
    calls: list[tuple[str, float, str]] = []

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        calls.append((url, timeout, ua))
        return _make_response(200, content_type="text/html; charset=utf-8")

    clock = _MonotonicClock()
    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
    )

    assert isinstance(result, UaProbeResult)
    assert result.status == "ACCEPTED"
    assert "HTTP 200" in result.detail
    assert result.cache_ttl_remaining_s == _ua_probe.DEFAULT_CACHE_TTL_SECONDS
    assert result.last_checked_at == _fake_now()
    assert calls == [(PROBE_URL, _ua_probe.DEFAULT_TIMEOUT_SECONDS, VALID_UA)]


def test_rejected_html_403_when_sec_returns_undeclared_automated_tool() -> None:
    body = (
        b"<html><body>Your client appears to be an undeclared automated tool, "
        b"please update your User-Agent...</body></html>"
    )

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(403, content_type="text/html", body=body)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "REJECTED_HTML_403"
    assert "fair-access" in result.detail
    assert "undeclared automated tool" in result.detail


def test_403_without_signature_is_treated_as_network_error() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(403, content_type="text/html", body=b"<html>WAF block</html>")

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "NETWORK_ERROR"
    assert "without recognised SEC rejection signature" in result.detail


def test_timeout_status_on_timeout_exception() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=None)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "TIMEOUT"
    assert "timed out" in result.detail


def test_network_error_status_on_connect_error() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=None)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "NETWORK_ERROR"
    assert "ConnectError" in result.detail


def test_unconfigured_when_ua_empty() -> None:
    raised: list[bool] = []

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        raised.append(True)
        return _make_response(200)

    result = probe_ua_reachability(
        "",
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "UNCONFIGURED"
    assert raised == []


def test_unconfigured_when_ua_contains_noreply_placeholder() -> None:
    bogus = "my-app (123456+user@users.noreply.github.com)"
    raised: list[bool] = []

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:  # pragma: no cover
        raised.append(True)
        return _make_response(200)

    result = probe_ua_reachability(
        bogus,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "UNCONFIGURED"
    assert raised == []


def test_unconfigured_when_ua_format_invalid() -> None:
    result = probe_ua_reachability(
        "not-a-valid-ua-no-email-here",
        head_func=lambda *a, **kw: _make_response(200),
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "UNCONFIGURED"


def test_cache_hit_avoids_second_head_call_within_ttl() -> None:
    call_counter = {"n": 0}

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        call_counter["n"] += 1
        return _make_response(200)

    clock = _MonotonicClock()
    first = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=300,
    )
    clock.advance(60)
    second = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=300,
    )

    assert call_counter["n"] == 1
    assert first.status == "ACCEPTED"
    assert second.status == "ACCEPTED"
    # TTL must count down from 300 → ~240 after 60s.
    assert second.cache_ttl_remaining_s <= 240
    assert second.cache_ttl_remaining_s > 200


def test_cache_invalidates_after_ttl_expiry() -> None:
    call_counter = {"n": 0}

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        call_counter["n"] += 1
        return _make_response(200)

    clock = _MonotonicClock()
    probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=10,
    )
    clock.advance(11)  # TTL exceeded
    probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=10,
    )

    assert call_counter["n"] == 2


def test_changing_ua_triggers_new_probe() -> None:
    seen_uas: list[str] = []

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        seen_uas.append(ua)
        return _make_response(200)

    clock = _MonotonicClock()
    probe_ua_reachability(
        "alpha-app (alpha@example.test)",
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=300,
    )
    probe_ua_reachability(
        "beta-app (beta@example.test)",
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
        cache_ttl_seconds=300,
    )

    assert seen_uas == ["alpha-app (alpha@example.test)", "beta-app (beta@example.test)"]


def test_unexpected_http_status_maps_to_network_error() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(503)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "NETWORK_ERROR"
    assert "503" in result.detail


def test_to_dict_payload_shape() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(200)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    payload = result.to_dict()
    assert set(payload.keys()) == {
        "status",
        "detail",
        "last_checked_at",
        "cache_ttl_remaining_s",
    }
    assert payload["status"] == "ACCEPTED"
    assert payload["last_checked_at"].endswith("+00:00")


def test_403_with_rate_threshold_signature_also_classified_rejected() -> None:
    body = b"<html>SEC fair access: request rate threshold exceeded</html>"

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(403, body=body)

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "REJECTED_HTML_403"


def test_default_head_callable_uses_module_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke-test the production HEAD path: stub out httpx.Client to capture args."""
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def head(self, url: str, headers: dict[str, str]) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            return _make_response(200)

    monkeypatch.setattr(_ua_probe.httpx, "Client", _FakeClient)

    resp = _ua_probe._default_head(PROBE_URL, 5.0, VALID_UA)
    assert resp.status_code == 200
    assert captured["url"] == PROBE_URL
    assert captured["timeout"] == 5.0
    assert captured["follow_redirects"] is True
    assert captured["headers"]["User-Agent"] == VALID_UA


def test_reset_cache_drops_all_entries() -> None:
    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(200)

    clock = _MonotonicClock()
    probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=clock,
    )
    assert _ua_probe._CACHE
    reset_cache()
    assert not _ua_probe._CACHE


def test_unconfigured_short_circuit_uses_dedicated_cache_key() -> None:
    """Two different unconfigured UAs both short-circuit, but each call
    re-runs detection (cheap) — verify they don't collide and don't probe."""
    head_calls: list[str] = []

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:  # pragma: no cover
        head_calls.append(ua)
        return _make_response(200)

    r1 = probe_ua_reachability(
        "",
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )
    r2 = probe_ua_reachability(
        "noreply (x@noreply.github.com)",
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert r1.status == "UNCONFIGURED"
    assert r2.status == "UNCONFIGURED"
    assert head_calls == []


def test_403_with_empty_body_is_network_error() -> None:
    """403 with no body cannot be confirmed as a fair-access ban; must
    fall back to NETWORK_ERROR rather than mis-classify."""

    def fake_head(url: str, timeout: float, ua: str) -> httpx.Response:
        return _make_response(403, body=b"")

    result = probe_ua_reachability(
        VALID_UA,
        head_func=fake_head,
        now_func=_fake_now,
        monotonic_func=_MonotonicClock(),
    )

    assert result.status == "NETWORK_ERROR"


def test_peek_body_handles_response_not_read() -> None:
    """If httpx raises ResponseNotRead while reading body, return ''."""

    class _NoBodyResponse:
        @property
        def content(self) -> bytes:
            raise httpx.ResponseNotRead()

    # Cast through Any: we're exercising the defensive guard.
    body = _ua_probe._peek_body_text(_NoBodyResponse())  # type: ignore[arg-type]
    assert body == ""


def test_matched_rejection_signature_empty_string_returns_none() -> None:
    assert _ua_probe._matched_rejection_signature("") is None
