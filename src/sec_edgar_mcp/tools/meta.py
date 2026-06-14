"""Meta tools: ``health_check``, ``get_server_info``, ``get_cache_stats``.

These are local-only — they never touch SEC EDGAR so they remain available
even when ``SEC_EDGAR_USER_AGENT`` is unset.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import mcp

from .._ua_probe import probe_ua_reachability
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
        return {"enabled": False, "backend": None, "entries": 0}
    cache = get_cache()
    if cache is None:
        return {"enabled": False, "backend": None, "entries": 0}
    try:
        stats = cache.get_stats()
    except Exception:
        return {"enabled": True, "backend": None, "entries": 0}
    return {
        "enabled": stats.enabled,
        "backend": stats.backend,
        "entries": stats.entries,
    }


async def health_check_impl() -> dict[str, Any]:
    """Local health probe with optional server-side UA reachability check.

    The local ``user_agent_configured`` flag only checks env-var format.
    ``sec_ua_reachable`` (R7) sends a single cached HEAD request to a
    cheap EDGAR endpoint and reports whether SEC's edge actually accepts
    the configured UA — the missing layer that lets a malformed
    ``noreply`` UA pass local format validation but later trip a 403
    fair-access rejection mid-playbook.

    ``overall_status`` aggregates these:

        * ``unhealthy`` — UA is unconfigured (server cannot call SEC).
        * ``degraded`` — UA is configured but SEC explicitly rejects it.
        * ``ok`` — everything else, including transient probe failures
          (TIMEOUT / NETWORK_ERROR), which we deliberately do **not**
          downgrade — a flaky probe is not a server-health problem.
    """
    ua = _safe_user_agent_status()
    cache_summary = _safe_cache_summary()

    raw_ua = os.environ.get(ENV_USER_AGENT, "").strip()
    probe = probe_ua_reachability(raw_ua)

    if probe.status == "UNCONFIGURED":
        overall_status = "unhealthy"
    elif probe.status == "REJECTED_HTML_403":
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return {
        "server_version": _SERVER_VERSION,
        "user_agent_configured": ua["configured"],
        "user_agent_reason": ua["reason"],
        "rate_limit_per_sec": resolve_rate_limit() if ua["configured"] else None,
        "rate_limit_hard_cap": SEC_HARD_RATE_LIMIT_PER_SEC,
        "cache_enabled": cache_summary["enabled"],
        "cache_backend": cache_summary["backend"],
        "cache_entries": cache_summary["entries"],
        "platform_supported": True,
        "sec_ua_reachable": probe.to_dict(),
        "overall_status": overall_status,
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
    """Local cache backend health — never calls SEC."""
    cache = get_cache()
    if cache is None:
        return {
            "backend": None,
            "enabled": False,
            "entries": 0,
        }
    try:
        return cache.get_stats().to_dict()
    except Exception as exc:  # pragma: no cover
        return {
            "backend": cache.backend.name,
            "enabled": True,
            "entries": 0,
            "error": type(exc).__name__,
        }


__all__ = ["get_cache_stats_impl", "get_server_info_impl", "health_check_impl"]
