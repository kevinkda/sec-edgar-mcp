"""Unit tests for sec_edgar_mcp.cache (DuckDB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from sec_edgar_mcp.cache import (
    Cache,
    CacheStats,
    cache_bypass,
    cache_enabled,
    default_db_path,
    get_cache,
    reset_cache_singleton,
    state_root,
)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache.duckdb")


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def test_cache_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_EDGAR_CACHE_ENABLED", raising=False)
    assert cache_enabled() is True


@pytest.mark.parametrize("val,expected", [("1", True), ("yes", True), ("0", False), ("no", False)])
def test_cache_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str, expected: bool) -> None:
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", val)
    assert cache_enabled() is expected


def test_cache_bypass_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_EDGAR_CACHE_BYPASS", raising=False)
    assert cache_bypass() is False


def test_default_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = default_db_path()
    assert "sec-edgar-mcp" in str(p)
    assert p.name == "cache.duckdb"


def test_state_root_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_root() == tmp_path


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

    def test_different_params_miss(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"x": 1})
        assert cache.get_filings_index({"k": 2}) is None


class TestForm4:
    def test_miss(self, cache: Cache) -> None:
        assert cache.get_form4({"k": 1}) is None

    def test_hit(self, cache: Cache) -> None:
        cache.put_form4({"k": 1}, {"transactions": [], "issuer": {"cik": "1"}})
        out = cache.get_form4({"k": 1})
        assert out is not None


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

    def test_miss(self, cache: Cache) -> None:
        assert cache.get_filing_text("0000000000-99-999999", "primary") is None


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    def test_filings_index_expired(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"x": 1})
        assert cache._conn is not None
        # Rewrite fetched_at to 25 hours ago.
        old = (datetime.now(tz=UTC).replace(tzinfo=None)) - timedelta(hours=25)
        cache._conn.execute(
            "UPDATE filings_index_cache SET fetched_at = ?",
            [old],
        )
        assert cache.get_filings_index({"k": 1}) is None

    def test_form4_expired(self, cache: Cache) -> None:
        cache.put_form4({"k": 1}, {"x": 1})
        assert cache._conn is not None
        old = (datetime.now(tz=UTC).replace(tzinfo=None)) - timedelta(hours=7)
        cache._conn.execute("UPDATE form4_cache SET fetched_at = ?", [old])
        assert cache.get_form4({"k": 1}) is None

    def test_filing_text_expired(self, cache: Cache) -> None:
        cache.put_filing_text(
            "0000320193-24-000123",
            "primary",
            content_type="text/html",
            text="x",
            byte_size=1,
            truncated=False,
        )
        assert cache._conn is not None
        old = (datetime.now(tz=UTC).replace(tzinfo=None)) - timedelta(days=31)
        cache._conn.execute("UPDATE filing_text_cache SET fetched_at = ?", [old])
        assert cache.get_filing_text("0000320193-24-000123", "primary") is None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_empty(self, cache: Cache) -> None:
        s = cache.get_stats()
        assert isinstance(s, CacheStats)
        assert s.hits_24h == 0
        assert s.misses_24h == 0
        assert s.hit_rate_24h is None

    def test_hit_recorded(self, cache: Cache) -> None:
        cache.put_filings_index({"k": 1}, {"x": 1})
        cache.get_filings_index({"k": 1})  # hit
        cache.get_filings_index({"k": 99})  # miss
        s = cache.get_stats()
        assert s.hits_24h >= 1
        assert s.misses_24h >= 1
        assert s.hit_rate_24h is not None

    def test_to_dict_shape(self, cache: Cache) -> None:
        d = cache.get_stats().to_dict()
        for key in (
            "db_path",
            "enabled",
            "size_mb",
            "rows_per_table",
            "expired_rows",
            "hit_rate_24h",
            "hits_24h",
            "misses_24h",
        ):
            assert key in d


# ---------------------------------------------------------------------------
# Reset / corruption isolation
# ---------------------------------------------------------------------------


def test_reset_drops_rows(cache: Cache) -> None:
    cache.put_filings_index({"k": 1}, {"x": 1})
    cache.reset()
    assert cache.get_filings_index({"k": 1}) is None


def test_corrupt_db_quarantined(tmp_path: Path) -> None:
    db = tmp_path / "cache.duckdb"
    db.write_bytes(b"this is not a valid duckdb file" * 1000)
    cache = Cache(db)
    # The corrupt file gets quarantined; the new DB is reopened.
    assert any(p.name.startswith("cache.duckdb.corrupt-") for p in tmp_path.iterdir())
    cache.put_filings_index({"k": 1}, {"x": 1})
    assert cache.get_filings_index({"k": 1}) is not None
    cache.close()


def test_close_idempotent(cache: Cache) -> None:
    cache.close()
    cache.close()


def test_context_manager(tmp_path: Path) -> None:
    with Cache(tmp_path / "ctx.duckdb") as c:
        c.put_search({"q": "x"}, {"y": 1})
        assert c.get_search({"q": "x"}) is not None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "0")
    reset_cache_singleton()
    assert get_cache() is None


def test_singleton_enabled_returns_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SEC_EDGAR_CACHE_ENABLED", "1")
    reset_cache_singleton()
    a = get_cache()
    b = get_cache()
    assert a is b


# ---------------------------------------------------------------------------
# Error paths — DuckDB connection lost
# ---------------------------------------------------------------------------


def test_get_when_connection_lost(cache: Cache) -> None:
    # Force the connection closed; subsequent gets must return None
    # without raising.
    cache._conn = None
    assert cache.get_filings_index({"k": 1}) is None
    assert cache.get_form4({"k": 1}) is None
    assert cache.get_search({"k": 1}) is None
    assert cache.get_filing_text("0000000000-99-999999", "primary") is None
    cache.put_filings_index({"k": 1}, {"x": 1})  # no-op
    cache.put_filing_text("a", "primary", content_type="x", text="y", byte_size=1, truncated=False)


def test_count_expired_unknown_table(cache: Cache) -> None:
    # private API exercise — should never raise
    assert cache._count_expired("not_a_table") == 0


def test_db_open_error_does_not_propagate(tmp_path: Path) -> None:
    """Open a path that already exists as a directory — DuckDB raises and
    Cache should swallow into _conn = None."""
    bad = tmp_path / "dirpath"
    bad.mkdir()
    c = Cache(bad)
    # DuckDB should fail to open a directory; cache silently degrades.
    # On some versions DuckDB may instead store inside the dir; either way
    # the cache must not raise.
    assert isinstance(c, Cache)


def test_duckdb_imports() -> None:
    # sanity: duckdb is importable; otherwise the rest of these tests are noise.
    assert hasattr(duckdb, "connect")


# ---------------------------------------------------------------------------
# Internal helpers — exercise tolerant parsing of fetched_at / raw_json
# ---------------------------------------------------------------------------


def test_parse_dt_accepts_iso_string() -> None:
    from sec_edgar_mcp.cache import _parse_dt

    out = _parse_dt("2024-05-01T12:34:56+00:00")
    assert out is not None
    assert out.year == 2024


def test_parse_dt_handles_z_suffix_and_garbage() -> None:
    from sec_edgar_mcp.cache import _parse_dt

    assert _parse_dt("2024-05-01T12:34:56Z") is not None
    assert _parse_dt("not-a-date") is None
    assert _parse_dt(None) is None
    assert _parse_dt(12345) is None


def test_parse_dt_strips_tzinfo_for_aware_datetime() -> None:
    from sec_edgar_mcp.cache import _parse_dt

    dt = datetime(2024, 5, 1, 12, 34, tzinfo=UTC)
    out = _parse_dt(dt)
    assert out is not None
    assert out.tzinfo is None


def test_is_expired_handles_none_and_garbage() -> None:
    from sec_edgar_mcp.cache import _is_expired

    assert _is_expired(None, 60) is True
    assert _is_expired(datetime.now(tz=UTC).replace(tzinfo=None), None) is True
    assert _is_expired("garbage-iso", 60) is True


def test_deserialise_handles_garbage() -> None:
    from sec_edgar_mcp.cache import _deserialise

    assert _deserialise(None) is None
    assert _deserialise({"k": 1}) == {"k": 1}
    assert _deserialise('{"k": 1}') == {"k": 1}
    assert _deserialise("not-json") is None
    assert _deserialise(b'{"k": 1}') == {"k": 1}
    assert _deserialise(12345) is None
    # Valid JSON but not a dict.
    assert _deserialise("[1,2,3]") is None
