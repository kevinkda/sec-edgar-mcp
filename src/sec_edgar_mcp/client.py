"""Async httpx wrapper for SEC EDGAR.

The SEC EDGAR `Fair Access Policy <https://www.sec.gov/os/accessing-edgar-data>`_
allows up to 10 requests per second per IP. We default to 8 req/s to leave
headroom for the bucket's burst smoothing and to be a polite citizen.

Required behaviour:

* Token-bucket rate limiter (sliding window) — tokens replenish smoothly,
  do **not** hold a slot across retry sleeps.
* User-Agent header MUST be set; otherwise SEC may block the IP.  We refuse
  to issue requests when ``SEC_EDGAR_USER_AGENT`` is missing.
* Three SEC hosts:
    - ``https://data.sec.gov/`` — JSON APIs (submissions, company facts).
    - ``https://www.sec.gov/`` — filing bodies, ticker map, browse-edgar.
    - ``https://efts.sec.gov/LATEST/search-index`` — full-text search.
* Errors are normalised:
    - 404 → :class:`SecNotFoundError`
    - 429 → :class:`SecRateLimitError`
    - 5xx / network → :class:`SecTransientError`
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Final

import httpx

from . import __version__
from .errors import (
    SecConfigurationError,
    SecNotFoundError,
    SecRateLimitError,
    SecTransientError,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------

#: SEC fair-use ceiling — never exceed regardless of operator override.
SEC_HARD_RATE_LIMIT_PER_SEC: Final[int] = 10

#: Default target rate (configurable via env). 8 req/s leaves headroom.
DEFAULT_RATE_LIMIT_PER_SEC: Final[int] = 8

DEFAULT_MAX_RETRIES_429: Final[int] = 2
DEFAULT_MAX_RETRIES_5XX: Final[int] = 3
DEFAULT_BACKOFF_BASE_SEC: Final[float] = 0.5
DEFAULT_REQUEST_TIMEOUT_SEC: Final[float] = 30.0

ENV_USER_AGENT: Final[str] = "SEC_EDGAR_USER_AGENT"
ENV_RATE_LIMIT: Final[str] = "SEC_EDGAR_RATE_LIMIT_PER_SEC"

# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

DATA_HOST: Final[str] = "https://data.sec.gov"
WWW_HOST: Final[str] = "https://www.sec.gov"
SEARCH_HOST: Final[str] = "https://efts.sec.gov"

#: Validate the operator-supplied User-Agent matches SEC's requested format.
#: SEC's docs say "Sample Company Name AdminContact@samplecompany.com" — we
#: enforce that the string contains an "@" and at least one space-separated
#: identifier so empty / placeholder values are rejected.
_USER_AGENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\S+.*\s.*@.+\..+",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def resolve_user_agent() -> str:
    """Return the configured User-Agent or raise :class:`SecConfigurationError`."""
    raw = os.environ.get(ENV_USER_AGENT, "").strip()
    if not raw:
        raise SecConfigurationError(
            hint=(
                f"{ENV_USER_AGENT} is not set.  SEC EDGAR requires every "
                f"request to carry a descriptive User-Agent in the form "
                f'"App Name (contact@email.example)".  Set it in your .env.'
            ),
        )
    if not _USER_AGENT_RE.match(raw):
        raise SecConfigurationError(
            hint=(
                f"{ENV_USER_AGENT} is set but does not match SEC's required "
                f'format "App Name (contact@email.example)".  Got: {raw!r}'
            ),
        )
    return raw


def resolve_rate_limit() -> int:
    """Return the active rate limit (≤ ``SEC_HARD_RATE_LIMIT_PER_SEC``)."""
    target = _env_int(ENV_RATE_LIMIT, DEFAULT_RATE_LIMIT_PER_SEC)
    target = max(target, 1)
    if target > SEC_HARD_RATE_LIMIT_PER_SEC:
        log.warning(
            '{"event":"rate_limit_clamped","requested":%d,"max":%d}',
            target,
            SEC_HARD_RATE_LIMIT_PER_SEC,
        )
        target = SEC_HARD_RATE_LIMIT_PER_SEC
    return target


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (sliding 1-second window).
# ---------------------------------------------------------------------------


class TokenBucket:
    """Sliding-1-second token bucket.

    We track outbound timestamps in a deque; before each request we evict
    timestamps older than 1 s and, if the deque is at capacity, sleep until
    the oldest one ages out.  This gives us a smooth ≤N req/s rate without
    holding the slot across retry sleeps (we record the timestamp **after**
    the request is admitted).
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity: int = capacity
        self._timestamps: deque[float] = deque()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                # Evict timestamps older than 1 second.
                while self._timestamps and (now - self._timestamps[0]) >= 1.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.capacity:
                    self._timestamps.append(now)
                    return
                wait = 1.0 - (now - self._timestamps[0])
                if wait <= 0:  # pragma: no cover - unreachable: eviction at >=1.0s guarantees wait>0
                    continue
                await asyncio.sleep(wait)

    def tokens_remaining(self) -> int:
        """Best-effort current-window headroom (no eviction; for stats only)."""
        now = time.monotonic()
        live = sum(1 for ts in self._timestamps if (now - ts) < 1.0)
        return max(0, self.capacity - live)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SecEdgarClient:
    """Async SEC EDGAR client with rate limiting and structured errors.

    One instance per process.  Construct via :func:`make_client` so the
    User-Agent / rate-limit / timeout knobs are pulled from env.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        rate_limit_per_sec: int,
        timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent: str = user_agent
        self.bucket: TokenBucket = TokenBucket(rate_limit_per_sec)
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=timeout_sec,
            headers={
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Host-Hint": "sec.gov",
            },
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SecEdgarClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        await self.aclose()

    # ------------------------------------------------------------ requests

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        """Issue a single HTTP request with retries, rate limit, and error mapping.

        Retry policy:
            * 429 → up to ``DEFAULT_MAX_RETRIES_429`` retries honouring
              ``Retry-After``.
            * 5xx / connection / timeout → up to ``DEFAULT_MAX_RETRIES_5XX``
              retries with exponential back-off + jitter.
            * 404 → raise :class:`SecNotFoundError` immediately.
            * Other 4xx → raise :class:`SecTransientError` (treated as
              non-retryable but surfaced via the same channel so the agent
              can react).
        """
        last_exc: Exception | None = None
        for attempt in range(DEFAULT_MAX_RETRIES_5XX + 1):
            await self.bucket.acquire()
            try:
                resp = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers={"Accept": accept},
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError) as exc:
                last_exc = exc
                if attempt >= DEFAULT_MAX_RETRIES_5XX:
                    raise SecTransientError(
                        status_code=0,
                        attempt=attempt,
                        hint=f"network error: {type(exc).__name__}",
                    ) from exc
                await asyncio.sleep(_backoff_delay(attempt))
                continue

            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                raise SecNotFoundError(
                    resource=url,
                    hint=f"SEC returned 404 for {url}",
                )
            if resp.status_code == 429:
                if attempt >= DEFAULT_MAX_RETRIES_429:
                    retry_after = _parse_retry_after(resp)
                    raise SecRateLimitError(
                        retry_after_seconds=retry_after,
                        current_window_used=self.bucket.capacity - self.bucket.tokens_remaining(),
                    )
                await asyncio.sleep(_parse_retry_after(resp))
                continue
            if 500 <= resp.status_code < 600:
                if attempt >= DEFAULT_MAX_RETRIES_5XX:
                    raise SecTransientError(
                        status_code=resp.status_code,
                        attempt=attempt,
                        hint=f"upstream {resp.status_code}",
                    )
                await asyncio.sleep(_backoff_delay(attempt))
                continue
            # Other 4xx — non-retryable, surface as transient with attempt=0.
            raise SecTransientError(
                status_code=resp.status_code,
                attempt=attempt,
                hint=f"unexpected {resp.status_code}",
            )

        # Should be unreachable — every loop branch either returns or raises.
        raise SecTransientError(  # pragma: no cover - defensive
            status_code=0,
            attempt=DEFAULT_MAX_RETRIES_5XX,
            hint=f"retry budget exhausted: {last_exc!r}",
        )

    # ---------------------------------------------------------------- API

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET *url* and parse as JSON."""
        resp = await self._request_with_retries("GET", url, params=params, accept="application/json")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SecTransientError(
                status_code=resp.status_code,
                attempt=0,
                hint=f"invalid json from {url}",
            ) from exc
        if not isinstance(data, (dict, list)):
            raise SecTransientError(
                status_code=resp.status_code,
                attempt=0,
                hint=f"unexpected json shape from {url}",
            )
        if isinstance(data, list):
            return {"items": data}
        return data

    async def get_text(self, url: str, *, max_bytes: int = 5 * 1024 * 1024) -> tuple[str, str, int, bool]:
        """GET *url* and return ``(text, content_type, byte_size, truncated)``.

        The 5 MiB cap keeps MCP frames bounded.  Larger filings get a
        ``truncated=True`` flag so callers can paginate via separate tools
        if/when we add them.
        """
        resp = await self._request_with_retries("GET", url, accept="text/html, text/plain")
        content = resp.content
        truncated = False
        if len(content) > max_bytes:
            content = content[:max_bytes]
            truncated = True
        ctype = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        try:
            text = content.decode("utf-8", errors="replace")
        except UnicodeDecodeError:  # pragma: no cover - errors=replace prevents this
            text = content.decode("latin-1", errors="replace")
        return text, ctype, len(resp.content), truncated


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(resp: httpx.Response) -> int:
    raw = resp.headers.get("Retry-After", "")
    try:
        v = int(raw)
        return max(0, min(v, 60))
    except ValueError:
        return 1


