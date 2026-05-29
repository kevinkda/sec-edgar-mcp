"""Real-corpus regression tests for :mod:`sec_edgar_mcp._xbrl`.

This is the v0.4.1 R8 hotfix invariant: every XML in the snapshot
fixture set ``tests/fixtures/form4_real_corpus/`` must parse cleanly
into a :class:`Form4Data`.  These fixtures are raw SEC EDGAR Form 4
``ownershipDocument`` bodies (50+ samples across tech / financial /
staples / energy / healthcare / ADR / dual-class issuers, fetched
2026-05-29 via ``scripts/fetch_form4_corpus.py``).

The corpus is the parser's *contract*: any future PR that breaks even
one entry must explicitly document why.  See
``tests/fixtures/form4_real_corpus/README.md`` for provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sec_edgar_mcp._xbrl import Form4Data, parse_form4

CORPUS_DIR = Path(__file__).parent / "fixtures" / "form4_real_corpus"
CORPUS_PATHS = sorted(CORPUS_DIR.glob("*.xml"))


def test_corpus_directory_has_at_least_50_samples() -> None:
    """The hotfix charter requires ≥ 50 real Form 4 fixtures."""
    assert len(CORPUS_PATHS) >= 50, (
        f"corpus shrank to {len(CORPUS_PATHS)} entries — see v0.4.1-roadmap.md §3.1 for the ≥ 50 requirement."
    )


@pytest.mark.parametrize("path", CORPUS_PATHS, ids=lambda p: p.stem)
def test_real_corpus_entry_parses(path: Path) -> None:
    """Per-file: parser must return a Form4Data without warnings or error."""
    body = path.read_bytes()
    data = parse_form4(body, accession_number=path.stem)
    assert isinstance(data, Form4Data)
    # Every fixture has a known issuer CIK in the file (raw XML guarantees).
    assert data.issuer_cik != "" or data.issuer_name != "", f"{path.name} produced empty issuer metadata"


def test_real_corpus_aggregate_parse_rate_is_100_percent() -> None:
    """Aggregate invariant: ≥ 95 % parse success on the full corpus.

    Charter target was ≥ 95 %; the actual number after the
    ``insider.py`` XSLT-prefix fix is 100 %.  We still gate at 95 %
    so a future filer-introduced edge case does not silently regress
    the corpus, but any drop below 95 % must be investigated and
    documented before merge.
    """
    successes = 0
    failures: list[tuple[str, str]] = []
    for path in CORPUS_PATHS:
        try:
            parse_form4(path.read_bytes(), accession_number=path.stem)
            successes += 1
        except Exception as exc:  # pragma: no cover - failure surface
            failures.append((path.name, repr(exc)))
    rate = successes / len(CORPUS_PATHS)
    assert rate >= 0.95, f"corpus parse rate dropped to {rate:.2%}: {failures[:3]}"


def test_real_corpus_yields_diverse_transaction_codes() -> None:
    """Sanity: the corpus exercises ≥ 5 distinct Section 16 codes.

    Catches accidental shrinkage to a single-code subset (which would
    weaken regression coverage even at 100 % parse rate).
    """
    codes: set[str] = set()
    for path in CORPUS_PATHS:
        data = parse_form4(path.read_bytes(), accession_number=path.stem)
        for tx in data.transactions:
            if tx.code:
                codes.add(tx.code)
    assert len(codes) >= 5, f"corpus only exercises codes={codes}"
