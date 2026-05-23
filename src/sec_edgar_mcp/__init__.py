"""SEC EDGAR Read-only MCP Server.

A Model Context Protocol (MCP) server exposing 6 tools that wrap the
SEC EDGAR public API (4 business + 2 meta tools).

Public modules:
    - :mod:`sec_edgar_mcp.server` — FastMCP entry point.
    - :mod:`sec_edgar_mcp.client` — async httpx client wrapper.
    - :mod:`sec_edgar_mcp.cache` — DuckDB local cache.
    - :mod:`sec_edgar_mcp.errors` — structured exception hierarchy.
    - :mod:`sec_edgar_mcp.models` — Pydantic v2 input schemas.
"""

from __future__ import annotations

__version__ = "0.1.1"

__all__ = ["__version__"]
