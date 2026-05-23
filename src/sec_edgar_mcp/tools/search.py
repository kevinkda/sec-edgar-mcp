"""``search_filings_full_text`` implementation.

EDGAR full-text search endpoint:
    ``GET https://efts.sec.gov/LATEST/search-index?q=<query>&dateRange=custom&startdt=<>&enddt=<>&forms=<>``

The response is a JSON envelope ``{"hits":{"hits":[{...}, ...], "total":{...}}}``
in Elasticsearch shape.  We normalise to ``{"results": [...], "total_hits": int}``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import Cache
from ..client import SEARCH_HOST, SecEdgarClient
from ..models import SearchFilingsFullTextInput
from ._runtime import call_with_cache

#: EDGAR caps ``size`` at 100 per page.  We expose a single page; agents
#: that need more should narrow ``query`` or shrink ``since_days``.
_MAX_HITS_PER_PAGE: int = 100


async def search_filings_full_text_impl(
    args: SearchFilingsFullTextInput,
) -> dict[str, Any]:
    """Run an EDGAR full-text search."""

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        end = datetime.now(tz=UTC).date()
        start = end - timedelta(days=args.since_days)
        params: dict[str, Any] = {
            "q": args.query,
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": 0,
            "size": _MAX_HITS_PER_PAGE,
        }
        if args.form_types:
            params["forms"] = ",".join(args.form_types)
        url = f"{SEARCH_HOST}/LATEST/search-index"
        raw = await client.get_json(url, params=params)
        normalised = _normalise(raw, query=args.query)
        return normalised

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_search(_cache_params(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_search(_cache_params(args), raw)

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


def _normalise(raw: dict[str, Any], *, query: str) -> dict[str, Any]:
    hits_block = raw.get("hits", {})
    total_block = hits_block.get("total", {}) if isinstance(hits_block, dict) else {}
    total = total_block.get("value", 0) if isinstance(total_block, dict) else total_block
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = 0
    inner = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
    results: list[dict[str, Any]] = []
    if isinstance(inner, list):
        for h in inner:
            if not isinstance(h, dict):
                continue
            src = h.get("_source", {})
            src = src if isinstance(src, dict) else {}
            results.append(
                {
                    "accession_number": src.get("adsh") or h.get("_id"),
                    "cik": _first(src.get("ciks")),
                    "company": _first(src.get("display_names")) or src.get("display_names"),
                    "form": src.get("form"),
                    "filing_date": src.get("file_date"),
                    "snippet": _first(
                        h.get("highlight", {}).get("text") if isinstance(h.get("highlight"), dict) else None
                    )
                    or src.get("description"),
                    "score": h.get("_score"),
                }
            )
    return {
        "query": query,
        "total_hits": total_int,
        "returned": len(results),
        "results": results,
    }


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def _cache_params(args: SearchFilingsFullTextInput) -> dict[str, Any]:
    return {
        "query": args.query,
        "form_types": sorted(args.form_types) if args.form_types is not None else None,
        "since_days": args.since_days,
    }


__all__ = ["search_filings_full_text_impl"]
