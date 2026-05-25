"""SEC EDGAR User-Agent reachability probe.

Validates that the configured ``SEC_EDGAR_USER_AGENT`` is actually accepted
by SEC's Fair-Access policy by issuing a minimal HEAD request against a
known-valid EDGAR endpoint and inspecting the response.

The local ``user_agent_configured`` flag in ``health_check`` only reflects
whether the env var matches SEC's required textual format.  It cannot
detect cases where SEC's edge has IP- or pattern-banned the UA (e.g. the
``users.noreply.github.com`` placeholder seen in the wild) — the request
still goes through formatting validation but SEC returns a 403 HTML page
saying "your client appears to be an undeclared automated tool".

This module fills that gap with a server-side probe.  Design constraints:

* **Zero load on SEC.**  HEAD against the cheapest, most cache-friendly
  EDGAR endpoint (``browse-edgar`` with ``count=1`` against AAPL CIK
  0000320193).  Total bytes on the wire ≈ 0 (HEAD has no body).
* **Cached.**  Module-level dict keyed by a SHA-256 of the UA string,
  default 5-minute TTL — health_check is called frequently and we do
  not want the probe to become a hidden quota consumer.
* **Bounded.**  5-second hard timeout.  Health-check callers cannot be
  blocked by a slow SEC handshake.
* **Synchronous.**  Returned via a regular ``def`` so the existing async
  ``health_check_impl`` can call it without restructuring; we run a
  short blocking httpx call inside the async coroutine.  At 5 s worst
  case this is acceptable for an admin probe.
* **Never raises.**  All failure modes are mapped to one of the
  structured ``UaProbeStatus`` values so the caller gets a deterministic
  payload.

The probe deliberately does *not* write to the DuckDB cache so a cold
``health_check`` cannot accidentally pre-warm SEC fixtures or interact
with the user's filings cache.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public type surface
# ---------------------------------------------------------------------------

UaProbeStatus = Literal[
    "ACCEPTED",
    "REJECTED_HTML_403",
    "TIMEOUT",
    "NETWORK_ERROR",
    "UNCONFIGURED",
]


@dataclass(frozen=True)
class UaProbeResult:
    """Structured result of one UA reachability probe."""

    status: UaProbeStatus
    detail: str
    last_checked_at: datetime
    cache_ttl_remaining_s: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "last_checked_at": self.last_checked_at.isoformat(),
            "cache_ttl_remaining_s": self.cache_ttl_remaining_s,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: SEC endpoint used for the probe.  AAPL CIK is hard-coded because it is
#: the longest-standing public issuer on EDGAR; ``count=1`` minimises the
#: rendered page size.  HEAD on this URL is served quickly and never
#: returns >0 bytes of body to us.
PROBE_URL: Final[str] = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK=0000320193&type=4&dateb=&owner=include&count=1"
)

DEFAULT_CACHE_TTL_SECONDS: Final[int] = 300  # 5 minutes
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

#: SEC's fair-use rejection page contains this signature string.  Matching
#: case-insensitively keeps the check resilient to minor wording changes.
_REJECTION_SIGNATURES: Final[tuple[str, ...]] = (
    "undeclared automated tool",
    "request rate threshold",
)

#: Substrings that signal an obviously bogus / placeholder UA we should
#: never probe SEC with — they will be deny-listed anyway and we owe SEC
#: zero rejected requests.
_BOGUS_UA_SUBSTRINGS: Final[tuple[str, ...]] = (
    "noreply",
    "no-reply",
    "example.com",
    "set-your-email",
    "youremail",
    "your-email",
)

#: SEC requires UAs to look like ``"App Name (contact@email.example)"``;
#: we treat anything missing an "@" + a TLD as unconfigured.
_VALID_UA_RE: Final[re.Pattern[str]] = re.compile(r"^\S+.*\s.*@.+\..+")

# ---------------------------------------------------------------------------
# Cache (module-level, thread-safe)
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """Internal cache slot."""

    result: UaProbeResult
    expires_at_monotonic: float


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK: threading.Lock = threading.Lock()


def _ua_fingerprint(user_agent: str) -> str:
    """Stable per-UA cache key.  We hash so we never log raw UA bodies."""
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()


def reset_cache() -> None:
    """Drop all cached probe results.  Used by tests and on reconfig."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Type alias for the HEAD-request callable injected in tests.  Defaults
