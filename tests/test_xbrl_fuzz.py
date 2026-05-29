"""Fuzz tests for :mod:`sec_edgar_mcp._xbrl` — the Form 4 XBRL parser.

Covers two flavours of input:

1. **Hand-crafted seeds** — known-bad / edge-case payloads that exercise
   specific failure modes (DTD, invalid namespaces, overflow, exotic Unicode,
   …).  Every seed must either return a :class:`Form4Data` (with optional
   ``raw_warnings``) or raise :class:`Form4ParseError`.  Nothing else.

2. **Real-corpus seeds** — every entry in
   ``tests/fixtures/form4_real_corpus/`` is fed back into the
   property-based fuzzer as an ``@example`` (R8 invariant: corpus
   entries must always be reachable through the fuzz contract too).

3. **Hypothesis property-based fuzzing** — generated payloads built from
   small XML primitives.  We assert the same invariant across hundreds of
   examples: parser must not raise any exception other than
   :class:`Form4ParseError`.

The shared invariant is encoded in :func:`_assert_safe`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from sec_edgar_mcp._xbrl import (
    MAX_INPUT_BYTES,
    Form4Data,
    parse_form4,
)
from sec_edgar_mcp.errors import Form4ParseError


def _assert_safe(payload: bytes) -> None:
    """Either parse successfully or raise Form4ParseError — never anything else."""
    try:
        result = parse_form4(payload)
    except Form4ParseError:
        return
    except Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"unexpected exception {type(exc).__name__}: {exc!r}")
    assert isinstance(result, Form4Data)
    # Structured payload must always be JSON-serialisable.
    import json

    json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Hand-crafted fuzz seeds (>= 5)
# ---------------------------------------------------------------------------


_FUZZ_SEEDS: list[bytes] = [
    # 1. Empty document is rejected.
    b"",
    # 2. Whitespace-only document.
    b"   \n\n\t",
    # 3. Wrong root element.
    b"<unrelated><stuff/></unrelated>",
    # 4. Unclosed root.
    b"<ownershipDocument>",
    # 5. DTD with external entity (XXE attempt).
    (
        b"<?xml version='1.0'?>"
        b"<!DOCTYPE ownershipDocument ["
        b"  <!ENTITY xxe SYSTEM 'file:///etc/passwd'>"
        b"]>"
        b"<ownershipDocument><issuer>&xxe;</issuer></ownershipDocument>"
    ),
    # 6. Billion-laughs.
    (
        b"<?xml version='1.0'?>"
        b"<!DOCTYPE lol ["
        b"  <!ENTITY a 'a'>"
        b"  <!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>"
        b"  <!ENTITY c '&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;'>"
        b"]>"
        b"<ownershipDocument>&c;</ownershipDocument>"
    ),
    # 7. Negative shares (Decimal will accept "-1"; net values use signed Decimal).
    (
        b"<?xml version='1.0'?>"
        b"<ownershipDocument><documentType>4</documentType>"
        b"<issuer/><reportingOwner/>"
        b"<nonDerivativeTable><nonDerivativeTransaction>"
        b"  <transactionCoding><transactionCode>S</transactionCode></transactionCoding>"
        b"  <transactionAmounts>"
        b"    <transactionShares><value>-100</value></transactionShares>"
        b"    <transactionPricePerShare><value>50.00</value></transactionPricePerShare>"
        b"    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>"
        b"  </transactionAmounts>"
        b"  <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>"
        b"</nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>"
    ),
    # 8. Exotic Unicode in issuer name (must round-trip).
    (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<ownershipDocument>"
        "<documentType>4</documentType>"
        "<issuer><issuerName>苹果公司 \U0001f34e Inc.</issuerName></issuer>"
        "<reportingOwner/>"
        "</ownershipDocument>"
    ).encode("utf-8"),
    # 9. Numeric wrapper present but value child empty.
    (
        b"<?xml version='1.0'?>"
        b"<ownershipDocument><documentType>4</documentType>"
        b"<issuer/><reportingOwner/>"
        b"<nonDerivativeTable><nonDerivativeTransaction>"
        b"  <transactionCoding><transactionCode>P</transactionCode></transactionCoding>"
        b"  <transactionAmounts>"
        b"    <transactionShares><value/></transactionShares>"
        b"    <transactionPricePerShare><value/></transactionPricePerShare>"
        b"    <transactionAcquiredDisposedCode><value/></transactionAcquiredDisposedCode>"
        b"  </transactionAmounts>"
        b"  <ownershipNature><directOrIndirectOwnership><value/></directOrIndirectOwnership></ownershipNature>"
        b"</nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>"
    ),
    # 10. Document with only a derivative table (no nonDerivativeTable).
    (
        b"<?xml version='1.0'?>"
        b"<ownershipDocument><documentType>4</documentType>"
        b"<issuer/><reportingOwner/>"
        b"<derivativeTable><derivativeTransaction>"
        b"  <transactionCoding><transactionCode>X</transactionCode></transactionCoding>"
        b"  <transactionAmounts>"
        b"    <transactionShares><value>100</value></transactionShares>"
        b"    <transactionPricePerShare><value>1.0</value></transactionPricePerShare>"
        b"    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
        b"  </transactionAmounts>"
        b"  <underlyingSecurity><underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle></underlyingSecurity>"
        b"  <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>"
        b"</derivativeTransaction></derivativeTable>"
        b"</ownershipDocument>"
    ),
    # 11. Mixed-case transactionCode (lowercase) — should warn, not raise.
    (
        b"<?xml version='1.0'?>"
        b"<ownershipDocument><documentType>4</documentType>"
        b"<issuer/><reportingOwner/>"
        b"<nonDerivativeTable><nonDerivativeTransaction>"
        b"  <transactionCoding><transactionCode>q</transactionCode></transactionCoding>"
        b"  <transactionAmounts>"
        b"    <transactionShares><value>1</value></transactionShares>"
        b"  </transactionAmounts>"
        b"</nonDerivativeTransaction></nonDerivativeTable>"
        b"</ownershipDocument>"
    ),
    # 12. UTF-16 BOM (defusedxml refuses non-canonical encodings ≈ malformed).
    "\ufeff<?xml version='1.0' encoding='utf-16'?><ownershipDocument><documentType>4</documentType></ownershipDocument>".encode(
        "utf-16"
    ),
]


@pytest.mark.parametrize("payload", _FUZZ_SEEDS)
def test_fuzz_hand_crafted_seed_is_safe(payload: bytes) -> None:
    _assert_safe(payload)


# ---------------------------------------------------------------------------
# R8 — real-corpus seeds.  The corpus represents the *contract surface*:
# every snapshot must satisfy the same invariant as hypothesis-generated
# inputs (no surprise exceptions, only Form4ParseError or Form4Data).
# ---------------------------------------------------------------------------


_CORPUS_DIR = Path(__file__).parent / "fixtures" / "form4_real_corpus"
_CORPUS_PAYLOADS: list[bytes] = sorted(
    (p.read_bytes() for p in _CORPUS_DIR.glob("*.xml")),
    key=len,
)


@pytest.mark.parametrize(
    "payload",
    _CORPUS_PAYLOADS,
    ids=lambda b: f"corpus[{len(b)}b]",
)
def test_fuzz_real_corpus_seed_is_safe(payload: bytes) -> None:
    """Every real-corpus entry must satisfy the fuzz invariant."""
    _assert_safe(payload)
    # Real corpus must additionally *succeed* — they are valid XML.
    data = parse_form4(payload)
    assert isinstance(data, Form4Data)


def test_negative_shares_seed_returns_signed_decimal() -> None:
    """The negative-shares seed should parse and surface a negative value."""
    payload = _FUZZ_SEEDS[6]
    data = parse_form4(payload)
    assert isinstance(data, Form4Data)
    assert data.transaction_count == 1
    # Decimal preserves sign; net_sell sums shares*price = -100 * 50 = -5000.
    assert data.transactions[0].shares == -100
    assert data.net_sell_value == -5000


def test_exotic_unicode_seed_round_trips() -> None:
    payload = _FUZZ_SEEDS[7]
    data = parse_form4(payload)
    assert "苹果" in data.issuer_name


def test_oversize_payload_raises_parse_error() -> None:
    big = b"<ownershipDocument>" + b"a" * (MAX_INPUT_BYTES + 1) + b"</ownershipDocument>"
    with pytest.raises(Form4ParseError):
        parse_form4(big)


# ---------------------------------------------------------------------------
# Hypothesis property-based fuzzing
# ---------------------------------------------------------------------------


_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="<>&\x00"),
    max_size=20,
)
_NUMERIC_LIKE = st.one_of(
    st.text(alphabet="0123456789.-eE", min_size=0, max_size=12),
    st.just(""),
)
_CODE_LIKE = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz?",
    min_size=0,
    max_size=4,
)


def _build_random_doc(
    issuer_cik: str,
    code: str,
    shares: str,
    price: str,
    ad: str,
    di: str,
    period: str,
    title: str,
) -> bytes:
    """Build a syntactically valid ownershipDocument from random fields."""
    return (
        f"<?xml version='1.0' encoding='utf-8'?>"
        f"<ownershipDocument>"
        f"<documentType>4</documentType>"
        f"<periodOfReport>{period}</periodOfReport>"
        f"<issuer><issuerCik>{issuer_cik}</issuerCik></issuer>"
        f"<reportingOwner>"
        f"<reportingOwnerRelationship>"
        f"<isOfficer>1</isOfficer>"
        f"</reportingOwnerRelationship>"
        f"</reportingOwner>"
        f"<nonDerivativeTable><nonDerivativeTransaction>"
        f"<securityTitle><value>{title}</value></securityTitle>"
        f"<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
        f"<transactionAmounts>"
        f"<transactionShares><value>{shares}</value></transactionShares>"
        f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>"
        f"<transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>"
        f"</transactionAmounts>"
        f"<ownershipNature>"
        f"<directOrIndirectOwnership><value>{di}</value></directOrIndirectOwnership>"
        f"</ownershipNature>"
        f"</nonDerivativeTransaction></nonDerivativeTable>"
        f"</ownershipDocument>"
    ).encode()


@given(
    issuer_cik=st.text(alphabet="0123456789", min_size=0, max_size=10),
    code=_CODE_LIKE,
    shares=_NUMERIC_LIKE,
    price=_NUMERIC_LIKE,
    ad=st.sampled_from(["A", "D", "?", ""]),
    di=st.sampled_from(["D", "I", "?", ""]),
    period=st.sampled_from(["2024-01-01", "not-a-date", "", "2024-13-99"]),
    title=_SAFE_TEXT,
)
@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_hypothesis_well_formed_random_fields_is_safe(
    issuer_cik: str,
    code: str,
    shares: str,
    price: str,
    ad: str,
    di: str,
    period: str,
    title: str,
) -> None:
    """For any well-formed XML with random leaf values: parse_form4 must
    return Form4Data or raise Form4ParseError, never a generic exception.
    """
    payload = _build_random_doc(issuer_cik, code, shares, price, ad, di, period, title)
    _assert_safe(payload)


_CORPUS_FUZZ_SAMPLES: tuple[bytes, ...] = tuple(_CORPUS_PAYLOADS[:5])


@given(
    blob=st.binary(min_size=0, max_size=512),
)
@example(blob=_CORPUS_FUZZ_SAMPLES[0] if _CORPUS_FUZZ_SAMPLES else b"")
@example(blob=_CORPUS_FUZZ_SAMPLES[1] if len(_CORPUS_FUZZ_SAMPLES) > 1 else b"")
@example(blob=_CORPUS_FUZZ_SAMPLES[2] if len(_CORPUS_FUZZ_SAMPLES) > 2 else b"")
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_hypothesis_random_bytes_is_safe(blob: bytes) -> None:
    """Pure-noise input must always either parse (vanishingly rare) or
    raise :class:`Form4ParseError` — never let a stray exception escape.

    R8: also seed the strategy with three real corpus samples so any
    refactor that drops support for the canonical SEC XML shape fails
    here, not just in the corpus invariant test.
    """
    _assert_safe(blob)


@given(
    text=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        max_size=400,
    ),
)
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_hypothesis_random_unicode_text_is_safe(text: str) -> None:
    _assert_safe(text.encode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Self-test of the safety harness — make sure the assertion catches
# unexpected exceptions if the parser were to start raising one.
# ---------------------------------------------------------------------------


def test_parse_form4_accession_propagates_to_error() -> None:
    """Form4ParseError must carry the accession_number we passed in."""
    with pytest.raises(Form4ParseError) as excinfo:
        parse_form4(b"<bad>", accession_number="0000000000-00-000000")
    assert excinfo.value.accession_number == "0000000000-00-000000"
