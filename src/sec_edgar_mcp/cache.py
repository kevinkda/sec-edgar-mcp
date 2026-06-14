"""Pluggable response cache for SEC EDGAR data (v0.7 T0).

.. versionchanged:: 0.3.0
    ⚠️ **BREAKING** — the embedded DuckDB cache is removed.  The cache now
    delegates to a pluggable :class:`~sec_edgar_mcp.cache_backend.CacheBackend`:

    * **memory** (default) — in-process LRU + TTL, zero external
      dependency, concurrency-safe, non-blocking (no global ``RLock``,
      no file locks).
    * **clickhouse** (opt-in) — ``pip install sec-edgar-mcp[clickhouse]``
      + ``SEC_EDGAR_CLICKHOUSE_URL`` + ``SEC_EDGAR_CACHE_BACKEND=clickhouse``
      for derived-analysis history persistence.

    Selection via ``SEC_EDGAR_CACHE_BACKEND`` (``memory`` | ``clickhouse``,
    default ``memory``).  Derived-analysis history without ClickHouse
    degrades to a ``requires_clickhouse_persistence`` signal; core tools
    are unaffected.

TTLs (per task spec):
    * filings_index_cache — 24 h
    * form4_cache         — 6  h
    * filing_text_cache   — 30 d
    * search_cache        — 24 h

Failure mode: best-effort — every backend swallows storage errors and the
caller falls through to the live API.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Final

from .cache_backend import (
    CacheBackend,
    get_cache_backend,
)

__all__ = [
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
    "get_cache",
    "reset_cache_singleton",
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TTL_FILINGS_S: Final[int] = 24 * 3600
DEFAULT_TTL_FORM4_S: Final[int] = 6 * 3600
DEFAULT_TTL_FILING_TEXT_S: Final[int] = 30 * 86_400
DEFAULT_TTL_SEARCH_S: Final[int] = 24 * 3600

ENV_CACHE_ENABLED: Final[str] = "SEC_EDGAR_CACHE_ENABLED"
ENV_CACHE_BYPASS: Final[str] = "SEC_EDGAR_CACHE_BYPASS"

_FILINGS_TABLE: Final[str] = "filings_index_cache"
_FORM4_TABLE: Final[str] = "form4_cache"
_SEARCH_TABLE: Final[str] = "search_cache"
_TICKER_TABLE: Final[str] = "ticker_map_cache"
_FILING_TEXT_TABLE: Final[str] = "filing_text_cache"

_GLOBAL_KEY: Final[str] = "global"


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_enabled() -> bool:
    """Honor ``SEC_EDGAR_CACHE_ENABLED`` (default off — opt-in).

    .. versionchanged:: 0.2.4
        cache now opt-in, default disabled.
    """
    return _truthy(os.environ.get(ENV_CACHE_ENABLED), default=False)


def cache_bypass() -> bool:
    """Honor ``SEC_EDGAR_CACHE_BYPASS`` (default off — single-call force fresh)."""
    return _truthy(os.environ.get(ENV_CACHE_BYPASS), default=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_params(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stats payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheStats:
    backend: str
    enabled: bool
    entries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "entries": self.entries,
        }


# ---------------------------------------------------------------------------
# Cache facade
# ---------------------------------------------------------------------------


class Cache:
    """Backend-agnostic response cache.  One instance per process.

    Delegates all storage to a :class:`CacheBackend` (memory by default,
    ClickHouse when opted in).  The legacy per-table public API is kept so
    tools require no changes.
    """

    def __init__(self, backend: CacheBackend | None = None) -> None:
        self.backend: CacheBackend = backend if backend is not None else get_cache_backend()

    def close(self) -> None:
        # Pluggable backends own their own lifecycle; nothing to close for
        # the memory backend, and the ClickHouse client is process-scoped.
        return None

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.close()

    # ----------------------------------------------- generic JSON tables

    def _get(self, table: str, key: str) -> dict[str, Any] | None:
        return self.backend.get(table, key)

    def _put(self, table: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        self.backend.set(table, key, value, ttl_seconds)

    # ---------------------------------------------- per-table public APIs

    def get_filings_index(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get(_FILINGS_TABLE, _hash_params(params))

    def put_filings_index(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        cik: str | None = None,
    ) -> None:
        del cik  # retained for API compatibility; backend keys on params hash
        self._put(_FILINGS_TABLE, _hash_params(params), raw, DEFAULT_TTL_FILINGS_S)

    def get_form4(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get(_FORM4_TABLE, _hash_params(params))

    def put_form4(
        self,
        params: dict[str, Any],
        raw: dict[str, Any],
        *,
        cik: str | None = None,
    ) -> None:
        del cik
        self._put(_FORM4_TABLE, _hash_params(params), raw, DEFAULT_TTL_FORM4_S)

    def get_search(self, params: dict[str, Any]) -> dict[str, Any] | None:
        return self._get(_SEARCH_TABLE, _hash_params(params))

    def put_search(self, params: dict[str, Any], raw: dict[str, Any]) -> None:
        self._put(_SEARCH_TABLE, _hash_params(params), raw, DEFAULT_TTL_SEARCH_S)

    def get_ticker_map(self) -> dict[str, Any] | None:
        return self._get(_TICKER_TABLE, _GLOBAL_KEY)

    def put_ticker_map(self, raw: dict[str, Any]) -> None:
        self._put(_TICKER_TABLE, _GLOBAL_KEY, raw, DEFAULT_TTL_FILINGS_S)

    # --------------------------------------------------- filing text TTL

    @staticmethod
    def _filing_text_key(accession_number: str, document_type: str) -> str:
        return f"{accession_number}\x1f{document_type}"

    def get_filing_text(self, accession_number: str, document_type: str) -> dict[str, Any] | None:
        hit = self._get(_FILING_TEXT_TABLE, self._filing_text_key(accession_number, document_type))
        if hit is None:
            return None
        return {
            "accession_number": accession_number,
            "document_type": document_type,
            "content_type": hit.get("content_type"),
            "text": hit.get("text"),
            "byte_size": int(hit.get("byte_size", 0) or 0),
            "truncated": bool(hit.get("truncated", False)),
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
        self._put(
            _FILING_TEXT_TABLE,
            self._filing_text_key(accession_number, document_type),
            {
                "content_type": content_type,
                "text": text,
                "byte_size": byte_size,
                "truncated": truncated,
            },
            DEFAULT_TTL_FILING_TEXT_S,
        )

    # --------------------------------------------------------------- stats

    def get_stats(self) -> CacheStats:
        try:
            entries = self.backend.size()
        except Exception:
            entries = 0
        return CacheStats(
            backend=self.backend.name,
            enabled=cache_enabled(),
            entries=entries,
        )

    def reset(self) -> None:
        """Drop all rows.  Test-only convenience."""
        self.backend.clear()


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
    """Test helper — drop the singleton so the next call re-creates it."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
            _singleton = None
