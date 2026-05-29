# Form 4 real-corpus fixtures

This directory contains **80 raw SEC EDGAR Form 4 ownership XML bodies**
captured from the public EDGAR API on **2026-05-29** as part of the
v0.4.1 R8 hotfix (sec-edgar-mcp v0.2.2).

## Why this corpus exists

Sprint v0.2.0's hand-crafted + hypothesis fuzz test suite passed 100 % but
PB-3 (insider-alert 2026-05-25) showed **0 / 70 real Form 4 filings
parsed successfully** with the error:

> ``malformed XML: mismatched tag: line 29, column 16``

Root cause: ``submissions.recent.primaryDocument[i]`` for Form 4 is
typically ``xsl<style>/<doc>.xml`` (e.g. ``xslF345X06/form4.xml``), which
is the **XSLT-rendered HTML view** — not the raw machine-readable
ownership XML.  Fuzz did not exercise this because the seed corpus was
synthetic.

Going forward this fixture set is the parser's **contract**: every
sample must continue to satisfy the corpus invariant in
``tests/test_xbrl_real_corpus.py`` and the fuzz seed in
``tests/test_xbrl_fuzz.py``.

## Provenance

- **Source**: SEC EDGAR public archive ``https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>.xml``
- **Capture date (UTC)**: 2026-05-29
- **Capture script**: ``scripts/fetch_form4_corpus.py`` (one-shot bootstrap, not imported by production code).
- **License**: SEC EDGAR data is public domain (17 U.S.C. § 105).
- **No PII / secrets**: filings are public; the Section 16 reporting-owner
  names that appear are already on the public docket; no API keys, tokens,
  or operator emails are present in this directory.

## Issuer / sector distribution

| Sector        | CIKs (representative tickers)                          |
| ------------- | ------------------------------------------------------ |
| Tech          | AAPL, GOOG, NVDA, TSLA, AMZN, META, MSFT, ADBE         |
| Financial     | JPM, BAC, MS, GS                                       |
| Staples       | PG, KO                                                 |
| ADR           | BABA (Alibaba), TSM (TSMC)                             |
| Healthcare    | JNJ, PFE                                               |
| Energy        | CVX (Chevron), XOM (ExxonMobil)                        |

Each CIK contributes up to 4 of its most recent Form 4 / 4-A filings.
The corpus also captures incidental filer-agent submissions (e.g. multiple
issuers filed through a common agent), which broadens the schema-version
and stylistic coverage without manual selection bias.

## Aggregate stats (as of 2026-05-29)

- 80 filings
- 242 transactions
- 9 distinct Section 16 transaction codes (S, M, F, A, G, P, C, H, D)
- 17 net-buy filings, 57 net-sell filings
- File sizes: ~ 1.5–37 KB; total < 1.5 MB (fits comfortably in git).

## Refreshing the corpus

The fixtures are committed at a point in time and are **not** auto-refreshed.
If a future SEC schema change requires a corpus refresh:

1. Run ``scripts/fetch_form4_corpus.py`` (requires ``SEC_EDGAR_USER_AGENT``).
2. Verify all entries parse via ``uv run pytest tests/test_xbrl_real_corpus.py -v``.
3. Update this README with the new capture date.
4. Commit the new ``.xml`` files in a separate ``chore(test)`` commit so
   the diff stays reviewable.

## Out of scope

- Form 5, Form 13F, Form 4-A amendment-only refinements (covered
  incidentally where the source CIK happens to file them).
- 8-K / 10-K / 10-Q / SC-13D / SC-13G filings — those have their own
  parsers (or none yet, in which case they are not in this directory).
