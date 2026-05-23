# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Form 4 XBRL parser** (`_xbrl.py`): structured parsing of insider
  trade reports using `defusedxml.ElementTree` for XXE / billion-laughs
  / external-entity protection. Returns `Form4Data` with all
  transactions (date, code, shares, price, ownership form), insider
  role flags (officer / director / 10 %-owner), net buy/sell values,
  and `raw_warnings` for tolerated field-level issues. Hard 8 MiB
  input ceiling, 5 000-transaction cap per filing, and a 30-digit
  exponent ceiling guard against pathological inputs.
- **`get_form4_insider_trades` upgraded**: now fetches each filing's
  XML body and returns structured `transactions[]` (date, code,
  shares, price, ownership form) plus `summary` (net_buy_value,
  net_sell_value, transaction_count, parse_failures) instead of
  metadata-only. Per-filing fetch / parse failures are surfaced as
  `parse_error` on the row instead of crashing the whole call. Cache
  schema is unchanged — a hit avoids both the submissions JSON and
  every Form 4 body fetch.
- **`get_8k_with_items` (7th tool)**: filter 8-K filings by item codes
  (1.01 entry into material agreement, 2.02 results of operations,
  5.02 director/officer changes, 7.01 Reg FD, 9.01 financial
  statements, etc.). Reuses the cached submissions JSON; new Pydantic
  `Get8KWithItemsInput` enforces an `X.YY` `ITEM_CODE_RE` regex.
- **Hypothesis fuzz tests** for the parser: 12 hand-crafted seeds
  (XXE, billion-laughs, oversize, exotic Unicode, wrong root, …) and
  3 property-based generators (well-formed random fields, random
  binary, random Unicode text). Surfaced one real Decimal-overflow
  bug that is now mitigated.
- 76 new tests (XBRL parser + fuzz + 8-K tool + helper unit tests).
- `defusedxml` runtime dependency and `hypothesis` dev dependency.

### Changed

- `docs/THREAT_MODEL.md`: removes "Form 4 XML parsing" from the
  out-of-scope list and documents the new XML attack-surface
  mitigation strategy (defusedxml + size / depth / exponent caps +
  `Form4ParseError` instead of unhandled exceptions).
- Test count: 169 → 245 on Linux; total coverage 86.06 % → 91.08 %;
  `_xbrl.py` 97 % line+branch coverage.
- `serverInfo.supportedTools` now reports 7 tools (4 business + 3
  meta) — `get_8k_with_items` joins the surface.

### Security

- All Form 4 XML parsing is delegated to `defusedxml.ElementTree`;
  external entity references, DTDs, and exponential entity expansion
  are refused before any business logic runs.
- The new `Form4ParseError` exception inherits from `SecError` so
  parse failures flow through the existing email-redacting framing
  pipeline.

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
