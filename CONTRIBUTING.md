# Contributing to `sec-edgar-mcp`

Thanks for taking the time to contribute.  This project is small and
batch-orientated; a tight, focused PR is much easier to review than a
large omnibus.

## Bootstrap

```bash
git clone https://github.com/kevinkda/sec-edgar-mcp.git
cd sec-edgar-mcp

uv sync --extra dev
uv run pre-commit install
```

Copy `.env.example` to `.env` and set `SEC_EDGAR_USER_AGENT` so the
integration tests can spot-check live SEC behavior if you opt in.

## Workflow

1. Create a topic branch from `main`:

   ```bash
   git switch -c feature/short-description
   ```

2. Make small, logical commits.  Conventional commit prefixes
   (`feat`, `fix`, `docs`, `test`, `chore`, `refactor`) are required.
3. Run the full local CI gate before pushing:

   ```bash
   bash scripts/local-ci.sh
   ```

   This runs `ruff check`, `ruff format --check`, `mypy --strict`,
   `bandit -r src -lll`, `pip-audit`, `pytest --cov`, and (best-effort)
   `pre-commit run --all-files`.

4. Open a PR using the template in `.github/PULL_REQUEST_TEMPLATE.md`.

## Code style

- Python 3.11+ with full type hints.
- 120-char line limit (handled by ruff format).
- Errors raised by the public surface MUST be subclasses of `SecError`.
- Do not log raw response bodies.  The User-Agent contains an operator
  email; the existing `redact_email()` helper protects exception text but
  log calls should still avoid `%r` on raw response objects.
- New tools must include:
  - A Pydantic input model in `models.py` with anchored regexes.
  - Unit tests for normal / 404 / 429 / 5xx paths.
  - A README "Tooling surface" entry with the four-section format
    (when to use / input / output / example).

## Security

- Never commit secrets — pre-commit hooks will block obvious cases via
  `detect-secrets` (always on) and `gitleaks` (manual stage).
- Never disable TLS verification.
- Do not hard-code an email in source; the User-Agent comes from
  `SEC_EDGAR_USER_AGENT`.

## Licensing

By submitting a PR you agree your contribution is licensed under MIT
(matching the repo `LICENSE`).
