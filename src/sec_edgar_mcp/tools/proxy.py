"""``get_proxy_statement`` implementation (DEF 14A).

A DEF 14A is the definitive proxy statement a company files ahead of its
annual shareholder meeting — disclosing executive compensation, the
board / director nominees, and shareholder proposals up for a vote.

Unlike Form 4 / 13F, DEF 14A is HTML, so we extract key facts from a
bounded, tag-stripped plain-text projection via
:mod:`sec_edgar_mcp._proxy` rather than feeding untrusted issuer HTML
into an XML parser (which would needlessly widen the XXE attack
surface).

SEC endpoints used:
    * ``GET https://data.sec.gov/submissions/CIK{cik:010d}.json`` — pick
      the most-recent DEF 14A filing.
    * ``GET https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}``
      — the proxy body (HTML / TXT).
"""

from __future__ import annotations

import logging
from typing import Any

from .._proxy import extract_proxy_statement
from ..cache import Cache
from ..client import DATA_HOST, WWW_HOST, SecEdgarClient, resolve_cik
from ..errors import SecNotFoundError
from ..models import GetProxyStatementInput
from ._runtime import call_with_cache

log = logging.getLogger(__name__)

#: SEC proxy-statement form kinds we accept (definitive + additional).
_PROXY_KINDS: tuple[str, ...] = ("DEF 14A", "DEFA14A", "DEFR14A")


async def get_proxy_statement_impl(args: GetProxyStatementInput) -> dict[str, Any]:
    """Return key facts extracted from the issuer's most-recent DEF 14A.

    Output shape::

        {
            "company": { cik, name, ticker },
            "accession_number": str,
            "form": "DEF 14A" | ...,
            "filing_date": str,
            "document_url": str,
            "proxy": { ... ProxyStatementData.to_dict() ... },
        }
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        data = await client.get_json(f"{DATA_HOST}/submissions/CIK{cik}.json")
        recent = data.get("filings", {}).get("recent", {})
        chosen = _select_proxy(recent)
        if chosen is None:
            raise SecNotFoundError(
                resource=f"DEF 14A:{cik}",
                hint=f"no DEF 14A filing found in the recent window for CIK {cik}",
            )

        cik_int = int(cik)
        accession = chosen["accession_number"]
        primary_doc = chosen.get("primary_document")
        if not isinstance(primary_doc, str) or not primary_doc:
            raise SecNotFoundError(
                resource=f"DEF 14A:{cik}:{accession}",
                hint="DEF 14A filing has no primary document in the submissions index",
            )
        accession_no_dashes = accession.replace("-", "")
        doc_url = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"
        text, _ctype, _size, _trunc = await client.get_text(doc_url)

        parsed = extract_proxy_statement(text, accession_number=accession)
        return {
            "company": {
                "cik": cik,
                "name": data.get("name"),
                "ticker": _first(data.get("tickers")),
            },
            "accession_number": accession,
            "form": chosen.get("form"),
            "filing_date": chosen.get("filing_date"),
            "document_url": doc_url,
            "proxy": parsed.to_dict(),
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


def _select_proxy(recent: Any) -> dict[str, Any] | None:
    """Pick the most-recent DEF 14A (preferred) / DEFA14A / DEFR14A row."""
    if not isinstance(recent, dict):
        return None
    accession = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_doc = recent.get("primaryDocument") or []
    rows: list[dict[str, Any]] = []
    for i, acc in enumerate(accession):
        if not isinstance(acc, str):
            continue
        form = _safe_get(forms, i)
        if form not in _PROXY_KINDS:
            continue
        rows.append(
            {
                "accession_number": acc,
                "form": form,
                "filing_date": _safe_get(dates, i),
                "primary_document": _safe_get(primary_doc, i),
                "_rank": _PROXY_KINDS.index(form),
            }
        )
    if not rows:
        return None
    # Stable sort: most-recent filing date first, then prefer the
    # canonical DEF 14A rank (stability preserves date order within rank).
    rows.sort(key=lambda r: _date_key(r.get("filing_date")), reverse=True)
    rows.sort(key=lambda r: r["_rank"])
    chosen = dict(rows[0])
    chosen.pop("_rank", None)
    return chosen


def _date_key(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def _safe_get(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and 0 <= idx < len(seq):
        return seq[idx]
    return None


def _cache_params(args: GetProxyStatementInput) -> dict[str, Any]:
    return {
        "tool": "get_proxy_statement",
        "cik_or_ticker": args.cik_or_ticker,
    }


__all__ = ["get_proxy_statement_impl"]