# to httpx.Client.head wrapped in a closure below.
HeadCallable = Callable[[str, float, str], httpx.Response]


def _default_head(url: str, timeout: float, user_agent: str) -> httpx.Response:
    """Real HEAD callable — used in production paths."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html",
        "Accept-Encoding": "gzip, deflate",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.head(url, headers=headers)


def probe_ua_reachability(
    user_agent: str,
    *,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    now_func: Callable[[], datetime] | None = None,
    monotonic_func: Callable[[], float] | None = None,
    head_func: HeadCallable | None = None,
) -> UaProbeResult:
    """Probe SEC EDGAR with *user_agent* and return a cached structured result.

    Parameters
    ----------
    user_agent:
        Raw value of ``SEC_EDGAR_USER_AGENT``.  Empty / placeholder values
        short-circuit to ``UNCONFIGURED`` without issuing any request.
    cache_ttl_seconds:
        How long an ``ACCEPTED`` / ``REJECTED_HTML_403`` result stays in
        cache.  Transient failures (TIMEOUT / NETWORK_ERROR) honour the
        same TTL — re-probing on every health_check call would compound
        the network problem.
    timeout_seconds:
        Hard ceiling on the HEAD request.
    now_func, monotonic_func, head_func:
        Test seams.  Production callers pass ``None``.
    """
    # Test seams default to real implementations.
    now = now_func or (lambda: datetime.now(UTC))
    mono = monotonic_func or _default_monotonic
    do_head = head_func or _default_head

    # Step 1 — short-circuit obviously unconfigured / placeholder UAs.
    if _is_unconfigured(user_agent):
        return _make_result(
            status="UNCONFIGURED",
            detail=(
                "SEC_EDGAR_USER_AGENT is missing, malformed, or contains a "
                "known-bogus placeholder (e.g. noreply.github.com); SEC "
                "will deny-list it on first contact, refusing to probe."
            ),
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
            cache_key_override="__UNCONFIGURED__",
        )

    cache_key = _ua_fingerprint(user_agent)

    # Step 2 — cache hit?
    cached = _read_cache(cache_key, mono())
    if cached is not None:
        return cached

    # Step 3 — perform HEAD probe.
    try:
        resp = do_head(PROBE_URL, timeout_seconds, user_agent)
    except httpx.TimeoutException as exc:
        return _make_result(
            status="TIMEOUT",
            detail=f"HEAD {PROBE_URL} timed out after {timeout_seconds:.1f}s: {type(exc).__name__}",
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
        )
    except httpx.HTTPError as exc:
        return _make_result(
            status="NETWORK_ERROR",
            detail=f"HEAD {PROBE_URL} failed: {type(exc).__name__}: {exc}",
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
        )
    except Exception as exc:  # pragma: no cover - defensive belt
        return _make_result(
            status="NETWORK_ERROR",
            detail=f"HEAD {PROBE_URL} raised unexpected {type(exc).__name__}: {exc}",
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
        )

    return _classify_response(
        resp,
        now=now,
        mono=mono,
        cache_ttl_seconds=cache_ttl_seconds,
        user_agent=user_agent,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_monotonic() -> float:
    import time

    return time.monotonic()


def _is_unconfigured(user_agent: str) -> bool:
    """Return ``True`` for empty / placeholder / format-invalid UAs."""
    raw = (user_agent or "").strip()
    if not raw:
        return True
    if not _VALID_UA_RE.match(raw):
        return True
    lower = raw.lower()
    return any(needle in lower for needle in _BOGUS_UA_SUBSTRINGS)


def _classify_response(
    resp: httpx.Response,
    *,
    now: Callable[[], datetime],
    mono: Callable[[], float],
    cache_ttl_seconds: int,
    user_agent: str,
) -> UaProbeResult:
    """Map an HTTP response to one of the probe statuses."""
    status_code = resp.status_code
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()

    if status_code == 200:
        return _make_result(
            status="ACCEPTED",
            detail=f"SEC accepted UA (HTTP 200, content-type={ctype or 'unknown'})",
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
        )

    if status_code == 403:
        body_text = _peek_body_text(resp)
        signature = _matched_rejection_signature(body_text)
        if signature is not None:
            return _make_result(
                status="REJECTED_HTML_403",
                detail=(
                    f"SEC fair-access policy rejected UA "
                    f"(HTTP 403, signature={signature!r}); update "
                    f"SEC_EDGAR_USER_AGENT to a real reachable email "
                    f"and reload."
                ),
                now=now,
                mono=mono,
                cache_ttl_seconds=cache_ttl_seconds,
                user_agent=user_agent,
            )
        # 403 without a recognisable rejection signature is treated as a
        # generic NETWORK_ERROR — could be a transient WAF blip rather
        # than a UA ban.
        return _make_result(
            status="NETWORK_ERROR",
            detail=(
                f"HTTP 403 without recognised SEC rejection signature; "
                f"treating as transient (content-type={ctype or 'unknown'})"
            ),
            now=now,
            mono=mono,
            cache_ttl_seconds=cache_ttl_seconds,
            user_agent=user_agent,
        )

    return _make_result(
        status="NETWORK_ERROR",
        detail=f"unexpected HTTP {status_code} from SEC probe endpoint",
        now=now,
        mono=mono,
        cache_ttl_seconds=cache_ttl_seconds,
        user_agent=user_agent,
    )


def _peek_body_text(resp: httpx.Response) -> str:
    """Return up to 4 KiB of body text without raising on missing body.

    HEAD responses normally have an empty body, but some servers attach
    a few bytes of explanation.  We cap at 4 KiB to keep the cost
    bounded.
    """
    try:
        raw = resp.content[:4096]
    except (httpx.ResponseNotRead, RuntimeError):
        return ""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _matched_rejection_signature(body_text: str) -> str | None:
    if not body_text:
        return None
    lower = body_text.lower()
    for sig in _REJECTION_SIGNATURES:
        if sig in lower:
            return sig
    return None


def _make_result(
    *,
    status: UaProbeStatus,
    detail: str,
    now: Callable[[], datetime],
    mono: Callable[[], float],
    cache_ttl_seconds: int,
    user_agent: str,
    cache_key_override: str | None = None,
) -> UaProbeResult:
    checked_at = now()
    monotonic_now = mono()
    expires_at = monotonic_now + max(0, cache_ttl_seconds)
    result = UaProbeResult(
        status=status,
        detail=detail,
        last_checked_at=checked_at,
        cache_ttl_remaining_s=max(0, cache_ttl_seconds),
    )
    cache_key = cache_key_override or _ua_fingerprint(user_agent)
    _write_cache(cache_key, result, expires_at)
    return result


def _read_cache(cache_key: str, monotonic_now: float) -> UaProbeResult | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry is None:
            return None
        remaining = entry.expires_at_monotonic - monotonic_now
        if remaining <= 0:
            del _CACHE[cache_key]
            return None
        # Re-emit the cached result with refreshed remaining-TTL so callers
        # always see an accurate countdown.
        return UaProbeResult(
            status=entry.result.status,
            detail=entry.result.detail,
            last_checked_at=entry.result.last_checked_at,
            cache_ttl_remaining_s=int(remaining),
        )


def _write_cache(cache_key: str, result: UaProbeResult, expires_at_monotonic: float) -> None:
    with _CACHE_LOCK:
        _CACHE[cache_key] = _CacheEntry(result=result, expires_at_monotonic=expires_at_monotonic)


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROBE_URL",
    "UaProbeResult",
    "UaProbeStatus",
    "probe_ua_reachability",
    "reset_cache",
]
