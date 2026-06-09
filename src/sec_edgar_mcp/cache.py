"""DuckDB-backed local cache for SEC EDGAR responses.

TTLs (per task spec):
    * filings_index_cache — 24 h
    * form4_cache         — 6  h
    * filing_text_cache   — 30 d
    * search_cache        — 24 h

Storage layout follows ``${XDG_STATE_HOME}/sec-edgar-mcp/cache.duckdb``,
parent ``0o700``, file ``0o600`` on POSIX.

Failure mode: best-effort — every method swallows DuckDB / IO errors at
WARNING level and the caller falls through to the live API.  A corrupt
DB is renamed aside (``cache.duckdb.corrupt-<ts>``) and a fresh one is
created.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import duckdb

from . import _platform

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TTL_FILINGS_S: Final[int] = 24 * 3600
DEFAULT_TTL_FORM4_S: Final[int] = 6 * 3600
DEFAULT_TTL_FILING_TEXT_S: Final[int] = 30 * 86_400
DEFAULT_TTL_SEARCH_S: Final[int] = 24 * 3600

CACHE_DB_FILENAME: Final[str] = "cache.duckdb"
CACHE_DIR_NAME: Final[str] = "sec-edgar-mcp"

ENV_CACHE_ENABLED: Final[str] = "SEC_EDGAR_CACHE_ENABLED"
ENV_CACHE_BYPASS: Final[str] = "SEC_EDGAR_CACHE_BYPASS"


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_enabled() -> bool:
    """Honor ``SEC_EDGAR_CACHE_ENABLED`` (default off — opt-in).

    .. versionchanged:: 0.2.4
        cache now opt-in, default disabled.  Set ``SEC_EDGAR_CACHE_ENABLED=true``
        to re-enable the DuckDB read-through cache.
    """
    return _truthy(os.environ.get(ENV_CACHE_ENABLED), default=False)


def cache_bypass() -> bool:
    """Honor ``SEC_EDGAR_CACHE_BYPASS`` (default off — single-call force fresh)."""
    return _truthy(os.environ.get(ENV_CACHE_BYPASS), default=False)


def state_root() -> Path:
    """Cross-platform state-directory root (delegates to ``_platform``)."""
    return _platform.state_root()


def default_db_path() -> Path:
    """Canonical cache DB path under ``$XDG_STATE_HOME``."""
    return state_root() / CACHE_DIR_NAME / CACHE_DB_FILENAME


# ---------------------------------------------------------------------------
# Schema (DDL)
# ---------------------------------------------------------------------------

_SCHEMA_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS filings_index_cache (
        cache_key VARCHAR PRIMARY KEY,
        cik VARCHAR,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 86400
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS form4_cache (
        cache_key VARCHAR PRIMARY KEY,
        cik VARCHAR,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 21600
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filing_text_cache (
        accession_number VARCHAR,
        document_type VARCHAR,
        fetched_at TIMESTAMP,
        content_type VARCHAR,
        text_body TEXT,
        byte_size BIGINT,
        truncated BOOLEAN,
        ttl_seconds INTEGER DEFAULT 2592000,
        PRIMARY KEY (accession_number, document_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_cache (
        cache_key VARCHAR PRIMARY KEY,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 86400
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticker_map_cache (
        cache_key VARCHAR PRIMARY KEY,
        fetched_at TIMESTAMP,
        raw_json JSON,
        ttl_seconds INTEGER DEFAULT 86400
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cache_events (
        ts TIMESTAMP,
        kind VARCHAR,
        table_name VARCHAR
    )
    """,
)

