"""``get_form4_insider_trades`` implementation.

Form 4 = SEC Section 16 filing of insider transactions (officers / directors
/ ≥10 % shareholders).  Must be filed within 2 business days of the trade.

Strategy:
    1. Resolve CIK from ticker.
    2. Pull the recent submissions index (same endpoint as filings.py).
    3. Filter to ``form == "4"`` rows in the requested window.
    4. For each filing, return the metadata only (we do **not** parse the
       XML transaction details here — that would require fetching one body
       per filing and would explode the rate-limit budget).  The agent can
       follow-up with ``get_filing_text`` per accession to read the
       structured XML body.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..cache import Cache
from ..client import DATA_HOST, SecEdgarClient, resolve_cik
from ..models import GetForm4InsiderTradesInput
from ._runtime import call_with_cache

#: SEC accepts both Form 4 and Form 4/A (amendment).
_FORM4_KINDS: frozenset[str] = frozenset({"4", "4/A"})


async def get_form4_insider_trades_impl(
    args: GetForm4InsiderTradesInput,
) -> dict[str, Any]:
    """Return Form 4 filings for *cik_or_ticker* in the last *since_days*."""

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        url = f"{DATA_HOST}/submissions/CIK{cik}.json"
        data = await client.get_json(url)
        cutoff = datetime.now(tz=UTC).date() - timedelta(days=args.since_days)
        recent = data.get("filings", {}).get("recent", {})
        rows = _filter_form4(recent, cik=cik, cutoff_iso=cutoff.isoformat())
        return {
            "issuer": {
                "cik": cik,
                "name": data.get("name"),
                "ticker": _first(data.get("tickers")),
            },
            "since_days": args.since_days,
            "transactions": rows,
            "count": len(rows),
            "note": (
                "Returns Form 4 filing metadata only.  Use get_filing_text "
                "with each accession_number to parse the structured XML "
                "transaction details."
            ),
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_form4(_cache_params(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cik = raw.get("issuer", {}).get("cik")
        cache.put_form4(
            _cache_params(args),
            raw,
            cik=cik if isinstance(cik, str) else None,
        )

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


def _filter_form4(
    recent: dict[str, Any], *, cik: str, cutoff_iso: str
) -> list[dict[str, Any]]:
    if not isinstance(recent, dict):
        return []
    accession = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_doc = recent.get("primaryDocument") or []
    out: list[dict[str, Any]] = []
    for i, acc in enumerate(accession):
        if not isinstance(acc, str):
            continue
        form = _safe_get(forms, i)
        if form not in _FORM4_KINDS:
            continue
        d = _safe_get(dates, i)
        if isinstance(d, str) and d < cutoff_iso:
            continue
        out.append(
            {
                "accession_number": acc,
                "cik": cik,
                "form": form,
                "filing_date": d,
                "primary_document": _safe_get(primary_doc, i),
            }
        )
    return out


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def _safe_get(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and 0 <= idx < len(seq):
        return seq[idx]
    return None


def _cache_params(args: GetForm4InsiderTradesInput) -> dict[str, Any]:
    return {
        "cik_or_ticker": args.cik_or_ticker,
        "since_days": args.since_days,
    }


__all__ = ["get_form4_insider_trades_impl"]
