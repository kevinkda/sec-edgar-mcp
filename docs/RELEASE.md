# Release process

`sec-edgar-mcp` follows the same release discipline as
`schwab-marketdata-mcp`.

## Cadence

- Patch (`0.1.x`): as needed for bug fixes; same-day green CI required.
- Minor (`0.2.0`): when adding a new tool or a non-breaking schema change.
- Major (`1.0.0`): once the API surface is considered stable.

## Steps

1. Make sure `main` is green (`bash scripts/local-ci.sh`).
2. Bump `__version__` in `src/sec_edgar_mcp/__init__.py` and `pyproject.toml`.
3. Update `CHANGELOG.md` under `## [Unreleased]` → new section with the
   target version + ISO date.
4. Commit:

   ```bash
   git commit -am "chore(release): v0.X.Y"
   ```

5. Tag and push:

   ```bash
   git tag -a v0.X.Y -m "v0.X.Y"
   git push origin main v0.X.Y
   ```

6. GitHub Actions builds and publishes the wheel.  Release notes are
   auto-extracted from `CHANGELOG.md`.

## Hotfix process

For a critical security fix on the latest release:

1. Branch off the tag: `git checkout -b hotfix/v0.X.Y v0.X.(Y-1)`.
2. Apply the minimum-diff fix and a test that fails before / passes after.
3. Bump the patch version and tag as above.
4. Cherry-pick to `main` if the fix isn't already there.

## Pre-release checklist

- [ ] `bash scripts/local-ci.sh` green
- [ ] `pytest --cov` ≥ 85 %
- [ ] `CHANGELOG.md` updated
- [ ] `README.md` and `README_zh.md` reflect any new tools / env vars
- [ ] `docs/REGISTER.md` example unchanged or updated to match new flags
- [ ] Pre-commit run: `uv run pre-commit run --all-files`

## Yanking a release

If a release ships a regression that cannot be hot-fixed forward, yank
the tag from PyPI / GitHub Releases and replace it with `0.X.(Y+1)`
containing the fix.  Do **not** retag — semver requires a bump.
