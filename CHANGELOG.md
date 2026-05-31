# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.3] - 2026-05-31

### Added

- **Test campaign batch 3 — 100% coverage + full security suite.**
  Raised line+branch coverage from 92.13% to **100.00%** (637 tests, up
  from 446) and added a complete security test matrix mirroring the
  batch-1 schwab-positions-mcp template:
  - `tests/test_coverage_completion.py` — drives every residual
    `file:line` branch to 100% (server error-framing, stdio-harden
    OSError paths, `_runtime` cache lookup/store exceptions, search
    normalisation edge cases, cache DuckDB-error resilience, `_xbrl`
    defensive skips, client JSON-shape + ticker-map branches).
  - `tests/test_owasp_2017.py`, `tests/test_owasp_2021.py`,
    `tests/test_owasp_2025.py` — OWASP Top 10 across all three editions,
    each applicable category asserting a concrete invariant. **N/A
    categories are explicitly documented with source-drift guards**:
    A2/A7 Broken Authentication (SEC EDGAR is unauthenticated — no
    Bearer/refresh tokens) and A7:2017 XSS (no HTML generated/served).
  - `tests/test_pentest.py` — active attacker simulation: SSRF
    redirection, SQL/command injection, XXE file-read + SSRF + parameter
    entities + XML bombs, resource exhaustion, and information-leak
    guards.
  - `tests/test_exception.py` — exception-path type guards, HTTP-layer
    error handling, cache best-effort resilience, and email/PII-leak
    scrubbing.
  - `tests/test_boundary.py` — boundary-value sweeps for every numeric
    and string input (limit, since_days, CIK/ticker length, accession
    format, search query, item codes, extra-field rejection).
- Confirmed XXE protection: `defusedxml` refuses external DTD entities,
  parameter entities, and entity-expansion bombs — sec-edgar-mcp is the
  only batch-3 repo with a genuine XXE-applicable surface (the Form 4
  XBRL parser).

### Changed

- CI coverage gate (`tool.coverage.report.fail_under`) raised from 85 to
  **100**.
- `markdownlint-cli2` pre-commit hook gated to `stages: [manual]` (aligns
  with gitleaks) because the `npx --yes` invocation times out on
  locked-down corporate networks; CI still runs markdownlint on the
  public-network reusable workflow.
- Two `# pragma: no cover` / `# pragma: no branch` annotations added to
  provably-unreachable defensive branches (token-bucket `wait<=0`
  continue, double-checked-lock race sides, POSIX-only chmod on the
  Windows path) with documented rationale, plus matching source-drift
  guards in the test suite.

## [0.2.2] - 2026-05-29

### Fixed

- **R8 — Form 4 XBRL parser real-corpus remediation** (closes
  v0.4.1 hotfix; PB-3 incident 2026-05-25). The
  `get_form4_insider_trades` tool was fetching SEC EDGAR's
  XSLT-rendered HTML view (`xsl<style>/<doc>.xml`) instead of the raw
  ownership XML, causing every Form 4 in PB-3's 70-entry watchlist
  to fail with a single uniform `malformed XML: mismatched tag:
  line 29, column 16` error. Two-layer fix:
    1. `tools/insider.py:_strip_xslt_prefix` strips the
       `xsl<style>/` segment from `submissions.recent.primaryDocument`
       so the URL points at the raw `<doc>.xml` ownership document.
    2. `_xbrl.py:_looks_like_html_rendering` defensively detects
       `<!DOCTYPE html>` / `<html>` heads (case-insensitive, BOM- and
       prolog-tolerant) and raises a structured
       `Form4ParseError(reason="received SEC XSLT-rendered HTML, …")`
       so any future regression surfaces with a self-diagnosing
       message instead of a generic XML parse error.
