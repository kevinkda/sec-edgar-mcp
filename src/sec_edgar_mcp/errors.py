"""Structured exception hierarchy for sec-edgar-mcp.

The SEC EDGAR API has no authentication, so the threat model is far simpler
than schwab-marketdata-mcp's: there are no Bearer tokens or refresh tokens
to leak. We still keep a structured exception hierarchy so MCP clients can
surface actionable messages, and we still strip ``User-Agent`` echoes from
exception text in case operators use a personal email there.

Coverage target: **100 %**.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Conservative redaction — strip the operator email from User-Agent echoes.
# Pattern: matches "<local>@<domain>" inside any string we render.
# ---------------------------------------------------------------------------

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
)
_REDACTED: Final[str] = "***REDACTED***"


def redact_email(text: str) -> str:
    """Replace any email address inside *text* with a redacted placeholder.

    Idempotent and side-effect-free. Used by every exception's ``__init__``
    so ``repr(exc)`` cannot leak the operator's contact email even if it
    came in via the SEC ``User-Agent`` header echo.
    """
    return _EMAIL_RE.sub(_REDACTED, text)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class SecError(Exception):
    """Base class for all sec-edgar-mcp errors.

    Subclasses MUST only accept allow-listed structured fields.  This base
    class deliberately keeps ``__str__`` short and does not capture extra
    args so a raw ``repr(exc)`` cannot accidentally leak operator data.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.__class__.__name__


class SecValidationError(SecError):
    """Input validation failure (raised before any HTTP call)."""

    def __init__(self, *, field: str, reason: str) -> None:
        if not isinstance(field, str):
            raise TypeError("field must be str")
        if not isinstance(reason, str):
            raise TypeError("reason must be str")
        self.field: str = field
        self.reason: str = redact_email(reason)
        super().__init__(f"validation failed: {field} — {self.reason}")

    def __str__(self) -> str:
        return f"SecValidationError(field={self.field}): {self.reason}"


class SecNotFoundError(SecError):
    """SEC returned 404 — the CIK / accession number does not exist."""

    def __init__(self, *, resource: str, hint: str) -> None:
        if not isinstance(resource, str):
            raise TypeError("resource must be str")
        if not isinstance(hint, str):
            raise TypeError("hint must be str")
        self.resource: str = resource
        self.hint: str = redact_email(hint)
        super().__init__(self.hint)

    def __str__(self) -> str:
        return f"SecNotFoundError(resource={self.resource}): {self.hint}"


class SecRateLimitError(SecError):
    """SEC returned 429 — fair-use throttle exceeded."""

    def __init__(self, *, retry_after_seconds: int, current_window_used: int) -> None:
        if not isinstance(retry_after_seconds, int):
            raise TypeError("retry_after_seconds must be int")
        if not isinstance(current_window_used, int):
            raise TypeError("current_window_used must be int")
        self.retry_after_seconds: int = retry_after_seconds
        self.current_window_used: int = current_window_used
        super().__init__(
            f"SEC rate limit exceeded; retry after {retry_after_seconds}s (used {current_window_used} in window)"
        )

    def __str__(self) -> str:
        return f"SecRateLimitError(retry_after={self.retry_after_seconds}s, window_used={self.current_window_used})"


class SecTransientError(SecError):
    """Retryable transient backend / network error (5xx, timeout, conn reset)."""

    def __init__(self, *, status_code: int, attempt: int, hint: str) -> None:
        if not isinstance(status_code, int):
            raise TypeError("status_code must be int")
        if not isinstance(attempt, int):
            raise TypeError("attempt must be int")
        if not isinstance(hint, str):
            raise TypeError("hint must be str")
        self.status_code: int = status_code
        self.attempt: int = attempt
        self.hint: str = redact_email(hint)
        super().__init__(self.hint)

    def __str__(self) -> str:
        return f"SecTransientError(status={self.status_code}, attempt={self.attempt}): {self.hint}"


class SecConfigurationError(SecError):
    """The operator has not set ``SEC_EDGAR_USER_AGENT`` to a valid value.

    SEC requires every request to carry a descriptive User-Agent.  Without
    one we refuse to issue requests rather than risk being IP-blocked.
    """

    def __init__(self, *, hint: str) -> None:
        if not isinstance(hint, str):
            raise TypeError("hint must be str")
        self.hint: str = redact_email(hint)
        super().__init__(self.hint)

    def __str__(self) -> str:
        return f"SecConfigurationError: {self.hint}"


__all__ = [
    "SecConfigurationError",
    "SecError",
    "SecNotFoundError",
    "SecRateLimitError",
    "SecTransientError",
    "SecValidationError",
    "redact_email",
]
