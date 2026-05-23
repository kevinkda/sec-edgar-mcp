# sec-edgar-mcp

[English](./README.md) | [简体中文](./README_zh.md)

只读 **Model Context Protocol (MCP)** 服务器，将
[SEC EDGAR](https://www.sec.gov/edgar.shtml) 公开 API 包装为 **6 个工具**
（4 业务 + 2 meta），可在 Cursor、Claude Code 以及任意 MCP 客户端中使用。

> **只读** —— 每个工具都只对 SEC 公开端点发起 HTTPS GET，不会回写。

---

## 为什么独立成仓

`sec-edgar-mcp` 是 `schwab-marketdata-mcp` 的姐妹仓。Schwab 提供行情/分钟线，
SEC EDGAR 提供公司行为和披露主干（10-K / 10-Q / 8-K / Form 4 内部人交易、
S-1、委托书 …）。两个仓库共享同一套硬化纪律：

- DuckDB 本地缓存（filings 24 小时；Form 4 6 小时；filing 全文 30 天）。
- httpx 异步客户端 + 令牌桶限速（SEC 公平使用：≤10 req/s）。
- Pydantic v2 入参校验（CIK / ticker / accession-number / 表单白名单）。
- stdio 加固，日志永远不会污染 JSON-RPC 流。
- 结构化错误体系（`SecNotFoundError` / `SecRateLimitError` / `SecValidationError` /
  `SecTransientError`）。

---

## 成本与认证

- **成本：** 0 元 —— SEC EDGAR 是免费公开服务。
- **认证：** 无。但 SEC 强制要求 `User-Agent` 头形如
  `"App Name (contact@email.example)"`。请在 `.env` 中设置
  `SEC_EDGAR_USER_AGENT`。

---

## 快速开始

```bash
git clone https://github.com/kevinkda/sec-edgar-mcp.git
cd sec-edgar-mcp

uv sync --extra dev
uv run pre-commit install

cp .env.example .env
# 编辑 .env —— 把 SEC_EDGAR_USER_AGENT 改成 "your-app/0.1 (you@example.com)"

uv run sec-edgar-mcp        # 在 stdio 上启动 MCP 服务器
```

在 Cursor / Claude Desktop 中注册 —— 见 [`docs/REGISTER.md`](./docs/REGISTER.md)。

---

## 工具清单

服务器对外暴露 **6 个工具**：4 业务 + 2 meta。

| Tool | 何时用 | 入参 | 缓存 TTL |
| --- | --- | --- | --- |
| `get_company_filings` | 列出某公司最近的 SEC filings | `cik_or_ticker`, `form_types?`, `limit=20` | 24 h |
| `get_form4_insider_trades` | 拉某公司 N 天内的 Form 4 内部人交易 | `cik_or_ticker`, `since_days=30` | 6 h |
| `get_filing_text` | 拉某 filing 的全文（HTML/TXT） | `accession_number`, `document_type=primary` | 30 d |
| `search_filings_full_text` | EDGAR 全文搜索 | `query`, `form_types?`, `since_days=90` | 24 h |
| `health_check` | 本地健康探针（不调 SEC） | 无 | n/a |
| `get_server_info` | 本地版本/工具列表（不调 SEC） | 无 | n/a |

详细的"何时用 / 入参 / 返回 / 示例"四段式见 [README.md](./README.md#tooling-surface)。

---

## 文档

- [`docs/REGISTER.md`](./docs/REGISTER.md) —— Cursor / Claude Desktop 注册步骤。
- [`docs/THREAT_MODEL.md`](./docs/THREAT_MODEL.md) —— STRIDE 威胁模型。
- [`docs/RELEASE.md`](./docs/RELEASE.md) —— 发布流程。
- [`CONTRIBUTING.md`](./CONTRIBUTING.md) —— 贡献者工作流。

---

## License

MIT —— 见 [LICENSE](./LICENSE)。

---

## 负责任使用

SEC EDGAR 数据本身在公共领域，但批量再分发受
[SEC Fair Access Policy](https://www.sec.gov/os/accessing-edgar-data) 约束。
本服务器面向**单用户的交互式研究**；不要嵌入到聚合 ≥10 req/s 的服务里。
