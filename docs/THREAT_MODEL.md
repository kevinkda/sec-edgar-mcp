# Threat Model — `sec-edgar-mcp`

## Summary

`sec-edgar-mcp` is a read-only MCP server that issues plain HTTPS GETs
against three SEC-owned hosts.  Compared to `schwab-marketdata-mcp` the
attack surface is small: there is no OAuth, no Bearer token, no refresh
token, no order-placement path, and no customer-account data.

The remaining concerns are:

1. Operator-supplied **`SEC_EDGAR_USER_AGENT`** typically contains a
   personal email.  Leaking it via logs or exception text is a low-impact
   PII issue but still worth defending against.
2. **Fair-use rate limiting** — exceeding 10 req/s can get the operator's
   IP throttled or temporarily blocked by SEC.
3. **Bulk redistribution** — SEC's
   [Fair Access Policy](https://www.sec.gov/os/accessing-edgar-data)
   restricts large-scale resale of EDGAR data even though the data itself
   is public.  This server is intended for **interactive single-user
   research**.

## STRIDE

### Spoofing

- **Threat:** an attacker spoofs a SEC TLS endpoint and serves modified
  filings to influence agent decisions.
- **Mitigation:** httpx defaults to `verify=True`; we never disable TLS
  verification.  Operators must keep their CA bundle current (managed by
  the OS / `certifi`).

### Tampering

- **Threat:** a man-in-the-middle alters response bodies.
- **Mitigation:** TLS as above.  Cache writes are local-process only and
  protected by DuckDB's own file lock; corruption is detected and the
  database is quarantined (`cache.duckdb.corrupt-<ts>`).

### Repudiation

- **Threat:** the operator denies running a query.
- **Mitigation:** every tool call is logged to
  `${XDG_STATE_HOME}/sec-edgar-mcp/logs/server.log` (rotated, 5 × 10 MiB
  by default).  No PII is logged at the default WARNING level.

### Information disclosure

- **Threat:** operator's contact email leaks via exception text or logs.
- **Mitigation:** every custom exception calls `redact_email()` in its
  constructor; structured fields are typed (`field: str`, etc.) so a
  raw `repr(exc)` cannot accidentally include a User-Agent echo.
- **Threat:** filing bodies stored in DuckDB cache are world-readable.
- **Mitigation:** parent dir created with `0o700`, DB file `chmod 0o600`
  on POSIX (best-effort no-op on Windows; relying on
  `%LOCALAPPDATA%` ACL inheritance).

### Denial of service

- **Threat:** agent fan-out exceeds SEC's 10 req/s ceiling and gets the
  operator IP-blocked.
- **Mitigation:** in-process token-bucket (sliding 1-second window),
  default 8 req/s, hard cap 10 req/s regardless of operator override.
  429 responses are retried with `Retry-After`.
- **Threat:** large filings exhaust agent memory or MCP frame budget.
- **Mitigation:** `client.get_text()` enforces a 5 MiB cap and reports
  `truncated=true` so callers can decide to chunk.

### Elevation of privilege

- **Threat:** server escalates beyond read-only.
- **Mitigation:** the server only issues HTTP GETs.  There is no SEC
  write API — the EDGAR XBRL / submission ingestion path requires
  X.509 certificates and is not reachable from any code path here.

## Out of scope

- **Bulk re-publication** of EDGAR data is **explicitly not a supported
  use case**.  Operators who do so are responsible for compliance with
  SEC's Fair Access Policy and any applicable copyright on derived works
  (e.g. third-party normalised SEC datasets).

## Form 4 XML parsing — attack surface

As of v0.2 the `get_form4_insider_trades` tool fetches each Form 4
filing's XML body and parses it via `sec_edgar_mcp._xbrl.parse_form4`.
XML parsers historically introduce three classes of risk: external
entity expansion (XXE), exponential entity expansion ("billion
laughs"), and unbounded numeric / string materialisation.

| Risk | Mitigation |
| --- | --- |
| XXE / external entity references | All parsing goes through `defusedxml.ElementTree`, which refuses external general or parameter entity references. |
| Billion-laughs / exponential entity expansion | `defusedxml` caps entity expansion depth and total expansion bytes. |
| DTD parsing | Disabled by default in `defusedxml`. |
| Oversize document | Hard 8 MiB ceiling enforced **before** the XML parser is invoked (`MAX_INPUT_BYTES`). |
| Pathologically large numbers | `Decimal` strings are rejected if they exceed 30 characters or have an absolute exponent > 30 (`MAX_NUMERIC_DIGITS`). The accumulator catches `decimal.Overflow` defensively. |
| Unbounded transaction list | Capped at `MAX_TRANSACTIONS = 5_000` per filing. |
| Bytes-out-of-frame | Per-call cap on Form 4 bodies fetched (`_MAX_BODIES_PER_CALL = 50`) keeps the SEC fair-use budget bounded even on chatty issuers. |
| Field-level malformation | Tolerated — set field to `None`/`Decimal("0")` and append a structured warning to `Form4Data.raw_warnings` instead of raising. Only structural failures raise the new `Form4ParseError`. |

The parser is exercised by 12 hand-crafted fuzz seeds (XXE, billion-
laughs, oversize, exotic Unicode, wrong root, …) and three Hypothesis
property-based generators (well-formed random fields, random binary,
random Unicode text).  The invariant under test is: **`parse_form4`
returns `Form4Data` or raises `Form4ParseError` — never any other
exception**.

## Cache failure modes

| Failure | Behavior |
| --- | --- |
| DB file does not exist | created on demand under `XDG_STATE_HOME` |
| DB file is corrupt | renamed `cache.duckdb.corrupt-<ts>`; fresh DB created |
| Disk full / read-only fs | every method logs WARNING and returns `None` |
| Concurrent process opens | DuckDB intra-process lock serialises writes |

In every failure case the cache **degrades to a no-op**; the live SEC
API path is still followed, with the rate limiter honoured.
