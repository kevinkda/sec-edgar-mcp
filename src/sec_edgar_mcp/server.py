"""FastMCP server entry point — 6 outward-facing tools.

The first thing this module does is harden stdio so no stray ``print`` /
log line pollutes the JSON-RPC stream:

* monkey-patch ``builtins.print`` so the default ``file`` is ``sys.stderr``;
* install a :class:`RotatingFileHandler` writing to
  ``${XDG_STATE_HOME}/sec-edgar-mcp/logs/server.log``;
* force ``httpx`` / ``httpcore`` to ``WARNING``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 0) Stdio hardening — must run BEFORE we import anything that might log /
#    print at import time (httpx, etc).
# ---------------------------------------------------------------------------
import builtins
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def _harden_stdio() -> None:
    """Install the print + logging mitigations."""
    # 1) builtins.print → stderr by default.
    _orig_print = builtins.print

    def _safe_print(*args: Any, file: Any = None, **kwargs: Any) -> None:
        _orig_print(*args, file=file or sys.stderr, **kwargs)

    builtins.print = _safe_print

    # 2) Logging - RotatingFileHandler + StreamHandler(stderr).
    from . import _platform

    log_dir: Path | None = _platform.state_root() / "sec-edgar-mcp" / "logs"
    try:
        assert log_dir is not None
        with _platform.restrictive_umask():
            log_dir.mkdir(parents=True, exist_ok=True)
        if not _platform.IS_WINDOWS:
            _platform.secure_chmod(log_dir, 0o700)
    except OSError:
        log_dir = None

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir is not None:
        try:
            file_handler = RotatingFileHandler(
                log_dir / "server.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                logging.Formatter('{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}')
            )
            handlers.append(file_handler)
        except OSError:
            pass

    level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        handlers=handlers,
        level=getattr(logging, level, logging.WARNING),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
        force=True,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_harden_stdio()


# ---------------------------------------------------------------------------
# 0b) Load .env from the current working directory.  Host-injected env vars
#     win because ``override=False``.
# ---------------------------------------------------------------------------
def _bootstrap_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover
        pass


_bootstrap_dotenv()


# ---------------------------------------------------------------------------
# Imports after hardening
# ---------------------------------------------------------------------------

from typing import Final  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

from . import __version__ as SERVER_VERSION  # noqa: E402
from .errors import (  # noqa: E402
    SecConfigurationError,
    SecError,
    SecNotFoundError,
    SecRateLimitError,
    SecTransientError,
    SecValidationError,
)
from .models import (  # noqa: E402
    Get8KWithItemsInput,
    GetCompanyFilingsInput,
    GetFilingTextInput,
    GetForm4InsiderTradesInput,
    SearchFilingsFullTextInput,
)
from .tools import filings, insider, meta, search  # noqa: E402

log = logging.getLogger("sec_edgar_mcp.server")

SERVER_NAME: Final[str] = "sec-edgar-mcp"


# ---------------------------------------------------------------------------
# Error framing — convert structured exceptions to JSON-friendly dicts so
# the MCP client surfaces actionable messages instead of stack traces.
# ---------------------------------------------------------------------------


def _frame_error(exc: BaseException) -> dict[str, Any]:
    """Convert any exception into a structured error envelope."""
    if isinstance(exc, SecValidationError):
        return {"error": "validation", "field": exc.field, "reason": exc.reason}
    if isinstance(exc, SecConfigurationError):
        return {"error": "configuration", "hint": exc.hint}
    if isinstance(exc, SecNotFoundError):
        return {"error": "not_found", "resource": exc.resource, "hint": exc.hint}
    if isinstance(exc, SecRateLimitError):
        return {
            "error": "rate_limit",
            "retry_after_seconds": exc.retry_after_seconds,
            "current_window_used": exc.current_window_used,
        }
    if isinstance(exc, SecTransientError):
        return {
            "error": "transient",
            "status_code": exc.status_code,
            "attempt": exc.attempt,
            "hint": exc.hint,
        }
    if isinstance(exc, SecError):
        return {"error": "sec_error", "type": type(exc).__name__}
    return {"error": "internal", "type": type(exc).__name__}


def _safe_run(name: str, coro: Any) -> dict[str, Any]:
    """Synchronous wrapper that converts SecError into a JSON envelope."""
    raise NotImplementedError("internal helper — async tools call directly")


# ---------------------------------------------------------------------------
# FastMCP wiring
# ---------------------------------------------------------------------------


def _build_mcp() -> FastMCP:
    mcp_app = FastMCP(SERVER_NAME)

    # FastMCP ctor (mcp SDK 1.27.x) does not expose a ``version=`` kwarg, so the
    # underlying lowlevel ``Server.version`` defaults to ``None`` and the
    # ``initialize`` response falls back to
    # ``importlib.metadata.version("mcp")`` (framework version, e.g. 1.27.1).
    # Inject the project release tag directly on the lowlevel server so
    # ``serverInfo.version`` reflects this package's ``__version__``.
    mcp_app._mcp_server.version = SERVER_VERSION

    @mcp_app.tool()
    async def get_company_filings(
        cik_or_ticker: str,
        form_types: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return up to ``limit`` recent SEC filings for a company.

        ``cik_or_ticker`` may be a 1-10 digit CIK or a US ticker symbol.
        ``form_types`` filters to a subset like ``["10-K", "10-Q", "8-K"]``.
        """
        try:
            args = GetCompanyFilingsInput(
                cik_or_ticker=cik_or_ticker,
                form_types=form_types,
                limit=limit,
            )
            return await filings.get_company_filings_impl(args)
        except SecError as exc:
            return _frame_error(exc)

    @mcp_app.tool()
    async def get_form4_insider_trades(
        cik_or_ticker: str,
        since_days: int = 30,
    ) -> dict[str, Any]:
        """Return Form 4 (insider transaction) filings in the last *since_days*."""
        try:
            args = GetForm4InsiderTradesInput(
                cik_or_ticker=cik_or_ticker,
                since_days=since_days,
            )
            return await insider.get_form4_insider_trades_impl(args)
        except SecError as exc:
            return _frame_error(exc)

    @mcp_app.tool()
    async def get_filing_text(
        accession_number: str,
        document_type: str = "primary",
    ) -> dict[str, Any]:
        """Return the body of a single SEC filing by accession number."""
        try:
            args = GetFilingTextInput(
                accession_number=accession_number,
                document_type=document_type,  # type: ignore[arg-type]
            )
            return await filings.get_filing_text_impl(args)
        except SecError as exc:
            return _frame_error(exc)

    @mcp_app.tool()
    async def search_filings_full_text(
        query: str,
        form_types: list[str] | None = None,
        since_days: int = 90,
    ) -> dict[str, Any]:
        """Run an EDGAR full-text search across all filers."""
        try:
            args = SearchFilingsFullTextInput(
                query=query,
                form_types=form_types,
                since_days=since_days,
            )
            return await search.search_filings_full_text_impl(args)
        except SecError as exc:
            return _frame_error(exc)

    @mcp_app.tool()
    async def get_8k_with_items(
        cik_or_ticker: str,
        item_codes: list[str] | None = None,
        since_days: int = 30,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return 8-K filings filtered by SEC item codes (e.g. '1.01', '5.02')."""
        try:
            args = Get8KWithItemsInput(
                cik_or_ticker=cik_or_ticker,
                item_codes=item_codes,
                since_days=since_days,
                limit=limit,
            )
            return await filings.get_8k_with_items_impl(args)
        except SecError as exc:
            return _frame_error(exc)

    @mcp_app.tool()
    async def health_check() -> dict[str, Any]:
        """Local health probe.  Never calls SEC."""
        return await meta.health_check_impl()

    @mcp_app.tool()
    async def get_server_info() -> dict[str, Any]:
        """Local server metadata.  Never calls SEC."""
        return await meta.get_server_info_impl(server_version=SERVER_VERSION)

    return mcp_app


# Lazy build so test collection (which imports server) doesn't fail when
# stdio is already connected to pytest's capture.
_app: FastMCP | None = None


def app() -> FastMCP:
    global _app
    if _app is None:
        _app = _build_mcp()
    return _app


def main() -> None:
    """Console-script entry point."""
    log.info('{"event":"server_start","version":"%s"}', SERVER_VERSION)
    app().run()


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "app",
    "main",
]
