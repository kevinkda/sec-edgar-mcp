"""Shared pytest fixtures and helpers for sec-edgar-mcp tests."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import sec_edgar_mcp.cache as cache_mod
import sec_edgar_mcp.tools._runtime as runtime_mod
from sec_edgar_mcp.client import SecEdgarClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "seed"


def _set_test_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "sec-edgar-mcp-tests/0 (test@example.com)")


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Re-route XDG_STATE_HOME so the cache lives under tmp_path."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("SEC_EDGAR_CACHE_BYPASS", raising=False)
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "1")
    _set_test_user_agent(monkeypatch)
    cache_mod.reset_cache_singleton()
    runtime_mod.reset_client_cache()
    yield
    cache_mod.reset_cache_singleton()
    runtime_mod.reset_client_cache()


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


def _load_fixture(name: str) -> dict | list | str:
    """Load a fixture by filename from tests/fixtures/seed/."""
    path = FIXTURE_DIR / name
    text = path.read_text(encoding="utf-8")
    if name.endswith(".json"):
        return json.loads(text)
    return text


@pytest.fixture
def load_fixture():
    return _load_fixture


# ---------------------------------------------------------------------------
# MockTransport helpers
# ---------------------------------------------------------------------------


class FakeRoute:
    """Single route description used by FakeTransport."""

    def __init__(
        self,
        url_substring: str,
        *,
        status_code: int = 200,
        json_body: dict | list | None = None,
        text_body: str | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url_substring = url_substring
        self.status_code = status_code
        self.json_body = json_body
        self.text_body = text_body
        self.content_type = content_type
        self.headers = dict(headers or {})


class FakeTransport(httpx.MockTransport):
    """``httpx.MockTransport`` that selects a response by URL substring."""

    def __init__(self, routes: list[FakeRoute]) -> None:
        self.routes = list(routes)
        self.call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.call_log.append(str(request.url))
            for route in self.routes:
                if route.url_substring in str(request.url):
                    if route.json_body is not None:
                        return httpx.Response(
                            route.status_code,
                            json=route.json_body,
                            headers={"Content-Type": "application/json", **route.headers},
                        )
                    if route.text_body is not None:
                        return httpx.Response(
                            route.status_code,
                            content=route.text_body.encode("utf-8"),
                            headers={"Content-Type": route.content_type, **route.headers},
                        )
                    return httpx.Response(route.status_code, headers=route.headers)
            return httpx.Response(404, json={"error": "no matching route"})

        super().__init__(handler)


@pytest.fixture
def make_client():
    """Factory for a SecEdgarClient backed by a FakeTransport."""

    def _factory(routes: list[FakeRoute]) -> SecEdgarClient:
        transport = FakeTransport(routes)
        return SecEdgarClient(
            user_agent="sec-edgar-mcp-tests/0 (test@example.com)",
            rate_limit_per_sec=10,
            transport=transport,
        )

    return _factory


# ---------------------------------------------------------------------------
# Skip POSIX-only tests on Windows.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform != "win32":
        return
    skip_posix = pytest.mark.skip(reason="POSIX-only test")
    for item in items:
        if "posix_only" in item.keywords:
            item.add_marker(skip_posix)


def pytest_configure(config: pytest.Config) -> None:
    # Make sure the CWD doesn't pollute test runs.
    os.environ.pop("SEC_EDGAR_RATE_LIMIT_PER_SEC", None)
