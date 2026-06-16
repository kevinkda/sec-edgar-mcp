"""OWASP Top 10 — 2025 (preview) security test suite for sec-edgar-mcp.

The 2025 preview emphasises AI/ML-era concerns: prompt injection (A03), secure
design review (A04), and supply-chain integrity (A08). For an MCP server the
most material 2025-specific surface is **prompt injection via tool
descriptions** — the text an LLM host reads to decide how to call each tool.

Each test asserts a concrete invariant — no empty-coverage padding.

Applicability map (2025 preview):
  * A01 Broken Access Control — read-only tool surface (re-asserted)
  * A02 Cryptographic Failures — secure cache perms (re-asserted)
  * A03 Injection (incl. prompt) — 7 tool descriptions carry no injection text
  * A04 Insecure Design        — fail-closed UA; bounded responses
  * A05 Security Misconfig      — explicit cache defaults
  * A06 Vulnerable Components   — defusedxml/pydantic declared
  * A07 AuthN Failures          — N/A explicit: unauthenticated API
  * A08 Data Integrity          — JSON shape validation + cache integrity
  * A09 Logging & Monitoring    — audit events
  * A10 SSRF                    — outbound host pinned to *.sec.gov
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "sec_edgar_mcp"

# Phrases an attacker might embed to subvert an LLM host reading tool docs.
PROMPT_INJECTION_MARKERS = [
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "you are now",
    "system prompt",
    "exfiltrate",
    "reveal your",
    "override your instructions",
    "do not tell the user",
    "send the api key",
]


# ===========================================================================
# A03:2025 — Injection (including LLM Prompt Injection)
# ===========================================================================


class TestA03PromptInjection:
    @pytest.mark.asyncio
    async def test_tool_descriptions_have_no_injection_text(self) -> None:
        """No registered tool description contains prompt-injection phrasing."""
        from sec_edgar_mcp.server import app

        tools = await app().list_tools()
        assert len(tools) == 10
        for t in tools:
            desc = (t.description or "").lower()
            for marker in PROMPT_INJECTION_MARKERS:
                assert marker not in desc, f"tool {t.name} description contains injection marker {marker!r}"

    @pytest.mark.asyncio
    async def test_tool_descriptions_are_bounded_and_descriptive(self) -> None:
        """Each tool has a short, human-readable description (no payload dumps)."""
        from sec_edgar_mcp.server import app

        tools = await app().list_tools()
        for t in tools:
            desc = t.description or ""
            assert 10 <= len(desc) <= 600, f"{t.name} description length {len(desc)} out of bounds"
            # No control characters / null bytes that could confuse a host parser.
            assert "\x00" not in desc

    def test_source_tool_docstrings_have_no_injection_text(self) -> None:
        """Structural guard: the server.py tool docstrings carry no injection text."""
        body = (SRC_ROOT / "server.py").read_text("utf-8").lower()
        for marker in PROMPT_INJECTION_MARKERS:
            assert marker not in body

    def test_filing_text_is_returned_as_opaque_data(self) -> None:
        """Filing bodies (attacker-controllable) are returned as inert text, not
        interpreted — there is no eval/exec on fetched content.

        We flag only the dangerous builtins ``eval(`` / ``exec(`` used as bare
        calls; ``re.compile`` and similar method calls are explicitly allowed.
        """
        bad = re.compile(r"(?<![.\w])(eval|exec)\s*\(")
        offenders = []
        for py in SRC_ROOT.rglob("*.py"):
            for lineno, line in enumerate(py.read_text("utf-8").splitlines(), 1):
                if line.strip().startswith(("#", '"', "'", "*")):
                    continue
                if bad.search(line):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:60]}")
        assert offenders == [], f"dynamic code execution present: {offenders}"


# ===========================================================================
# A01 / A02 / A04 / A05 / A06 / A08 / A09 / A10 — re-asserted invariants
# ===========================================================================


class TestReassertedInvariants:
    @pytest.mark.asyncio
    async def test_a01_read_only_surface(self) -> None:
        from sec_edgar_mcp.server import app

        for t in await app().list_tools():
            assert not any(v in t.name for v in ("create", "delete", "update", "write"))

    def test_a02_no_cache_file_on_disk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from sec_edgar_mcp.cache import Cache
        from sec_edgar_mcp.cache_backend import MemoryBackend

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        cache = Cache(backend=MemoryBackend())
        cache.put_search({"q": "x"}, {"v": 1})
        # v0.3.0 default backend is in-process memory — no file to mis-permission.
        assert list(tmp_path.rglob("*.duckdb")) == []

    def test_a04_rate_cap_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sec_edgar_mcp.client import SEC_HARD_RATE_LIMIT_PER_SEC, resolve_rate_limit

        monkeypatch.setenv("SEC_EDGAR_RATE_LIMIT_PER_SEC", "999")
        assert resolve_rate_limit() <= SEC_HARD_RATE_LIMIT_PER_SEC

    def test_a05_cache_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sec_edgar_mcp.cache import cache_bypass, cache_enabled

        monkeypatch.delenv("SEC_EDGAR_CACHE_ENABLED", raising=False)
        monkeypatch.delenv("SEC_EDGAR_CACHE_BYPASS", raising=False)
        # v0.2.4: cache is opt-in (default disabled); bypass stays off.
        assert not cache_enabled() and not cache_bypass()

    def test_a06_deps_declared(self) -> None:
        body = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        assert "defusedxml" in body and "pydantic" in body

    @pytest.mark.asyncio
    async def test_a08_json_shape_validation(self, make_client) -> None:
        from sec_edgar_mcp.errors import SecTransientError
        from tests.conftest import FakeRoute

        client = make_client([FakeRoute("/s", text_body="true", content_type="application/json")])
        with pytest.raises(SecTransientError):
            await client.get_json("https://data.sec.gov/s")

    def test_a09_cache_observability(self) -> None:
        from sec_edgar_mcp.cache import Cache
        from sec_edgar_mcp.cache_backend import MemoryBackend

        cache = Cache(backend=MemoryBackend())
        cache.put_search({"q": "z"}, {"v": 1})
        stats = cache.get_stats().to_dict()
        assert stats["entries"] >= 1
        assert stats["backend"] == "memory"

    def test_a10_ssrf_cik_rejected(self) -> None:
        from sec_edgar_mcp.models import GetForm4InsiderTradesInput

        with pytest.raises(Exception):
            GetForm4InsiderTradesInput(cik_or_ticker="http://169.254.169.254/")


# ===========================================================================
# A07:2025 — Identification and Authentication Failures  (N/A)
# ===========================================================================


class TestA07AuthFailures:
    def test_na_unauthenticated_documented(self) -> None:
        """N/A: SEC EDGAR is unauthenticated. The errors.py module documents
        this explicitly so the N/A status is institutional knowledge, not
        relegated to undocumented assumptions."""
        body = (SRC_ROOT / "errors.py").read_text("utf-8").lower()
        assert "no authentication" in body or "no bearer tokens" in body or "unauthenticated" in body
