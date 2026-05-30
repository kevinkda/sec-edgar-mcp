# Known Issues

Tracked known issues and limitations for `sec-edgar-mcp`. For resolved
issues see [CHANGELOG.md](./CHANGELOG.md).

## Open

### 13F holdings and proxy-statement parsing not implemented

`sec-edgar-mcp` does not yet parse 13F institutional-holdings filings or
DEF 14A proxy statements. These are **v0.6 candidates**, not regressions.
Use the existing 8-K / 10-K / Form 4 tools for now.

### `SEC_EDGAR_USER_AGENT` must be a real, reachable email

SEC's Fair Access Policy requires the User-Agent header to carry a real
contact email. A placeholder / `noreply.github.com` UA is silently
accepted locally but rejected SEC-side with a fair-use 403 (this was the
R7 incident). `health_check` validates local env presence but **cannot**
confirm SEC-side acceptance — verify against a live SEC request.

## Upstream / Deferred

- **`mcp` 1.x → 2.x major bump deferred** — requires the compatibility
  checklist run manually; dependabot ignores the major bump.
- **SEC fair-use rate limit (10 req/s)** — exceeding it can get the
  operator's IP throttled or temporarily blocked. The client enforces a
  fair-use rate budget; bulk redistribution is out of scope (interactive
  single-user research only). See `docs/THREAT_MODEL.md`.

## Resolved

- **R8 — Form 4 XBRL parser 100 % parse failure** (PB-3 2026-05-25):
  the tool was fetching SEC's XSLT-rendered HTML instead of raw ownership
  XML. Fixed in **v0.2.2** (2026-05-29) with a 2-layer fix + an 80-entry
  real-corpus contract test (≥ 95 % parse rate). See CHANGELOG v0.2.2.

See [CHANGELOG.md](./CHANGELOG.md) for the full history.
