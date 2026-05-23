"""Console-script entry point.

``python -m sec_edgar_mcp`` and the ``sec-edgar-mcp`` script both land
here, which delegates to :func:`sec_edgar_mcp.server.main`.
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
