# Register `sec-edgar-mcp` with your MCP host

This guide shows how to wire the server up inside Cursor and Claude Desktop.

> **Prereq:** finish the bootstrap in the [Quick Start](../README.md#quick-start)
> first — `uv sync --extra dev`, copy `.env.example` to `.env`, and set
> `SEC_EDGAR_USER_AGENT` to a real value.  Without that env var the server
> refuses to start.

---

## Cursor (`mcp.json`)

Open Cursor → Settings → MCP → "Add New MCP Server", or edit
`~/.cursor/mcp.json` directly:

```json
{
  "mcpServers": {
    "sec-edgar-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/opt/workspace/code/kevinkda/sec-edgar-mcp",
        "run",
        "sec-edgar-mcp"
      ],
      "envFile": "/opt/workspace/code/kevinkda/sec-edgar-mcp/.env"
    }
  }
}
```

- Replace the `--directory` path with wherever you cloned the repo.
- `envFile` points at the `.env` you populated; Cursor reads it before
  spawning the server so `SEC_EDGAR_USER_AGENT` reaches the process.

Restart Cursor.  In the agent panel you should see 6 tools come online:

```text
get_company_filings
get_form4_insider_trades
get_filing_text
search_filings_full_text
health_check
get_server_info
```

---

## Claude Desktop (`claude_desktop_config.json`)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "sec-edgar-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/sec-edgar-mcp",
        "run",
        "sec-edgar-mcp"
      ],
      "env": {
        "SEC_EDGAR_USER_AGENT": "your-app/0.1 (you@example.com)"
      }
    }
  }
}
```

Claude Desktop does not support `envFile` so we inline the env var.  Quit
and restart Claude.

---

## Verifying the connection

Once registered, ask the agent:

> Run health_check on sec-edgar-mcp

Expected response (the agent will surface this from the tool):

```json
{
  "server_version": "0.1.0",
  "user_agent_configured": true,
  "rate_limit_per_sec": 8,
  "rate_limit_hard_cap": 10,
  "cache_enabled": true,
  "cache_size_mb": 0.0,
  "platform_supported": true
}
```

If `user_agent_configured` is `false`, the server is running but
`SEC_EDGAR_USER_AGENT` is not reaching the process.  Re-check `envFile`
or `env` in the host config.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Tools don't show up | wrong `--directory` path | absolute path required |
| `SecConfigurationError` on every call | missing `SEC_EDGAR_USER_AGENT` | populate `.env`; restart host |
| `SecRateLimitError` | concurrent agents sharing IP exceeded 10 req/s | lower `SEC_EDGAR_RATE_LIMIT_PER_SEC` |
| `SecNotFoundError: ticker:XYZ` | ticker not in SEC's published ticker map | use the numeric CIK |
