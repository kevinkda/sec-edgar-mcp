# Security

`sec-edgar-mcp` is a read-only MCP server that issues plain HTTPS GETs
against SEC-owned EDGAR hosts. It has **no OAuth, no bearer token, no
refresh token, no order-placement path, and no customer-account data** —
the smallest attack surface of the MCP fleet.

For the full STRIDE catalogue and trust-boundary detail, see
[`docs/THREAT_MODEL.md`](./THREAT_MODEL.md). This document is the short
operator-facing summary.

## Threat model (summary)

There is no secret credential. The remaining concerns are:

- **`SEC_EDGAR_USER_AGENT` PII** — the operator-supplied User-Agent
  typically contains a personal email (required by SEC Fair Access).
  Leaking it via logs or exception text is a low-impact PII issue;
  mitigated by not logging PII at the default WARNING level.
- **Fair-use rate abuse** — exceeding SEC's ~10 req/s budget can get the
  operator's IP throttled or temporarily blocked. The client enforces a
  fair-use rate budget.
- **TLS spoofing / MITM** — httpx `verify=True` always; never disabled.
  Cache writes are local-process only, protected by DuckDB's file lock;
  corruption is detected and the database is quarantined.
- **Bulk redistribution** — SEC's Fair Access Policy restricts large-scale
  resale of EDGAR data; this server is for interactive single-user
  research only.

## Secret handling

- **No secret credential is required.** The only operator-supplied value
  is `SEC_EDGAR_USER_AGENT` (a contact email), sourced from `.env`
  (git-ignored). It is treated as low-sensitivity PII and is not logged at
  the default level.
- Pre-commit runs `detect-secrets`; CI runs `gitleaks-action@v2` on every
  push and PR (defence-in-depth even though no high-value secret exists).

## Read/write boundary

This MCP is **read-only by design**: it performs HTTPS GET requests only
against SEC EDGAR; there is no write / mutation path of any kind.

## Reporting security issues

Open a private security advisory on GitHub:
<https://github.com/kevinkda/sec-edgar-mcp/security/advisories>.
Do **not** open a public issue with the details.