_TABLE_NAMES: Final[tuple[str, ...]] = (
    "filings_index_cache",
    "form4_cache",
    "filing_text_cache",
    "search_cache",
    "ticker_map_cache",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _hash_params(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _is_expired(fetched_at: Any, ttl_seconds: Any) -> bool:
    if fetched_at is None or ttl_seconds is None:
        return True
    if not isinstance(fetched_at, datetime):
        parsed = _parse_dt(fetched_at)
        if parsed is None:
            return True
        fetched_at = parsed
    age = _utcnow() - fetched_at
    return bool(age.total_seconds() > float(ttl_seconds))


def _deserialise(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


# ---------------------------------------------------------------------------
# Stats payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheStats:
    db_path: str
    enabled: bool
    size_mb: float
    rows_per_table: dict[str, int]
    expired_rows: dict[str, int]
    hit_rate_24h: float | None
    hits_24h: int
    misses_24h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "enabled": self.enabled,
            "size_mb": round(self.size_mb, 4),
            "rows_per_table": dict(self.rows_per_table),
            "expired_rows": dict(self.expired_rows),
            "hit_rate_24h": self.hit_rate_24h,
            "hits_24h": self.hits_24h,
            "misses_24h": self.misses_24h,
        }


# ---------------------------------------------------------------------------
# Cache class
# ---------------------------------------------------------------------------


class Cache:
    """DuckDB-backed cache.  One instance per process."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._lock = threading.RLock()
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._open()

    def _ensure_parent(self) -> None:
        parent = self.db_path.parent
        with _platform.restrictive_umask():
            parent.mkdir(parents=True, exist_ok=True)
        # Best-effort tighten on POSIX (no-op on Windows; restrictive_umask
        # already prevented broad bits from being set on creation).
        if parent.exists() and not _platform.IS_WINDOWS:  # pragma: no branch - parent always exists post-mkdir
            _platform.secure_chmod(parent, 0o700)

    def _open(self) -> None:
        try:
            self._ensure_parent()
            self._conn = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA_DDL:
                self._conn.execute(stmt)
            _platform.secure_chmod(self.db_path, 0o600)
        except (duckdb.Error, OSError) as exc:
            log.warning('{"event":"cache_open_failed","path":"%s","error":"%s"}', self.db_path, exc)
            self._quarantine_and_reopen(exc)

    def _quarantine_and_reopen(self, original_exc: Exception) -> None:
        if not self.db_path.exists():
            self._conn = None
            return
        ts = int(time.time())
        backup = self.db_path.with_suffix(self.db_path.suffix + f".corrupt-{ts}")
        try:
            os.rename(self.db_path, backup)
            log.warning(
                '{"event":"cache_quarantined","backup":"%s","original_error":"%s"}',
                backup,
                original_exc,
            )
        except OSError as exc:
            log.warning('{"event":"cache_quarantine_failed","error":"%s"}', exc)
            self._conn = None
            return
        try:
            self._conn = duckdb.connect(str(self.db_path))
            for stmt in _SCHEMA_DDL:
                self._conn.execute(stmt)
            _platform.secure_chmod(self.db_path, 0o600)
        except (duckdb.Error, OSError) as exc:
            log.warning('{"event":"cache_reopen_failed","error":"%s"}', exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(duckdb.Error):
                    self._conn.close()
                self._conn = None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.close()

    def _record_event(self, kind: str, table: str) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO cache_events (ts, kind, table_name) VALUES (?, ?, ?)",
                [_utcnow(), kind, table],
            )
        except duckdb.Error:
            pass

    # ----------------------------------------------- generic JSON tables

    def _get_json_row(self, table: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    f"SELECT raw_json, fetched_at, ttl_seconds FROM {table} WHERE cache_key = ?",  # noqa: S608
                    [key],
                ).fetchone()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_get_failed","table":"%s","error":"%s"}',
                    table,
                    exc,
                )
                return None
            if row is None:
                self._record_event("miss", table)
                return None
            raw, fetched_at, ttl = row
            if _is_expired(fetched_at, ttl):
                self._record_event("expired", table)
                return None
            self._record_event("hit", table)
            return _deserialise(raw)

    def _put_json_row(
        self,
        table: str,
        key: str,
        raw: dict[str, Any],
        ttl_seconds: int,
        cik: str | None = None,
    ) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                if cik is not None:
                    self._conn.execute(
                        f"INSERT OR REPLACE INTO {table} "  # noqa: S608
                        "(cache_key, cik, fetched_at, raw_json, ttl_seconds) VALUES (?, ?, ?, ?, ?)",
                        [key, cik, _utcnow(), json.dumps(raw, default=str), ttl_seconds],
                    )
                else:
                    self._conn.execute(
                        f"INSERT OR REPLACE INTO {table} "  # noqa: S608
                        "(cache_key, fetched_at, raw_json, ttl_seconds) VALUES (?, ?, ?, ?)",
                        [key, _utcnow(), json.dumps(raw, default=str), ttl_seconds],
                    )
                self._record_event("write", table)
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"%s","error":"%s"}',
                    table,
                    exc,
                )

    # ---------------------------------------------- per-table public APIs

    def get_filings_index(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get_json_row("filings_index_cache", _hash_params(params))

    def put_filings_index(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        cik: str | None = None,
    ) -> None:
        self._put_json_row(
            "filings_index_cache",
            _hash_params(params),
            raw,
            DEFAULT_TTL_FILINGS_S,
            cik=cik,
        )

    def get_form4(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get_json_row("form4_cache", _hash_params(params))

    def put_form4(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        cik: str | None = None,
    ) -> None:
        self._put_json_row(
            "form4_cache",
            _hash_params(params),
            raw,
            DEFAULT_TTL_FORM4_S,
            cik=cik,
        )

    def get_search(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get_json_row("search_cache", _hash_params(params))

    def put_search(self, params: dict[str, Any], raw: dict[str, Any]) -> None:
        self._put_json_row(
            "search_cache",
            _hash_params(params),
            raw,
            DEFAULT_TTL_SEARCH_S,
        )

    def get_ticker_map(self) -> dict[str, Any] | None:
        return self._get_json_row("ticker_map_cache", "global")

    def put_ticker_map(self, raw: dict[str, Any]) -> None:
        self._put_json_row(
            "ticker_map_cache",
            "global",
            raw,
            DEFAULT_TTL_FILINGS_S,
        )

    # --------------------------------------------------- filing text TTL

    def get_filing_text(self, accession_number: str, document_type: str) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT content_type, text_body, byte_size, truncated, fetched_at, ttl_seconds "
                    "FROM filing_text_cache WHERE accession_number = ? AND document_type = ?",
                    [accession_number, document_type],
                ).fetchone()
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_get_failed","table":"filing_text_cache","error":"%s"}',
                    exc,
                )
                return None
            if row is None:
                self._record_event("miss", "filing_text_cache")
                return None
            ctype, body, size, truncated, fetched_at, ttl = row
            if _is_expired(fetched_at, ttl):
                self._record_event("expired", "filing_text_cache")
                return None
            self._record_event("hit", "filing_text_cache")
            return {
                "accession_number": accession_number,
                "document_type": document_type,
                "content_type": ctype,
                "text": body,
                "byte_size": int(size) if size is not None else 0,
                "truncated": bool(truncated),
            }

    def put_filing_text(
        self,
        accession_number: str,
        document_type: str,
        *,
        content_type: str,
        text: str,
        byte_size: int,
        truncated: bool,
    ) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO filing_text_cache (
                        accession_number, document_type, fetched_at,
                        content_type, text_body, byte_size, truncated, ttl_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        accession_number,
                        document_type,
                        _utcnow(),
                        content_type,
                        text,
                        byte_size,
                        truncated,
                        DEFAULT_TTL_FILING_TEXT_S,
                    ],
                )
                self._record_event("write", "filing_text_cache")
            except duckdb.Error as exc:
                log.warning(
                    '{"event":"cache_put_failed","table":"filing_text_cache","error":"%s"}',
                    exc,
                )

    # --------------------------------------------------------------- stats

    def get_stats(self) -> CacheStats:
        rows: dict[str, int] = {}
        expired: dict[str, int] = {}
        hits = 0
        misses = 0
        size_mb = 0.0
        if self.db_path.exists():
            try:
                size_mb = self.db_path.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0.0
        with self._lock:
            if self._conn is not None:
                for tbl in _TABLE_NAMES:
                    try:
                        c = self._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()  # noqa: S608
                        rows[tbl] = int(c[0]) if c else 0
                    except duckdb.Error:
                        rows[tbl] = 0
                    expired[tbl] = self._count_expired(tbl)
                cutoff = _utcnow() - timedelta(hours=24)
                try:
                    h = self._conn.execute(
                        "SELECT COUNT(*) FROM cache_events WHERE kind = 'hit' AND ts >= ?",
                        [cutoff],
                    ).fetchone()
                    hits = int(h[0]) if h else 0
                    m = self._conn.execute(
                        "SELECT COUNT(*) FROM cache_events WHERE kind IN ('miss', 'expired') AND ts >= ?",
                        [cutoff],
                    ).fetchone()
                    misses = int(m[0]) if m else 0
                except duckdb.Error:
                    pass
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else None
        return CacheStats(
            db_path=str(self.db_path),
            enabled=cache_enabled(),
            size_mb=size_mb,
            rows_per_table=rows,
            expired_rows=expired,
            hit_rate_24h=hit_rate,
            hits_24h=hits,
            misses_24h=misses,
        )

    def _count_expired(self, table: str) -> int:
        if self._conn is None or table not in _TABLE_NAMES:
            return 0
        try:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} "  # noqa: S608
                "WHERE fetched_at + INTERVAL (ttl_seconds) SECOND < CURRENT_TIMESTAMP"
            ).fetchone()
            return int(row[0]) if row else 0
        except duckdb.Error:
            return 0

    def reset(self) -> None:
        """Drop all rows.  Test-only convenience."""
        with self._lock:
            if self._conn is None:
                return
            for tbl in (*_TABLE_NAMES, "cache_events"):
                try:
                    self._conn.execute(f"DELETE FROM {tbl}")  # noqa: S608
                except duckdb.Error:
                    pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Cache | None = None
_singleton_lock = threading.Lock()


def get_cache() -> Cache | None:
    """Return the process-wide cache, or ``None`` if disabled."""
    if not cache_enabled():
        return None
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:  # pragma: no branch - double-checked lock; race side not deterministically testable
            _singleton = Cache()
    return _singleton


def reset_cache_singleton() -> None:
    """Test helper — close the singleton so the next call re-opens it."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None


__all__ = [
    "CACHE_DB_FILENAME",
    "CACHE_DIR_NAME",
    "DEFAULT_TTL_FILINGS_S",
    "DEFAULT_TTL_FILING_TEXT_S",
    "DEFAULT_TTL_FORM4_S",
    "DEFAULT_TTL_SEARCH_S",
    "ENV_CACHE_BYPASS",
    "ENV_CACHE_ENABLED",
    "Cache",
    "CacheStats",
    "cache_bypass",
    "cache_enabled",
    "default_db_path",
    "get_cache",
    "reset_cache_singleton",
    "state_root",
]
