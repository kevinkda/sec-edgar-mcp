# sec-edgar-mcp

[English](./README.md) | [简体中文](./README_zh.md)

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11+-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

Read-only **Model Context Protocol (MCP)** server that wraps the
[SEC EDGAR](https://www.sec.gov/edgar.shtml) public API as **10 tools**
(8 business + 2 meta) for use inside Cursor, Claude Code, and any
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

- Pluggable response cache (24 h for filings index, 6 h for Form 4, 30 d
  for filing text) — **disabled by default (opt-in)**; enable with
  `SEC_EDGAR_CACHE_ENABLED=true`. The default backend is an in-process
  memory LRU (zero external dependency, concurrency-safe, non-blocking);
  ClickHouse is an optional backend (`pip install sec-edgar-mcp[clickhouse]`)
  for derived-analysis history.
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

The server exposes **10 tools**: 8 business + 2 meta.

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
  N days, with **structured transaction data** (date, code, shares,
  price, ownership form) parsed from the filing's XBRL body.
- **Input:** `cik_or_ticker: str`, `since_days: int = 30` (1 ≤ N ≤ 365).
- **Output:** `{ issuer: {...}, transactions: [{ accession_number,
  form, filing_date, primary_document, form4: { issuer_*,
  reporting_owner_*, is_officer, is_director, transactions: [...],
  net_buy_value, net_sell_value, ... } | null, parse_error: str | null
  }], summary: { transaction_count, net_buy_value, net_sell_value,
  parse_failures } }`.
- **Security:** XML parsing uses `defusedxml.ElementTree` —
  XXE / billion-laughs / external-entity references are refused.
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

### `get_8k_with_items`

- **When to use:** to pull recent **8-K current-report** filings for an
  issuer and filter them by SEC item codes — the "show me MSFT's recent
  Item 5.02 director/officer changes" query.
- **Input:** `cik_or_ticker: str`, optional `item_codes: list[str]`
  (e.g. `["1.01", "5.02"]` — must match `^\d{1,2}\.\d{1,2}$`),
  `since_days: int = 30` (1 ≤ N ≤ 3650), `limit: int = 50` (1 ≤ N ≤ 200).
- **Output:** `{ company: {...}, filings: [{ accession_number,
  filed_date, period_of_report, items: list[str], primary_document,
  primary_doc_url, ... }, ...] }`.
- **Example call:**

  ```python
  get_8k_with_items(cik_or_ticker="MSFT", item_codes=["5.02", "1.01"], since_days=180)
  ```

### `get_13f_holdings`

- **When to use:** to read an **institutional investment manager's**
  quarterly 13F-HR holdings — "what did Berkshire Hathaway hold last
  quarter?". `cik_or_ticker` identifies the **filer** (the manager),
  not a held company.
- **Input:** `cik_or_ticker: str`, optional `quarter: str` (`YYYYQN`,
  e.g. `"2024Q3"`). When omitted, the most-recent 13F-HR is used.
- **Output:** `{ manager: {cik, name}, report_date, accession_number,
  form, value_units: "thousands" | "dollars", holding_count,
  total_value_reported, total_value_usd, total_shares,
  holdings: [{ name_of_issuer, title_of_class, cusip, value,
  shares_or_principal_amount, shares_or_principal_type, put_call,
  investment_discretion, voting_authority_sole/shared/none }, ...],
  warnings }`.
- **Security:** the information-table XML is parsed with `defusedxml`
  (external entities / billion-laughs refused). The SEC **2023Q3
  value-unit cutover** (thousands → whole dollars) is handled — both the
  raw reported value and a normalised whole-dollar total are returned.
- **Example call:**

  ```python
  get_13f_holdings(cik_or_ticker="1067983", quarter="2024Q2")  # Berkshire Hathaway
  ```

### `get_institutional_holders`

- **When to use:** the reverse of `get_13f_holdings` — "which 13F managers
  report a position in $TICKER?". Aggregates recent 13F-HR full-text
  search hits into distinct filers.
- **Input:** `ticker: str`, `since_days: int = 120` (1 ≤ N ≤ 550),
  `limit: int = 50` (1 ≤ N ≤ 200).
- **Output:** `{ ticker, company, holder_count, holders: [{ cik, name,
  filings, latest_filing_date, latest_accession }, ...],
  search_total_hits, warnings }`.
- **Note:** bounded by `since_days` — a manager whose latest 13F predates
  the window will not appear. This is an approximation built on SEC's
  full-text index, not a definitive holders register.
- **Example call:**

  ```python
  get_institutional_holders(ticker="AAPL", since_days=120)
  ```

### `get_proxy_statement`

- **When to use:** to extract key facts from an issuer's most-recent
  **DEF 14A proxy statement** — meeting / record dates, the auditor,
  shareholder proposals, and executive-compensation figures.
- **Input:** `cik_or_ticker: str`.
- **Output:** `{ company: {cik, name, ticker}, accession_number, form,
  filing_date, document_url, proxy: { meeting_date, record_date,
  fiscal_year, auditor, proposals: [{number, title}, ...],
  proposal_count, max_total_compensation, compensation_figures,
  warnings } }`.
- **Security:** DEF 14A is HTML, so facts are extracted from a **bounded,
  tag-stripped plain-text projection** — untrusted issuer HTML is never
  fed to an XML parser, keeping the XXE attack surface closed. All
  extracted fields are length-capped.
- **Example call:**

  ```python
  get_proxy_statement(cik_or_ticker="AAPL")
  ```

### `health_check` (meta)

Local + server-side health probe. Returns server version, cache state,
rate-limit budget, recent-error counter, and the new `sec_ua_reachable`
field (R7) — a cached HEAD probe against EDGAR that reports whether
SEC's edge actually accepts the configured `SEC_EDGAR_USER_AGENT`:

| `sec_ua_reachable.status` | Meaning |
| --- | --- |
| `ACCEPTED` | SEC returned 200 to a HEAD on the probe URL. |
| `REJECTED_HTML_403` | SEC fair-access policy banned the UA. **Fix `.env`.** |
| `UNCONFIGURED` | UA missing, malformed, or contains a known placeholder (e.g. `noreply.github.com`). |
| `TIMEOUT` | Probe timed out (transient — does not affect `overall_status`). |
| `NETWORK_ERROR` | Other HTTP / network failure (transient). |

`overall_status` aggregates: `unhealthy` if `UNCONFIGURED`, `degraded`
if `REJECTED_HTML_403`, `ok` otherwise. Probe result cached 5 minutes
to avoid hidden rate-limit consumption.

### `get_server_info` (meta)

Local metadata: server version, supported tools, MCP SDK version, OS hint.
Never calls SEC.

---

## Cache backends & TTLs

| Table | TTL | Rationale |
| --- | --- | --- |
| `filings_index_cache` | 24 h | Filings index changes only when issuers file. |
| `form4_cache` | 6 h | Form 4 must be filed within 2 business days of trade. |
| `filing_text_cache` | 30 d | Filing bodies are immutable post-publication. |
| `search_cache` | 24 h | Full-text search is expensive on SEC's side. |

The cache is **disabled by default (opt-in)** — enable it explicitly with
`SEC_EDGAR_CACHE_ENABLED=true` (also accepts `1` / `yes` / `on`). Once
enabled, override with `SEC_EDGAR_CACHE_BYPASS=1` for a single-call
force-fresh.

### Pluggable backends

The cache delegates to a pluggable backend selected via
`SEC_EDGAR_CACHE_BACKEND` (default `memory`):

| Backend | Select with | Dependency | Use |
| --- | --- | --- | --- |
| `memory` (default) | unset / `SEC_EDGAR_CACHE_BACKEND=memory` | none (stdlib) | In-process LRU + TTL response cache. Concurrency-safe, non-blocking, zero files. Works out of the box. |
| `clickhouse` | `SEC_EDGAR_CACHE_BACKEND=clickhouse` + `SEC_EDGAR_CLICKHOUSE_URL` | `pip install sec-edgar-mcp[clickhouse]` | Adds durable derived-analysis history (true concurrent read/write). |

Without ClickHouse, derived-analysis history requests degrade gracefully —
they return `{"status": "requires_clickhouse_persistence", "hint": ...}`
instead of raising. **Core tools behave identically with or without
ClickHouse.**

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
