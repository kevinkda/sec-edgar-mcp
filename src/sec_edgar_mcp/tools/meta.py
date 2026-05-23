"""Meta tools: ``health_check``, ``get_server_info``, ``get_cache_stats``.

These are local-only — they never touch SEC EDGAR so they remain available
even when ``SEC_EDGAR_USER_AGENT`` is unset.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import mcp

from ..cache import cache_enabled, get_cache
from ..client import (
    ENV_USER_AGENT,
    SEC_HARD_RATE_LIMIT_PER_SEC,
    resolve_rate_limit,
)
from ..errors import SecConfigurationError
from ..models import supported_tool_names

# Captured at import time so health_check stays offline-safe.
_SERVER_VERSION: str | None = None


def _safe_user_agent_status() -> dict[str, Any]:
    """Check User-Agent without raising into the caller."""
    raw = os.environ.get(ENV_USER_AGENT, "").strip()
    if not raw:
        return {"configured": False, "reason": "missing"}
    try:
        from ..client import resolve_user_agent

        resolve_user_agent()
    except SecConfigurationError as exc:
        return {"configured": False, "reason": exc.hint}
    return {"configured": True, "reason": None}


def _safe_cache_summary() -> dict[str, Any]:
    if not cache_enabled():
        return {"enabled": False, "size_mb": 0.0, "hit_rate_24h": None}
    cache = get_cache()
    if cache is None:
        return {"enabled": False, "size_mb": 0.0, "hit_rate_24h": None}
    try:
        stats = cache.get_stats()
    except Exception:
        return {"enabled": True, "size_mb": 0.0, "hit_rate_24h": None}
    return {
        "enabled": stats.enabled,
        "size_mb": round(stats.size_mb, 4),
        "hit_rate_24h": stats.hit_rate_24h,
    }


async def health_check_impl() -> dict[str, Any]:
    """Local health probe — never calls SEC."""
    ua = _safe_user_agent_status()
    cache_summary = _safe_cache_summary()
    return {
        "server_version": _SERVER_VERSION,
        "user_agent_configured": ua["configured"],
        "user_agent_reason": ua["reason"],
        "rate_limit_per_sec": resolve_rate_limit() if ua["configured"] else None,
        "rate_limit_hard_cap": SEC_HARD_RATE_LIMIT_PER_SEC,
        "cache_enabled": cache_summary["enabled"],
        "cache_size_mb": cache_summary["size_mb"],
        "cache_hit_rate_24h": cache_summary["hit_rate_24h"],
        "platform_supported": True,
    }


async def get_server_info_impl(*, server_version: str) -> dict[str, Any]:
    """Local server metadata — version + tool list.  Never calls SEC."""
    global _SERVER_VERSION
    _SERVER_VERSION = server_version
    return {
        "server_version": server_version,
        "mcp_sdk_version": getattr(mcp, "__version__", "unknown"),
        "python_version": (f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
        "supported_tools": supported_tool_names(),
        "platform_supported_v1": ["macos>=11", "linux"],
    }


async def get_cache_stats_impl() -> dict[str, Any]:
    """Local DuckDB cache health — never calls SEC."""
    cache = get_cache()
    if cache is None:
        return {
            "db_path": None,
            "enabled": False,
            "size_mb": 0.0,
            "rows_per_table": {},
            "expired_rows": {},
            "hit_rate_24h": None,
            "hits_24h": 0,
            "misses_24h": 0,
        }
    try:
        return cache.get_stats().to_dict()
    except Exception as exc:  # pragma: no cover
        return {
            "db_path": str(cache.db_path),
            "enabled": True,
            "size_mb": 0.0,
            "rows_per_table": {},
            "expired_rows": {},
            "hit_rate_24h": None,
            "hits_24h": 0,
            "misses_24h": 0,
            "error": type(exc).__name__,
        }


__all__ = ["get_cache_stats_impl", "get_server_info_impl", "health_check_impl"]
