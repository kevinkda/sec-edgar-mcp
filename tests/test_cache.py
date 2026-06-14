"""Unit tests for sec_edgar_mcp.cache (pluggable-backend facade)."""

from __future__ import annotations

import time

import pytest

from sec_edgar_mcp.cache import (
    Cache,
    CacheStats,
    cache_bypass,
    cache_enabled,
    get_cache,
    reset_cache_singleton,
)
from sec_edgar_mcp.cache_backend import MemoryBackend


@pytest.fixture
def cache() -> Cache:
    return Cache(backend=MemoryBackend())


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def test_cache_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """v0.2.4 BREAKING: unset env var defaults to disabled (opt-in)."""
    monkeypatch.delenv("SEC_EDGAR_CACHE_ENABLED", raising=False)
    assert cache_enabled() is False


@pytest.mark.parametrize(
    "val",
    ["1", "true", "yes", "on", "TRUE", "Yes", "On", " true ", "  1 ", "\tyes\n"],
)
def test_cache_enabled_truthy_matrix(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    """opt-in flag accepts 1/true/yes/on across case + surrounding whitespace."""
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", val)
    assert cache_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "nope", "2", "", "   "])
def test_cache_enabled_falsy_matrix(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    """Anything outside the truthy set (incl. empty / whitespace-only) → disabled."""
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", val)
    assert cache_enabled() is False


def test_cache_enabled_unset_get_cache_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """unset → disabled → get_cache() returns None (no backend constructed)."""
    monkeypatch.delenv("SEC_EDGAR_CACHE_ENABLED", raising=False)
    reset_cache_singleton()
    assert cache_enabled() is False
    assert get_cache() is None


def test_cache_bypass_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_EDGAR_CACHE_BYPASS", raising=False)
    assert cache_bypass() is False


# ---------------------------------------------------------------------------
# Generic JSON-row tables (filings_index, form4, search, ticker_map)
# ---------------------------------------------------------------------------


class TestFilingsIndex:
    def test_miss(self, cache: Cache) -> None:
        assert cache.get_filings_index({"k": 1}) is None

    def test_hit(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"company": {"cik": "0000000001"}, "filings": []})
        out = cache.get_filings_index({"k": 1})
        assert out is not None
        assert out["company"]["cik"] == "0000000001"

    def test_hit_with_cik_kwarg(self, cache: Cache) -> None:
        """The retained ``cik`` kwarg is accepted (API compatibility)."""
        cache.put_filings_index({"k": 1}, {"x": 1}, cik="0000000001")
        assert cache.get_filings_index({"k": 1}) == {"x": 1}

    def test_different_params_miss(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"x": 1})
        assert cache.get_filings_index({"k": 2}) is None


class TestForm4:
    def test_miss(self, cache: Cache) -> None:
        assert cache.get_form4({"k": 1}) is None

    def test_hit(self, cache: Cache) -> None:
        cache.put_form4({"k": 1}, {"transactions": [], "issuer": {"cik": "1"}})
        assert cache.get_form4({"k": 1}) is not None

    def test_hit_with_cik_kwarg(self, cache: Cache) -> None:
        cache.put_form4({"k": 1}, {"x": 1}, cik="1")
        assert cache.get_form4({"k": 1}) == {"x": 1}


class TestSearch:
    def test_round_trip(self, cache: Cache) -> None:
        cache.put_search({"q": "x"}, {"results": [], "total_hits": 0})
        out = cache.get_search({"q": "x"})
        assert out is not None
        assert out["total_hits"] == 0


class TestTickerMap:
    def test_round_trip(self, cache: Cache) -> None:
        assert cache.get_ticker_map() is None
        cache.put_ticker_map({"AAPL": "0000320193"})
        assert cache.get_ticker_map() == {"AAPL": "0000320193"}


class TestFilingText:
    def test_round_trip(self, cache: Cache) -> None:
        cache.put_filing_text(
            "0000320193-24-000123",
            "primary",
            content_type="text/html",
            text="<html>hi</html>",
            byte_size=15,
            truncated=False,
        )
        hit = cache.get_filing_text("0000320193-24-000123", "primary")
        assert hit is not None
        assert hit["text"] == "<html>hi</html>"
        assert hit["truncated"] is False
        assert hit["content_type"] == "text/html"
        assert hit["byte_size"] == 15

    def test_miss(self, cache: Cache) -> None:
        assert cache.get_filing_text("0000000000-99-999999", "primary") is None


# ---------------------------------------------------------------------------
# TTL expiry (memory backend honors per-entry TTL)
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    def test_zero_ttl_expires_immediately(self) -> None:
        backend = MemoryBackend()
        backend.set("filings_index_cache", "k", {"x": 1}, 0)
        # monotonic clock has advanced past expiry of a 0-second TTL.
        assert backend.get("filings_index_cache", "k") is None

    def test_short_ttl_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = MemoryBackend()
        backend.set("search_cache", "k", {"x": 1}, 1)
        # Fast-forward monotonic time past the TTL.
        base = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: base + 2.0)
        assert backend.get("search_cache", "k") is None

    def test_unexpired_within_ttl(self) -> None:
        backend = MemoryBackend()
        backend.set("search_cache", "k", {"x": 1}, 3600)
        assert backend.get("search_cache", "k") == {"x": 1}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty(self, cache: Cache) -> None:
        s = cache.get_stats()
        assert isinstance(s, CacheStats)
        assert s.entries == 0
        assert s.backend == "memory"

    def test_entries_counted(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"x": 1})
        cache.put_search({"q": "y"}, {"z": 2})
        s = cache.get_stats()
        assert s.entries == 2

    def test_to_dict_shape(self, cache: Cache) -> None:
        d = cache.get_stats().to_dict()
        for key in ("backend", "enabled", "entries"):
            assert key in d


# ---------------------------------------------------------------------------
# Reset / lifecycle
# ---------------------------------------------------------------------------


def test_reset_drops_rows(cache: Cache) -> None:
    cache.put_filings_index({"k": 1}, {"x": 1})
    cache.reset()
    assert cache.get_filings_index({"k": 1}) is None


def test_close_idempotent(cache: Cache) -> None:
    cache.close()
    cache.close()


def test_context_manager() -> None:
    with Cache(backend=MemoryBackend()) as c:
        c.put_search({"q": "x"}, {"y": 1})
        assert c.get_search({"q": "x"}) is not None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
    reset_cache_singleton()
    assert get_cache() is None


def test_singleton_enabled_returns_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "1")
    monkeypatch.delenv("SEC_EDGAR_CACHE_BACKEND", raising=False)
    reset_cache_singleton()
    a = get_cache()
    b = get_cache()
    assert a is b
    assert a is not None
    assert a.backend.name == "memory"
    reset_cache_singleton()


def test_default_facade_uses_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A facade built with no explicit backend picks up the env-selected one."""
    monkeypatch.delenv("SEC_EDGAR_CACHE_BACKEND", raising=False)
    c = Cache()
    assert c.backend.name == "memory"


# ---------------------------------------------------------------------------
# Defensive isolation — cached values are copied, not shared
# ---------------------------------------------------------------------------


def test_cached_value_is_isolated(cache: Cache) -> None:
    payload = {"results": [1, 2, 3]}
    cache.put_search({"q": "x"}, payload)
    payload["results"].append(4)  # mutate caller's copy after store
    out = cache.get_search({"q": "x"})
    assert out == {"results": [1, 2, 3]}
    out["results"].append(99)  # mutate returned copy
    again = cache.get_search({"q": "x"})
    assert again == {"results": [1, 2, 3]}
