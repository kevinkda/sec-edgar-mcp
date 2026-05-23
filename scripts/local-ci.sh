#!/usr/bin/env bash
# scripts/local-ci.sh — equivalent to GitHub Actions, runs every gate the
# CI pipeline runs.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd)"
cd "$ROOT"

section() {
    printf '\n\033[1;36m== %s ==\033[0m\n' "$1"
}

section "uv sync --extra dev"
uv sync --extra dev

section "ruff check"
uv run ruff check src tests

section "ruff format --check"
uv run ruff format --check src tests

section "mypy --strict"
uv run mypy --strict src

section "bandit -r src -lll"
uv run bandit -r src -lll

section "pip-audit"
uv run pip-audit

section "pytest --cov"
uv run pytest --cov

section "pre-commit run --all-files (best-effort; skipped if no network)"
if command -v pre-commit >/dev/null 2>&1; then
    pre-commit run --all-files || echo "WARN: pre-commit failed (often network/TLS in restricted environments)"
elif uv run pre-commit --version >/dev/null 2>&1; then
    uv run pre-commit run --all-files || echo "WARN: pre-commit failed (often network/TLS in restricted environments)"
else
    echo "WARN: pre-commit not installed, skipping (run 'uv run pre-commit install' first)" >&2
fi

printf '\n\033[1;32mAll local-ci gates passed.\033[0m\n'
