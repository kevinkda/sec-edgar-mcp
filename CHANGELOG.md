# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-05-24

### Fixed

- **`serverInfo.version` now reports the project release tag** (`0.1.1`)
  instead of the underlying mcp framework version (`1.27.x`). The mcp
  Python SDK 1.27.x `FastMCP.__init__` does not accept a `version=` kwarg,
  so the lowlevel `Server.version` defaulted to `None` and the
  `initialize` response fell back to `importlib.metadata.version("mcp")`.
  Fix: directly set `mcp_app._mcp_server.version = SERVER_VERSION` after
  FastMCP construction. Adds `test_initialize_reports_release_tag_version`
  integration test asserting the fix.

### Compatibility

- Test count: 143 → 144 passing on Linux (85.25% coverage).

## [0.1.0] - 2026-05-23

Initial scaffold.

### Added

- 4 read-only MCP tools wrapping the SEC EDGAR public API:
  - `get_company_filings(cik_or_ticker, form_types?, limit=20)`
  - `get_form4_insider_trades(cik_or_ticker, since_days=30)`
  - `get_filing_text(accession_number, document_type="primary")`
  - `search_filings_full_text(query, form_types?, since_days=90)`
- 2 meta tools: `health_check` and `get_server_info` (offline-safe).
- DuckDB local cache with per-table TTL (24 h / 6 h / 30 d / 24 h).
- Token-bucket rate limiter (sliding 1-second window, default 8 req/s,
  hard cap 10 req/s — SEC fair-use ceiling).
- Structured error hierarchy: `SecValidationError`, `SecNotFoundError`,
  `SecRateLimitError`, `SecTransientError`, `SecConfigurationError`.
- Pydantic v2 input schemas with anchored CIK / ticker / accession-number
  regexes; form-type allowlist of 40+ common SEC form codes.
- Stdio-hardened FastMCP server with rotating file logs under
  `${XDG_STATE_HOME}/sec-edgar-mcp/logs/`.
- 143 unit + integration tests (85.23 % line coverage).
- Documentation: README (en + zh), `docs/REGISTER.md`,
  `docs/THREAT_MODEL.md`, `docs/RELEASE.md`, `CONTRIBUTING.md`.

[Unreleased]: https://github.com/kevinkda/sec-edgar-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.1.0