- New `tests/fixtures/form4_real_corpus/` directory with **80 real
  SEC EDGAR Form 4 ownership XML bodies** (raw, not XSLT-rendered)
  captured 2026-05-29 across 20 issuers spanning tech / financial /
  staples / energy / healthcare / ADR / dual-class. These fixtures
  exercise 9 distinct Section 16 transaction codes (S, M, F, A, G,
  P, C, H, D) over 242 transactions and form the parser's
  contract-test corpus.
- New `tests/test_xbrl_real_corpus.py` invariants:
  - directory holds ≥ 50 samples;
  - every entry parses without exception;
  - aggregate parse rate ≥ 95 %;
  - corpus exercises ≥ 5 distinct transaction codes.
- `tests/test_xbrl_fuzz.py` upgraded with corpus-seeded
  `@example` decorators on the random-bytes hypothesis fuzzer plus a
  parametrised real-corpus seed test, so any refactor that drops
  support for the canonical SEC XML shape fails the fuzz suite,
  not just the corpus invariant.

### Added

- `scripts/fetch_form4_corpus.py` — one-shot bootstrap fetcher
  (not imported by production) that pulls a diverse Form 4 corpus
  from SEC EDGAR using the configured `SEC_EDGAR_USER_AGENT` and
  the same fair-use rate budget as the production client. Documented
  in `tests/fixtures/form4_real_corpus/README.md`.

### Compatibility

- Test count: 273 → 446 passing on Linux (+173 tests including 80
  parametrised corpus + 80 fuzz-seeded entries + helper coverage).
- Total coverage: 91.08 % → 92.13 %.
- `_xbrl.py` 97 % → 98 % branch + line; `tools/insider.py` 92 % → 97 %.
- `src/sec_edgar_mcp/__init__.py:__version__` updated to `0.2.2`
  (also corrects a stale `0.2.0` value carried forward from v0.2.0).

### Security

- `_looks_like_html_rendering` operates on the leading 1 KiB only,
  before any `defusedxml` parsing, so an oversized HTML payload is
  rejected without exposing the secure parser to it. No relaxation
  of the existing XXE / billion-laughs / DTD posture; defusedxml
  remains the only XML parsing path.

## [0.2.1] - 2026-05-25

### Added

- **R7 server-side UA reachability probe** (`_ua_probe.py`): the
  `health_check` tool now exposes a new `sec_ua_reachable` field that
  issues a single cached `HEAD` request to a cheap EDGAR endpoint
  (`browse-edgar?CIK=0000320193&type=4&count=1`) and reports whether
  SEC's edge actually accepts the configured `SEC_EDGAR_USER_AGENT`.
  This catches the case where the env var matches SEC's textual format
  (e.g. `noreply.github.com` placeholders) but SEC has IP- or
  pattern-banned the UA, which previously passed local
  `user_agent_configured: true` validation only to trip a 403 mid-call.
  The probe returns one of `ACCEPTED` / `REJECTED_HTML_403` /
  `TIMEOUT` / `NETWORK_ERROR` / `UNCONFIGURED`, with results cached
  for 5 minutes (sha256-keyed) to avoid hidden rate-limit consumption.
  100 % line + branch coverage on `_ua_probe.py`.

### Changed

- `health_check` now returns an `overall_status` field aggregating the
  probe result:
  - `unhealthy` when `sec_ua_reachable.status == UNCONFIGURED`
      (server cannot call SEC at all).
  - `degraded` when `sec_ua_reachable.status == REJECTED_HTML_403`
      (UA explicitly banned — user must fix `.env`).
  - `ok` otherwise; transient `TIMEOUT` / `NETWORK_ERROR` results
      do **not** downgrade `overall_status` because they are not the
      server's fault.
  All pre-R7 fields are preserved unchanged for backward compatibility.

## [0.2.0] - 2026-05-24

### Added

- (Sprint A) Cross-platform `_platform.py` shim with 25 tests covering
  POSIX/Windows file locking, `secure_chmod`, `restrictive_umask`,
  `state_root`, `notify_desktop`. 100% line + branch coverage.
- (Sprint A) `windows-latest` runner in CI matrix
  (3 OS × 2 Python = 6 cells).
