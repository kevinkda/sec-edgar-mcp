"""Tests for the `sec_ua_reachable` field and `overall_status` aggregation
inside `health_check_impl`.

These exercise the integration between `_ua_probe` and `tools.meta`,
mocking the probe so the test never touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sec_edgar_mcp._ua_probe import UaProbeResult, reset_cache
from sec_edgar_mcp.tools.meta import health_check_impl


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> None:
    reset_cache()
    yield
    reset_cache()


def _stub_probe_with(monkeypatch: pytest.MonkeyPatch, status: str, detail: str = "stub") -> None:
    """Force `probe_ua_reachability` to return a fixed status."""

    fixed = UaProbeResult(
        status=status,  # type: ignore[arg-type]
        detail=detail,
        last_checked_at=datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC),
        cache_ttl_remaining_s=300,
    )

    def fake(*_args: object, **_kwargs: object) -> UaProbeResult:
        return fixed

    # Patch the symbol *as imported by tools.meta*, not just the module.
    import sec_edgar_mcp.tools.meta as meta_mod

    monkeypatch.setattr(meta_mod, "probe_ua_reachability", fake)


@pytest.mark.asyncio
async def test_health_check_includes_sec_ua_reachable_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "ACCEPTED")
    out = await health_check_impl()

    assert "sec_ua_reachable" in out
    assert out["sec_ua_reachable"]["status"] == "ACCEPTED"
    assert "last_checked_at" in out["sec_ua_reachable"]
    assert "cache_ttl_remaining_s" in out["sec_ua_reachable"]


@pytest.mark.asyncio
async def test_overall_status_ok_when_probe_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "ACCEPTED")
    out = await health_check_impl()
    assert out["overall_status"] == "ok"


@pytest.mark.asyncio
async def test_overall_status_degraded_when_probe_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "REJECTED_HTML_403", detail="SEC denied UA")
    out = await health_check_impl()
    assert out["overall_status"] == "degraded"
    assert out["sec_ua_reachable"]["status"] == "REJECTED_HTML_403"


@pytest.mark.asyncio
async def test_overall_status_unhealthy_when_probe_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "UNCONFIGURED", detail="missing")
    out = await health_check_impl()
    assert out["overall_status"] == "unhealthy"


@pytest.mark.asyncio
async def test_overall_status_ok_on_transient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient TIMEOUT must not downgrade overall_status."""
    _stub_probe_with(monkeypatch, "TIMEOUT", detail="slow")
    out = await health_check_impl()
    assert out["overall_status"] == "ok"


@pytest.mark.asyncio
async def test_overall_status_ok_on_transient_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "NETWORK_ERROR", detail="conn refused")
    out = await health_check_impl()
    assert out["overall_status"] == "ok"


@pytest.mark.asyncio
async def test_existing_fields_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe_with(monkeypatch, "ACCEPTED")
    out = await health_check_impl()
    # All pre-R7 fields must still be present so downstream consumers
    # don't break.
    for key in (
        "server_version",
        "user_agent_configured",
        "user_agent_reason",
        "rate_limit_per_sec",
        "rate_limit_hard_cap",
        "cache_enabled",
        "cache_size_mb",
        "cache_hit_rate_24h",
        "platform_supported",
    ):
        assert key in out


@pytest.mark.asyncio
async def test_real_probe_short_circuits_on_test_user_agent() -> None:
    """The conftest sets a UA containing 'example.com' which is on our
    placeholder deny-list; the real probe must short-circuit to
    UNCONFIGURED without touching the network."""

    head_calls: list[str] = []

    def trap_head(url: str, timeout: float, ua: str) -> object:
        head_calls.append(ua)
        raise AssertionError("network must not be touched in tests")

    # Inject the trap by monkeypatching the default head.
    import sec_edgar_mcp._ua_probe as probe_mod

    real_default = probe_mod._default_head
    probe_mod._default_head = trap_head  # type: ignore[assignment]
    try:
        out = await health_check_impl()
    finally:
        probe_mod._default_head = real_default  # type: ignore[assignment]

    # conftest UA is 'sec-edgar-mcp-tests/0 (test@example.com)'; example.com
    # is on the placeholder deny-list so the probe short-circuits.
    assert out["sec_ua_reachable"]["status"] == "UNCONFIGURED"
    assert head_calls == []
