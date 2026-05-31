"""Shared runtime helper for the 4 business tool modules.

Each business tool needs:

1. A lazily-constructed :class:`SecEdgarClient` (so tests can override the
   transport via env) — created **once per server process**.
2. Optional DuckDB cache lookup / store hooks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..cache import Cache, cache_bypass, get_cache
from ..client import SecEdgarClient, make_client

_lock = asyncio.Lock()
_client: SecEdgarClient | None = None


async def get_client() -> SecEdgarClient:
    """Lazily instantiate the SEC client (one per process)."""
    global _client
    if _client is None:
        async with _lock:
            if _client is None:  # pragma: no branch - double-checked lock; race side not deterministically testable
                _client = make_client()
    return _client


def reset_client_cache() -> None:
    """Clear the cached client.  Used by integration tests between scenarios."""
    global _client
    _client = None


async def set_client_for_tests(client: SecEdgarClient | None) -> None:
    """Inject a custom client (test-only)."""
    global _client
    _client = client


CacheLookup = Callable[[Cache], "dict[str, Any] | None"]
CacheStore = Callable[[Cache, "dict[str, Any]"], None]


async def call_with_cache(
    fetch: Callable[[SecEdgarClient], Awaitable[dict[str, Any]]],
    *,
    cache_lookup: CacheLookup | None = None,
    cache_store: CacheStore | None = None,
) -> dict[str, Any]:
    """Run *fetch(client)* with optional cache short-circuit.

    The returned dict carries a ``_cache_status`` field
    (``"hit" | "miss" | "bypass" | "disabled"``).
    """
    cache = get_cache() if (cache_lookup is not None or cache_store is not None) else None
    bypass = cache_bypass()
    cache_status = "disabled"

    if cache is not None and not bypass and cache_lookup is not None:
        try:
            hit = cache_lookup(cache)
        except Exception:
            hit = None
        if isinstance(hit, dict):
            payload = dict(hit)
            payload["_cache_status"] = "hit"
            return payload

    if cache is not None and bypass:
        cache_status = "bypass"
    elif cache is not None:
        cache_status = "miss"

    client = await get_client()
    payload = await fetch(client)

    if cache is not None and not bypass and cache_store is not None:
        try:
            cache_store(cache, payload)
        except Exception:  # noqa: S110 - cache errors must never break tools
            pass
    payload = dict(payload)
    payload["_cache_status"] = cache_status
    return payload


__all__ = [
    "CacheLookup",
    "CacheStore",
    "call_with_cache",
    "get_client",
    "reset_client_cache",
    "set_client_for_tests",
]
