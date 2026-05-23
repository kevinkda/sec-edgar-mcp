# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-23

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
