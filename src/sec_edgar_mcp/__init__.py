"""SEC EDGAR Read-only MCP Server.

A Model Context Protocol (MCP) server exposing 10 tools that wrap the
SEC EDGAR public API (8 business + 2 meta tools).

Public modules:
    - :mod:`sec_edgar_mcp.server` — FastMCP entry point.
    - :mod:`sec_edgar_mcp.client` — async httpx client wrapper.
    - :mod:`sec_edgar_mcp.cache` — pluggable response cache.
    - :mod:`sec_edgar_mcp.errors` — structured exception hierarchy.
    - :mod:`sec_edgar_mcp.models` — Pydantic v2 input schemas.
    - :mod:`sec_edgar_mcp._xbrl` — defused Form 4 XML parser.
    - :mod:`sec_edgar_mcp._thirteenf` — defused 13F information-table parser.
    - :mod:`sec_edgar_mcp._proxy` — bounded DEF 14A key-fact extractor.
"""

from __future__ import annotations

__version__ = "0.4.0"

__all__ = ["__version__"]
