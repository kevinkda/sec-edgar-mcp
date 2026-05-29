"""Fetch a diverse real Form 4 corpus from SEC EDGAR for parser remediation tests.

Run:
    SEC_EDGAR_USER_AGENT="..." uv run python scripts/fetch_form4_corpus.py

Writes XML files to ``tests/fixtures/form4_real_corpus/<accession>.xml`` and
emits a summary line per CIK.  Reuses the configured SEC user agent and
respects the same rate-limit budget the production client uses.

This is a one-shot bootstrap script for the v0.4.1 R8 hotfix; it is not
imported by any production code path.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests" / "fixtures" / "form4_real_corpus"

# 20 CIKs across tech / financial / staples / energy / healthcare / ADR /
# dual-class.  We cap fetches per CIK so we keep the corpus diverse.
ISSUERS: list[tuple[str, str, str]] = [
    # tech
    ("0000320193", "AAPL", "Apple"),
    ("0001652044", "GOOG", "Alphabet"),
    ("0001045810", "NVDA", "NVIDIA"),
    ("0001318605", "TSLA", "Tesla"),
    ("0001018724", "AMZN", "Amazon"),
    ("0001326801", "META", "Meta"),
    ("0000789019", "MSFT", "Microsoft"),
    ("0000796343", "ADBE", "Adobe"),
    # financial
    ("0000019617", "JPM", "JPMorgan"),
    ("0000070858", "BAC", "BankOfAmerica"),
    ("0000895421", "MS", "MorganStanley"),
    ("0000886982", "GS", "GoldmanSachs"),
    # staples
    ("0000080424", "PG", "ProcterGamble"),
    ("0000021344", "KO", "CocaCola"),
    # ADR
    ("0001403161", "BABA", "Alibaba"),
    ("0001046179", "TSM", "TSMC"),
    # healthcare
    ("0000200406", "JNJ", "JNJ"),
    ("0000078003", "PFE", "Pfizer"),
    # energy
    ("0000093410", "CVX", "Chevron"),
    ("0000034088", "XOM", "ExxonMobil"),
]

PER_CIK_TARGET = 4  # ~ 80 candidates → keep ≥ 50 after dedupe / cleanup
MAX_BYTES = 500_000  # any Form 4 > 500 KB is a malformed wrapper, skip

DATA_HOST = "https://data.sec.gov"
WWW_HOST = "https://www.sec.gov"


async def fetch_one_cik(client: httpx.AsyncClient, cik: str, ticker: str, name: str) -> int:
    """Fetch up to PER_CIK_TARGET Form 4 XMLs for a single CIK."""
    submissions_url = f"{DATA_HOST}/submissions/CIK{cik}.json"
    r = await client.get(submissions_url)
    if r.status_code != 200:
        print(f"[{ticker}] submissions HTTP {r.status_code}", file=sys.stderr)
        return 0
    try:
        data = r.json()
    except Exception as exc:
        print(f"[{ticker}] submissions JSON decode failed: {exc}", file=sys.stderr)
        return 0

    recent = data.get("filings", {}).get("recent", {})
    accession_list = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    primary_docs = recent.get("primaryDocument") or []

    cik_int = int(cik)
    written = 0
    for i, accession in enumerate(accession_list):
        if written >= PER_CIK_TARGET:
            break
        if i >= len(forms) or i >= len(primary_docs):
            break
        form_type = forms[i]
        if form_type not in {"4", "4/A"}:
            continue
        primary = primary_docs[i]
        if not isinstance(primary, str) or not primary:
            continue
        # Only fetch if the primary document looks like an XML (we want raw
        # ownership XML, not the HTML rendering); SEC primary docs for
        # Form 4 are typically ``wf-form4_*.xml`` or ``primary_doc.xml``.
        if not primary.lower().endswith(".xml"):
            # Some filers route through wrapper; skip these to keep corpus pure.
            continue

        accession_no_dashes = accession.replace("-", "")
        # SEC's submissions API returns the XSLT-rendered HTML path
        # (``xsl<style>/<doc>.xml``) as ``primaryDocument`` for Form 4.
        # The *raw* ownership XML lives one level up at
        # ``<doc>.xml`` — strip the XSLT prefix so the corpus is the
        # parser's actual contract.
        primary_raw = primary.split("/", 1)[1] if primary.startswith("xsl") and "/" in primary else primary
        xml_url = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_raw}"
        # rate-limit: gentle 0.15s between requests
        await asyncio.sleep(0.15)
        try:
            x = await client.get(xml_url, timeout=15.0)
        except httpx.HTTPError as exc:
            print(f"[{ticker}] body fetch error {accession}: {exc}", file=sys.stderr)
            continue
        if x.status_code != 200:
            print(
                f"[{ticker}] body HTTP {x.status_code} for {accession}",
                file=sys.stderr,
            )
            continue
        body = x.content
        if len(body) > MAX_BYTES:
            print(
                f"[{ticker}] body too large ({len(body)}) for {accession}",
                file=sys.stderr,
            )
            continue
        out_path = CORPUS / f"{accession}.xml"
        out_path.write_bytes(body)
        written += 1
        print(f"[{ticker}] wrote {accession} ({len(body)} bytes)")
    print(f"[{ticker}] -> {written} fixtures")
    return written


async def main() -> None:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not ua:
        print("ERROR: SEC_EDGAR_USER_AGENT must be set.", file=sys.stderr)
        sys.exit(2)
    CORPUS.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    total = 0
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        for cik, ticker, name in ISSUERS:
            try:
                total += await fetch_one_cik(client, cik, ticker, name)
            except Exception as exc:
                print(f"[{ticker}] ERROR: {exc}", file=sys.stderr)
            # Inter-issuer pause to stay well under SEC fair-use ceiling.
            await asyncio.sleep(0.3)
    print(f"\nTOTAL fixtures written: {total}")


if __name__ == "__main__":
    asyncio.run(main())