def _backoff_delay(attempt: int) -> float:
    """Exponential back-off with jitter."""
    base = DEFAULT_BACKOFF_BASE_SEC * (2**attempt)
    return float(base + random.random() * 0.25)


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> SecEdgarClient:
    """Build a configured :class:`SecEdgarClient` from env."""
    return SecEdgarClient(
        user_agent=resolve_user_agent(),
        rate_limit_per_sec=resolve_rate_limit(),
        transport=transport,
    )


def server_user_agent_default() -> str:
    """Build a stable default User-Agent that operators can copy into .env."""
    return f"sec-edgar-mcp/{__version__} (set-your-email@example.com)"


# ---------------------------------------------------------------------------
# Ticker → CIK resolver (uses SEC's published ticker.json file)
# ---------------------------------------------------------------------------


async def resolve_cik(client: SecEdgarClient, cik_or_ticker: str) -> str:
    """Return a 10-digit zero-padded CIK for *cik_or_ticker*.

    If the input is all digits, we zero-pad and return.  Otherwise we
    fetch ``https://www.sec.gov/files/company_tickers.json`` (cached by
    the caller via DuckDB) and look up the ticker.
    """
    raw = cik_or_ticker.strip().upper()
    if raw.isdigit():
        return raw.zfill(10)
    data = await client.get_json(f"{WWW_HOST}/files/company_tickers.json")
    # SEC ships a dict keyed by integers-as-strings; values are
    # {"cik_str": int, "ticker": str, "title": str}.
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("ticker", "")).upper() == raw:
            cik = entry.get("cik_str")
            if isinstance(cik, int):
                return str(cik).zfill(10)
            if isinstance(cik, str) and cik.isdigit():
                return cik.zfill(10)
    raise SecNotFoundError(
        resource=f"ticker:{raw}",
        hint=f"ticker {raw!r} not found in SEC ticker map",
    )


__all__ = [
    "DATA_HOST",
    "DEFAULT_MAX_RETRIES_5XX",
    "DEFAULT_MAX_RETRIES_429",
    "DEFAULT_RATE_LIMIT_PER_SEC",
    "DEFAULT_REQUEST_TIMEOUT_SEC",
    "ENV_RATE_LIMIT",
    "ENV_USER_AGENT",
    "SEARCH_HOST",
    "SEC_HARD_RATE_LIMIT_PER_SEC",
    "WWW_HOST",
    "Awaitable",
    "Callable",
    "SecEdgarClient",
    "TokenBucket",
    "make_client",
    "resolve_cik",
    "resolve_rate_limit",
    "resolve_user_agent",
    "server_user_agent_default",
]