- (Sprint A) CodeQL workflow for Python static analysis
  (push/PR + Mon 02:45 UTC).
- (Sprint B) **Form 4 XBRL parser** (`_xbrl.py`): structured parsing of
  insider trade reports using `defusedxml.ElementTree` for XXE /
  billion-laughs / external-entity protection. Returns frozen
  `Form4Data` with all transactions (date, code, shares, price,
  ownership form), insider role flags (officer / director / 10 %-owner),
  net buy/sell values, and `raw_warnings` for tolerated field-level
  issues. Hard 8 MiB input ceiling, 5 000-transaction cap per filing,
  and a 30-digit exponent ceiling guard against pathological inputs.
  Fuzz-tested with hypothesis (250+ generated examples).
- (Sprint B) **`get_form4_insider_trades` upgraded**: now fetches each
  filing's XML body and returns structured `transactions[].form4`
  (date, code, shares, price, ownership form, post_transaction_shares,
  is_derivative) plus `summary` (net_buy_value, net_sell_value,
  transaction_count, parse_failures) instead of metadata-only.
  Per-filing fetch / parse failures are surfaced as `parse_error` on
  the row instead of crashing the whole call. `_MAX_BODIES_PER_CALL=50`
  protects SEC rate-limit budget. Cache schema is unchanged — a hit
  avoids both the submissions JSON and every Form 4 body fetch.
- (Sprint B) **`get_8k_with_items` (7th tool)**: filter 8-K filings by
  item codes (1.01 entry into material agreement, 2.02 results of
  operations, 5.02 director/officer changes, 7.01 Reg FD, 9.01
  financial statements, etc.). Reuses the cached submissions JSON; new
  Pydantic `Get8KWithItemsInput` enforces an `X.YY` `ITEM_CODE_RE`
  regex.
- (Sprint B) **Hypothesis fuzz tests** for the parser: 12 hand-crafted
  seeds (XXE, billion-laughs, oversize, exotic Unicode, wrong root, …)
  and 3 property-based generators (well-formed random fields, random
  binary, random Unicode text). Surfaced one real Decimal-overflow bug
  that is now mitigated.
- (Sprint B) `defusedxml` runtime dependency and `hypothesis` dev
  dependency.
- 76 new tests (Sprint B XBRL parser + fuzz + 8-K tool + helper unit
  tests) plus 25 platform tests (Sprint A).

### Changed

- Tool count: 6 → 7 (5 business + 2 meta);
  `serverInfo.supportedTools` now reports 7 tools.
- `docs/THREAT_MODEL.md`: removes "Form 4 XML parsing" from the
  out-of-scope list and documents the new XML attack-surface
  mitigation strategy (defusedxml + size / depth / exponent caps +
  `Form4ParseError` instead of unhandled exceptions).
- Test count: 144 → 245 on Linux; total coverage 85.25% → 91.08%;
  `_xbrl.py` 97% line+branch coverage; 4 critical modules at 100%.

### Fixed

- `decimal.Overflow` bug in `_xbrl.py` discovered by hypothesis fuzz
  (commit `5b1d2ea`): cap Decimal exponents at 30 digits before
  parsing.

### Security

- All Form 4 XML parsing is delegated to `defusedxml.ElementTree`;
  external entity references, DTDs, and exponential entity expansion
  are refused before any business logic runs.
- The new `Form4ParseError` exception inherits from `SecError` so
  parse failures flow through the existing email-redacting framing
  pipeline.

### Compatibility

- `_platform.py` shim provides Windows native support (Tier A
  experimental; parent-dir 0o700 / file 0o600 enforced on POSIX with
  no-op + warning on Windows).
- Test count: 245 passed on Linux (91.08% coverage; `_xbrl.py` 97%,
  4 critical modules at 100%).

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

[Unreleased]: https://github.com/kevinkda/sec-edgar-mcp/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.2.2
[0.2.1]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/kevinkda/sec-edgar-mcp/releases/tag/v0.1.0
