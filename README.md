# sec-edgar-mcp

[English](./README.md) | [简体中文](./README_zh.md)

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11+-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

Read-only **Model Context Protocol (MCP)** server that wraps the
[SEC EDGAR](https://www.sec.gov/edgar.shtml) public API as **6 tools**
(4 business + 2 meta) for use inside Cursor, Claude Code, and any
MCP-aware agent.

> **Read-only** — every tool issues plain HTTPS GETs against
> `https://data.sec.gov/`, `https://www.sec.gov/cgi-bin/browse-edgar`, and
> `https://efts.sec.gov/LATEST/search-index`. Nothing is ever written back
> to SEC.

---

## Why a separate repo

`sec-edgar-mcp` is sister to `schwab-marketdata-mcp`. Where Schwab provides
prices and quotes, SEC EDGAR provides the corporate-action / disclosure
backbone (10-K / 10-Q / 8-K / Form 4 insider trades, S-1, proxy materials,
…). Both repos share the same hardening discipline:

- DuckDB-backed local cache (24 h for filings index, 6 h for Form 4, 30 d
  for filing text).
- httpx async client with token-bucket rate limit (SEC fair-use: ≤10 req/s).
- Pydantic v2 input validation (CIK / ticker / accession-number / form
  whitelist).
- Stdio hardening so log lines never corrupt the JSON-RPC stream.
- Structured error hierarchy (`SecNotFoundError`, `SecRateLimitError`,
  `SecValidationError`, `SecTransientError`).

---

## Cost & authentication

- **Cost:** $0 — SEC EDGAR is a free public service.
- **Auth:** none. SEC requires a descriptive `User-Agent` header in the
  form `"App Name (contact@email.example)"`. Set it via
  `SEC_EDGAR_USER_AGENT` in `.env`.

---

## Quick start

```bash
git clone https://github.com/kevinkda/sec-edgar-mcp.git
cd sec-edgar-mcp

uv sync --extra dev
uv run pre-commit install

cp .env.example .env
# edit .env — set SEC_EDGAR_USER_AGENT to "your-app/0.1 (you@example.com)"

uv run sec-edgar-mcp        # start the MCP server on stdio
```

Register the server with Cursor / Claude Desktop — see
[`docs/REGISTER.md`](./docs/REGISTER.md).

---

## Tooling surface

The server exposes **6 tools**: 4 business + 2 meta.

### `get_company_filings`

- **When to use:** to enumerate the most recent SEC filings for a single
  issuer (10-K, 10-Q, 8-K, Form 4, S-1, …) — the "what has $TICKER filed
  lately" query.
- **Input:** `cik_or_ticker: str` (e.g. `"AAPL"` or `"0000320193"`),
  optional `form_types: list[str]` (deny-listed against an internal
  allowlist of valid SEC form codes), `limit: int = 20` (≤200).
- **Output:** `{ company: {...}, filings: [{ accession_number, form,
  filing_date, primary_document, ... }, ...] }`.
- **Example call:**

  ```python
  get_company_filings(cik_or_ticker="AAPL", form_types=["10-K", "10-Q"], limit=5)
  ```

### `get_form4_insider_trades`

- **When to use:** to surface recent **Form 4** insider transactions
  (Section 16 officer / director trades) for an issuer in the last
  N days.
- **Input:** `cik_or_ticker: str`, `since_days: int = 30` (1 ≤ N ≤ 365).
- **Output:** `{ issuer: {...}, transactions: [{ insider_name, role,
  transaction_date, transaction_code, shares, price_per_share,
  shares_owned_after, accession_number, ... }, ...] }`.
- **Example call:**

  ```python
  get_form4_insider_trades(cik_or_ticker="MSFT", since_days=14)
  ```

### `get_filing_text`

- **When to use:** to pull the full text (HTML or plain TXT) of a single
  filing identified by accession number — the "show me the body of this
  10-K" query.
- **Input:** `accession_number: str` (`"0000320193-24-000123"` style),
  `document_type: "primary" | "complete" = "primary"`.
- **Output:** `{ accession_number, document_url, content_type, text,
  truncated, byte_size }`. Caps at 5 MB to keep MCP frames bounded.
- **Example call:**

  ```python
  get_filing_text(accession_number="0000320193-24-000123")
  ```

### `search_filings_full_text`

- **When to use:** to run an EDGAR full-text search across all filers (the
  "who mentioned $keyword in their 10-K" query).
- **Input:** `query: str` (1 ≤ len ≤ 200), optional `form_types`,
  `since_days: int = 90` (1 ≤ N ≤ 3650).
- **Output:** `{ query, total_hits, results: [{ accession_number, cik,
  company, form, filing_date, snippet, ... }, ...] }`. Pages capped at
  100 hits.
- **Example call:**

  ```python
  search_filings_full_text(query="cybersecurity incident", form_types=["8-K"], since_days=30)
  ```

### `health_check` (meta)

Local probe: returns server version, cache state, rate-limit budget, and
recent-error counter. Never calls SEC.

### `get_server_info` (meta)

Local metadata: server version, supported tools, MCP SDK version, OS hint.
Never calls SEC.

---

## Cache TTLs

| Table | TTL | Rationale |
| --- | --- | --- |
| `filings_index_cache` | 24 h | Filings index changes only when issuers file. |
| `form4_cache` | 6 h | Form 4 must be filed within 2 business days of trade. |
| `filing_text_cache` | 30 d | Filing bodies are immutable post-publication. |
| `search_cache` | 24 h | Full-text search is expensive on SEC's side. |

Override with `SEC_EDGAR_CACHE_BYPASS=1` for a single-call force-fresh.

---

## Documentation

- [`docs/REGISTER.md`](./docs/REGISTER.md) — Cursor / Claude Desktop
  registration steps.
- [`docs/THREAT_MODEL.md`](./docs/THREAT_MODEL.md) — STRIDE analysis.
- [`docs/RELEASE.md`](./docs/RELEASE.md) — release / version process.
- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — contributor workflow.

---

## License

MIT — see [LICENSE](./LICENSE).

---

## Responsible use

The SEC EDGAR data is in the public domain, but its bulk redistribution
is constrained by the SEC's
[Fair Access Policy](https://www.sec.gov/os/accessing-edgar-data). This
server is intended for **interactive research** by a single user agent;
do not embed it in a service that fan-outs more than ~10 req/s in
aggregate.
