"""``get_form4_insider_trades`` implementation.

Form 4 = SEC Section 16 filing of insider transactions (officers / directors
/ ≥10 % shareholders).  Must be filed within 2 business days of the trade.

Sprint B upgrade (v0.2): we now fetch each Form 4 filing's XML body and
parse it via :mod:`sec_edgar_mcp._xbrl` so the tool returns structured
transaction data (date, code, shares, price, ownership form) instead of
filing metadata only.  Parsing is bounded:

* one HTTP GET per filing inside the ``since_days`` window;
* per-filing failures are tolerated — the filing's ``parse_error`` field
  carries the reason and the row is still returned with metadata, so a
  single bad XML never poisons the whole response;
* the cached payload is the structured form, so a cache hit avoids the
  body fetches entirely.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .._xbrl import Form4Data, parse_form4
from ..cache import Cache
from ..client import DATA_HOST, WWW_HOST, SecEdgarClient, resolve_cik
from ..errors import Form4ParseError, SecError
from ..models import GetForm4InsiderTradesInput
from ._runtime import call_with_cache

log = logging.getLogger(__name__)

#: SEC accepts both Form 4 and Form 4/A (amendment).
_FORM4_KINDS: frozenset[str] = frozenset({"4", "4/A"})

#: Hard ceiling on the number of Form 4 bodies we fetch per call to keep
#: the SEC fair-use budget bounded even when the issuer is filing very
#: actively (e.g. a popular big-cap during earnings week).
_MAX_BODIES_PER_CALL: int = 50


async def get_form4_insider_trades_impl(
    args: GetForm4InsiderTradesInput,
) -> dict[str, Any]:
    """Return Form 4 filings with structured transactions in the last *since_days*.

    Output shape::

        {
            "issuer": { cik, name, ticker },
            "since_days": int,
            "transactions": [
                {
                    "accession_number": str,
                    "form": "4" | "4/A",
                    "filing_date": str,  # ISO date
                    "primary_document": str | None,
                    "form4": dict | None,  # Form4Data.to_dict() or None on failure
                    "parse_error": str | None,
                },
                ...
            ],
            "count": int,            # number of Form 4 filings in the window
            "summary": {
                "transaction_count": int,
                "net_buy_value": str,    # Decimal as string
                "net_sell_value": str,
            },
        }
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        url = f"{DATA_HOST}/submissions/CIK{cik}.json"
        data = await client.get_json(url)
        cutoff = datetime.now(tz=UTC).date() - timedelta(days=args.since_days)
        recent = data.get("filings", {}).get("recent", {})
        rows = _filter_form4(recent, cik=cik, cutoff_iso=cutoff.isoformat())
        rows = rows[:_MAX_BODIES_PER_CALL]

        cik_int = int(cik)
        for row in rows:
            primary_doc = row.get("primary_document")
            accession = row["accession_number"]
            if not isinstance(primary_doc, str) or not primary_doc:
                row["form4"] = None
                row["parse_error"] = "no primary_document in submissions index"
                continue
            await _enrich_with_xbrl(client, row, cik_int=cik_int, accession=accession, primary_doc=primary_doc)

        summary = _summarise(rows)
        return {
            "issuer": {
                "cik": cik,
                "name": data.get("name"),
                "ticker": _first(data.get("tickers")),
            },
            "since_days": args.since_days,
            "transactions": rows,
            "count": len(rows),
            "summary": summary,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _enrich_with_xbrl(
    client: SecEdgarClient,
    row: dict[str, Any],
    *,
    cik_int: int,
    accession: str,
    primary_doc: str,
) -> None:
    """Fetch + parse the Form 4 XML body for *row*; mutate it in-place."""
    accession_no_dashes = accession.replace("-", "")
    xml_url = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"
    try:
        text, _ctype, _byte_size, _truncated = await client.get_text(xml_url)
    except SecError as exc:
        row["form4"] = None
        row["parse_error"] = f"{type(exc).__name__}: fetch failed"
        return
    try:
        parsed: Form4Data = parse_form4(text.encode("utf-8", errors="replace"), accession_number=accession)
    except Form4ParseError as exc:
        row["form4"] = None
        row["parse_error"] = exc.reason
        return
    row["form4"] = parsed.to_dict()
    row["parse_error"] = None


def _filter_form4(recent: Any, *, cik: str, cutoff_iso: str) -> list[dict[str, Any]]:
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


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-filing parsed payloads into a top-level summary."""
    from decimal import Decimal

    tx_count = 0
    net_buy = Decimal("0")
    net_sell = Decimal("0")
    parse_failures = 0
    for row in rows:
        f4 = row.get("form4")
        if not isinstance(f4, dict):
            if row.get("parse_error"):
                parse_failures += 1
            continue
        tx_count += int(f4.get("transaction_count", 0) or 0)
        net_buy += _to_decimal(f4.get("net_buy_value"))
        net_sell += _to_decimal(f4.get("net_sell_value"))
    return {
        "transaction_count": tx_count,
        "net_buy_value": str(net_buy),
        "net_sell_value": str(net_sell),
        "parse_failures": parse_failures,
    }


def _to_decimal(value: Any) -> Any:
    """Coerce a serialised Decimal (str / int / float) back to Decimal."""
    from decimal import Decimal, InvalidOperation

    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


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
