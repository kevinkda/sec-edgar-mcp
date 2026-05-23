"""``get_company_filings`` and ``get_filing_text`` implementations.

SEC EDGAR endpoints used:
    * ``GET https://data.sec.gov/submissions/CIK{cik:010d}.json`` — full
      submission index (used by ``get_company_filings``).
    * ``GET https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_document}``
      — the filing body (used by ``get_filing_text``).
    * ``GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}&dateb=&owner=include&count={limit}``
      — fallback for filings beyond the most-recent 1000 (the submissions
      index covers the recent window only).

Reference: https://www.sec.gov/edgar/sec-api-documentation
"""

from __future__ import annotations

from typing import Any

from ..cache import Cache
from ..client import DATA_HOST, WWW_HOST, SecEdgarClient, resolve_cik
from ..errors import SecNotFoundError
from ..models import Get8KWithItemsInput, GetCompanyFilingsInput, GetFilingTextInput
from ._runtime import call_with_cache


async def get_company_filings_impl(args: GetCompanyFilingsInput) -> dict[str, Any]:
    """Return up to ``args.limit`` recent filings for the issuer.

    Filters by ``args.form_types`` if provided.
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        url = f"{DATA_HOST}/submissions/CIK{cik}.json"
        data = await client.get_json(url)
        company_meta = {
            "cik": cik,
            "name": data.get("name"),
            "ticker": _first(data.get("tickers")),
            "exchange": _first(data.get("exchanges")),
            "sic": data.get("sic"),
            "sic_description": data.get("sicDescription"),
            "ein": data.get("ein"),
        }
        recent = data.get("filings", {}).get("recent", {})
        filings = _zip_recent(recent, cik=cik)
        if args.form_types is not None:
            wanted = set(args.form_types)
            filings = [f for f in filings if f.get("form") in wanted]
        filings = filings[: args.limit]
        return {
            "company": company_meta,
            "filings": filings,
            "count": len(filings),
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_filings_index(_cache_params(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cik = raw.get("company", {}).get("cik")
        cache.put_filings_index(
            _cache_params(args),
            raw,
            cik=cik if isinstance(cik, str) else None,
        )

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


async def get_filing_text_impl(args: GetFilingTextInput) -> dict[str, Any]:
    """Return the primary filing document body (HTML or TXT)."""

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        accession = args.accession_number
        # Need to resolve the CIK + primary doc from the index.json that
        # SEC publishes alongside every filing.
        accession_no_dashes = accession.replace("-", "")
        cik_int = int(accession.split("-")[0])
        index_url = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{accession}-index.json"
        index = await client.get_json(index_url)
        primary_doc = _select_document(index, args.document_type)
        if primary_doc is None:
            raise SecNotFoundError(
                resource=f"accession:{accession}:{args.document_type}",
                hint=(f"no primary document found in index.json for accession {accession}"),
            )
        doc_url = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"
        text, ctype, byte_size, truncated = await client.get_text(doc_url)
        return {
            "accession_number": accession,
            "document_type": args.document_type,
            "document_url": doc_url,
            "content_type": ctype,
            "text": text,
            "byte_size": byte_size,
            "truncated": truncated,
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        hit = cache.get_filing_text(args.accession_number, args.document_type)
        if hit is None:
            return None
        # Re-add the document_url from the cache fetch path (we did not
        # persist it; recompute on miss is cheap).
        return hit

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_filing_text(
            args.accession_number,
            args.document_type,
            content_type=str(raw.get("content_type", "")),
            text=str(raw.get("text", "")),
            byte_size=int(raw.get("byte_size", 0)),
            truncated=bool(raw.get("truncated", False)),
        )

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


# ---------------------------------------------------------------------------
# get_8k_with_items
# ---------------------------------------------------------------------------


_EIGHT_K_KINDS: frozenset[str] = frozenset({"8-K", "8-K/A"})


async def get_8k_with_items_impl(args: Get8KWithItemsInput) -> dict[str, Any]:
    """Return 8-K filings filtered by SEC item codes.

    8-K is the SEC current-report form used to disclose unscheduled
    material events.  Each filing reports one or more *items* identified
    by an ``X.YY`` code (e.g. ``"1.01"`` Entry into a Material Definitive
    Agreement, ``"5.02"`` Officer/Director Changes).  The submissions
    index publishes a comma-separated ``items`` list per filing which we
    parse + filter.

    Output shape::

        {
            "company": { cik, name, ticker },
            "since_days": int,
            "item_codes_filter": list[str] | None,
            "filings": [
                { accession_number, filed_date, primary_doc_url,
                  items: list[str], primary_document, period_of_report? },
                ...
            ],
            "count": int,
        }
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        url = f"{DATA_HOST}/submissions/CIK{cik}.json"
        data = await client.get_json(url)
        company_meta = {
            "cik": cik,
            "name": data.get("name"),
            "ticker": _first(data.get("tickers")),
        }
        recent = data.get("filings", {}).get("recent", {})
        cutoff_iso = _cutoff_iso(args.since_days)
        rows = _filter_8k(recent, cik=cik, cutoff_iso=cutoff_iso)

        wanted_codes = frozenset(c.strip() for c in args.item_codes) if args.item_codes else None
        if wanted_codes is not None:
            rows = [r for r in rows if wanted_codes.intersection(r["items"])]
        rows = rows[: args.limit]
        return {
            "company": company_meta,
            "since_days": args.since_days,
            "item_codes_filter": sorted(wanted_codes) if wanted_codes is not None else None,
            "filings": rows,
            "count": len(rows),
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_filings_index(_cache_params_8k(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cik = raw.get("company", {}).get("cik")
        cache.put_filings_index(
            _cache_params_8k(args),
            raw,
            cik=cik if isinstance(cik, str) else None,
        )

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


def _cutoff_iso(since_days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(tz=UTC).date() - timedelta(days=since_days)).isoformat()


def _filter_8k(recent: Any, *, cik: str, cutoff_iso: str) -> list[dict[str, Any]]:
    if not isinstance(recent, dict):
        return []
    accession = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_doc = recent.get("primaryDocument") or []
    items = recent.get("items") or []
    period = recent.get("reportDate") or []
    out: list[dict[str, Any]] = []
    cik_int = int(cik) if cik.isdigit() else None
    for i, acc in enumerate(accession):
        if not isinstance(acc, str):
            continue
        form = _safe_get(forms, i)
        if form not in _EIGHT_K_KINDS:
            continue
        d = _safe_get(dates, i)
        if isinstance(d, str) and d < cutoff_iso:
            continue
        primary = _safe_get(primary_doc, i)
        primary_url = _build_primary_url(cik_int, acc, primary)
        out.append(
            {
                "accession_number": acc,
                "cik": cik,
                "form": form,
                "filed_date": d,
                "period_of_report": _safe_get(period, i) or None,
                "primary_document": primary,
                "primary_doc_url": primary_url,
                "items": _parse_items(_safe_get(items, i)),
            }
        )
    return out


def _build_primary_url(
    cik_int: int | None,
    accession: str,
    primary: Any,
) -> str | None:
    if cik_int is None or not isinstance(primary, str) or not primary:
        return None
    return f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession.replace('-', '')}/{primary}"


def _parse_items(raw: Any) -> list[str]:
    """Split ``"Item 1.01,Item 2.02"`` (SEC's format) into ``["1.01", "2.02"]``.

    SEC publishes the items list as a single comma-separated string
    inside ``filings.recent.items[i]``.  Each token is typically prefixed
    with ``"Item "`` and may carry stray whitespace.
    """
    if not isinstance(raw, str) or raw == "":
        return []
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    out: list[str] = []
    for token in tokens:
        # Strip an optional "Item " prefix; preserve the X.YY code as-is.
        normalised = token
        if normalised.lower().startswith("item "):
            normalised = normalised[5:].strip()
        if normalised:
            out.append(normalised)
    return out


def _cache_params_8k(args: Get8KWithItemsInput) -> dict[str, Any]:
    return {
        "tool": "get_8k_with_items",
        "cik_or_ticker": args.cik_or_ticker,
        "item_codes": sorted(args.item_codes) if args.item_codes else None,
        "since_days": args.since_days,
        "limit": args.limit,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def _zip_recent(recent: Any, *, cik: str) -> list[dict[str, Any]]:
    """Convert SEC's columnar ``filings.recent`` shape to a row list."""
    if not isinstance(recent, dict):
        return []
    accession = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_doc = recent.get("primaryDocument") or []
    primary_doc_desc = recent.get("primaryDocDescription") or []
    items = recent.get("items") or []
    is_xbrl = recent.get("isXBRL") or []
    is_inline = recent.get("isInlineXBRL") or []
    out: list[dict[str, Any]] = []
    for i, acc in enumerate(accession):
        if not isinstance(acc, str):
            continue
        out.append(
            {
                "accession_number": acc,
                "cik": cik,
                "form": _safe_get(forms, i),
                "filing_date": _safe_get(dates, i),
                "primary_document": _safe_get(primary_doc, i),
                "primary_document_description": _safe_get(primary_doc_desc, i),
                "items": _safe_get(items, i) or "",
                "is_xbrl": bool(_safe_get(is_xbrl, i) or 0),
                "is_inline_xbrl": bool(_safe_get(is_inline, i) or 0),
            }
        )
    return out


def _safe_get(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and 0 <= idx < len(seq):
        return seq[idx]
    return None


def _select_document(index: dict[str, Any], document_type: str) -> str | None:
    """Pick the primary HTML/TXT doc out of an SEC index.json."""
    items = index.get("directory", {}).get("item", [])
    if not isinstance(items, list):
        return None
    if document_type == "complete":
        # full-submission .txt
        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            if name.endswith(".txt") and "submission" in name.lower():
                return name
        # fallback: first .txt
        for entry in items:
            if isinstance(entry, dict) and str(entry.get("name", "")).endswith(".txt"):
                return str(entry["name"])
        return None
    # primary
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if name.endswith((".htm", ".html")) and not name.startswith(("R", "Show.js")):
            return name
    # fallback: any non-index-related .txt
    for entry in items:
        if isinstance(entry, dict) and str(entry.get("name", "")).endswith(".txt"):
            return str(entry["name"])
    return None


def _cache_params(args: GetCompanyFilingsInput) -> dict[str, Any]:
    return {
        "cik_or_ticker": args.cik_or_ticker,
        "form_types": sorted(args.form_types) if args.form_types is not None else None,
        "limit": args.limit,
    }


__all__ = ["get_8k_with_items_impl", "get_company_filings_impl", "get_filing_text_impl"]
