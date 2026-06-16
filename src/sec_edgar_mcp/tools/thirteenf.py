"""``get_13f_holdings`` and ``get_institutional_holders`` implementations.

13F-HR = the quarterly disclosure institutional investment managers with
≥ $100M in Section 13(f) securities must file within 45 days of quarter
end.  The machine-readable *information table* (one ``<infoTable>`` row
per holding) is parsed via :mod:`sec_edgar_mcp._thirteenf` using the
same defused-XML posture as Form 4 (R8) — XXE / billion-laughs / DTD all
refused.

SEC endpoints used:
    * ``GET https://data.sec.gov/submissions/CIK{cik:010d}.json`` — the
      manager's submission index (pick the 13F-HR filing).
    * ``GET https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{accn}-index.json``
      — the filing directory (locate the information-table XML).
    * ``GET https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}.xml``
      — the information-table body.
    * ``GET https://efts.sec.gov/LATEST/search-index`` — full-text search
      over 13F-HR filings for the reverse ``get_institutional_holders``
      lookup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .._thirteenf import ThirteenFData, ThirteenFHolding, parse_13f
from ..cache import Cache
from ..client import DATA_HOST, SEARCH_HOST, WWW_HOST, SecEdgarClient, resolve_cik
from ..errors import SecError, SecNotFoundError, ThirteenFParseError
from ..models import Get13FHoldingsInput, GetInstitutionalHoldersInput
from ._runtime import call_with_cache

log = logging.getLogger(__name__)

#: SEC accepts both 13F-HR and 13F-HR/A (amendment).
_THIRTEENF_KINDS: frozenset[str] = frozenset({"13F-HR", "13F-HR/A"})

#: 2023Q3 is the first period the SEC reports ``value`` in whole dollars
#: (prior filings report thousands).  We compare on the report date.
_DOLLAR_UNIT_CUTOVER_ISO: str = "2023-06-30"

#: Cap on holdings echoed back in the tool payload (full set still parsed).
_MAX_HOLDINGS_RETURNED: int = 1_000

#: EDGAR full-text search page size ceiling.
_MAX_HITS_PER_PAGE: int = 100


# ===========================================================================
# get_13f_holdings
# ===========================================================================


async def get_13f_holdings_impl(args: Get13FHoldingsInput) -> dict[str, Any]:
    """Return a 13F manager's reported holdings for the latest (or named) quarter.

    Output shape::

        {
            "manager": { cik, name },
            "quarter": str | None,          # requested quarter filter
            "report_date": str | None,      # SEC period-of-report (ISO)
            "accession_number": str,
            "form": "13F-HR" | "13F-HR/A",
            "filing_date": str,
            "value_units": "thousands" | "dollars",
            "holding_count": int,
            "total_value_reported": str,    # Decimal as string (raw units)
            "total_value_usd": str,         # normalised to whole dollars
            "total_shares": str,
            "holdings": [ ... ThirteenFHolding ... ],
            "holdings_truncated": bool,
            "warnings": [str, ...],
        }
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        cik = await resolve_cik(client, args.cik_or_ticker)
        url = f"{DATA_HOST}/submissions/CIK{cik}.json"
        data = await client.get_json(url)
        recent = data.get("filings", {}).get("recent", {})
        rows = _filter_13f(recent)
        if not rows:
            raise SecNotFoundError(
                resource=f"13F-HR:{cik}",
                hint=f"no 13F-HR filing found in the recent window for CIK {cik}",
            )
        chosen = _select_quarter(rows, quarter=args.quarter)
        if chosen is None:
            raise SecNotFoundError(
                resource=f"13F-HR:{cik}:{args.quarter}",
                hint=f"no 13F-HR matching quarter {args.quarter} for CIK {cik}",
            )

        cik_int = int(cik)
        accession = chosen["accession_number"]
        report_date = chosen.get("report_date")
        value_in_thousands = _is_thousands(report_date)
        parsed = await _fetch_and_parse_info_table(
            client,
            cik_int=cik_int,
            accession=accession,
            value_in_thousands=value_in_thousands,
        )

        holdings = [_holding_to_dict(h) for h in parsed.holdings]
        truncated = len(holdings) > _MAX_HOLDINGS_RETURNED
        if truncated:
            holdings = holdings[:_MAX_HOLDINGS_RETURNED]

        total_value_usd = _normalise_value(parsed.total_value, value_in_thousands)
        return {
            "manager": {"cik": cik, "name": data.get("name")},
            "quarter": args.quarter,
            "report_date": report_date,
            "accession_number": accession,
            "form": chosen.get("form"),
            "filing_date": chosen.get("filing_date"),
            "value_units": "thousands" if value_in_thousands else "dollars",
            "holding_count": parsed.holding_count,
            "total_value_reported": str(parsed.total_value),
            "total_value_usd": total_value_usd,
            "total_shares": str(parsed.total_shares),
            "holdings": holdings,
            "holdings_truncated": truncated,
            "warnings": list(parsed.raw_warnings),
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_filings_index(_cache_params_13f(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cik = raw.get("manager", {}).get("cik")
        cache.put_filings_index(
            _cache_params_13f(args),
            raw,
            cik=cik if isinstance(cik, str) else None,
        )

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


async def _fetch_and_parse_info_table(
    client: SecEdgarClient,
    *,
    cik_int: int,
    accession: str,
    value_in_thousands: bool,
) -> ThirteenFData:
    """Locate + fetch + parse the information-table XML for *accession*."""
    accession_no_dashes = accession.replace("-", "")
    base = f"{WWW_HOST}/Archives/edgar/data/{cik_int}/{accession_no_dashes}"
    index = await client.get_json(f"{base}/{accession}-index.json")
    doc = _select_info_table_doc(index)
    if doc is None:
        raise ThirteenFParseError(
            accession_number=accession,
            reason="no information-table XML found in filing index",
        )
    text, _ctype, _size, _trunc = await client.get_text(f"{base}/{doc}")
    return parse_13f(
        text.encode("utf-8", errors="replace"),
        accession_number=accession,
        value_in_thousands=value_in_thousands,
    )


def _select_info_table_doc(index: dict[str, Any]) -> str | None:
    """Pick the 13F information-table XML out of an SEC index.json.

    The information table is the XML whose name contains ``form13f`` /
    ``infotable`` / ``information_table`` (case-insensitive); we exclude
    the ``primary_doc.xml`` cover page.  Falls back to the first ``.xml``
    that is not the cover page.
    """
    items = index.get("directory", {}).get("item", [])
    if not isinstance(items, list):
        return None
    candidates: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if not name.lower().endswith(".xml"):
            continue
        if name.lower() == "primary_doc.xml":
            continue
        candidates.append(name)
    for name in candidates:
        lowered = name.lower()
        if any(tag in lowered for tag in ("form13f", "infotable", "information_table", "informationtable")):
            return name
    return candidates[0] if candidates else None


def _filter_13f(recent: Any) -> list[dict[str, Any]]:
    if not isinstance(recent, dict):
        return []
    accession = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    out: list[dict[str, Any]] = []
    for i, acc in enumerate(accession):
        if not isinstance(acc, str):
            continue
        form = _safe_get(forms, i)
        if form not in _THIRTEENF_KINDS:
            continue
        out.append(
            {
                "accession_number": acc,
                "form": form,
                "filing_date": _safe_get(dates, i),
                "report_date": _safe_get(report_dates, i) or None,
            }
        )
    return out


def _select_quarter(rows: list[dict[str, Any]], *, quarter: str | None) -> dict[str, Any] | None:
    """Return the row matching *quarter* (``YYYYQN``) or the most-recent."""
    if quarter is None:
        return _most_recent(rows)
    target = _quarter_to_report_date(quarter)
    for row in rows:
        if row.get("report_date") == target:
            return row
    # Tolerate filings whose report_date falls in the requested quarter
    # range even if it is not the canonical quarter-end day.
    year = int(quarter[:4])
    q = int(quarter[5])
    start, end = _quarter_bounds(year, q)
    for row in rows:
        rd = row.get("report_date")
        if isinstance(rd, str) and start <= rd <= end:
            return row
    return None


def _most_recent(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in rows:
        if best is None:
            best = row
            continue
        if str(row.get("filing_date") or "") > str(best.get("filing_date") or ""):
            best = row
    return best


def _quarter_to_report_date(quarter: str) -> str:
    year = int(quarter[:4])
    q = int(quarter[5])
    return _quarter_bounds(year, q)[1]


def _quarter_bounds(year: int, q: int) -> tuple[str, str]:
    ends = {1: ("01-01", "03-31"), 2: ("04-01", "06-30"), 3: ("07-01", "09-30"), 4: ("10-01", "12-31")}
    start_md, end_md = ends[q]
    return f"{year}-{start_md}", f"{year}-{end_md}"


def _is_thousands(report_date: Any) -> bool:
    """Pre-2023Q3 13F filings report ``value`` in thousands of dollars."""
    if not isinstance(report_date, str) or not report_date:
        return False  # assume modern whole-dollar units when unknown
    return report_date <= _DOLLAR_UNIT_CUTOVER_ISO


def _normalise_value(total_value: Any, value_in_thousands: bool) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        v = Decimal(str(total_value))
    except (InvalidOperation, ValueError, TypeError):  # pragma: no cover - parser pre-validates
        return "0"
    if value_in_thousands:
        v = v * Decimal("1000")
    return str(v)


def _holding_to_dict(holding: ThirteenFHolding) -> dict[str, Any]:
    return holding.to_dict()


def _cache_params_13f(args: Get13FHoldingsInput) -> dict[str, Any]:
    return {
        "tool": "get_13f_holdings",
        "cik_or_ticker": args.cik_or_ticker,
        "quarter": args.quarter,
    }


# ===========================================================================
# get_institutional_holders
# ===========================================================================


async def get_institutional_holders_impl(args: GetInstitutionalHoldersInput) -> dict[str, Any]:
    """Reverse-lookup which 13F managers report a position in *ticker*.

    SEC offers no native "who holds X" API, so we full-text-search recent
    13F-HR filings for the ticker / company name and aggregate the distinct
    filers.  The result is an approximation bounded by the search window —
    a manager whose latest 13F predates ``since_days`` will not appear.

    Output shape::

        {
            "ticker": str,
            "company": str | None,
            "since_days": int,
            "holder_count": int,
            "holders": [
                { "cik": str, "name": str, "filings": int,
                  "latest_filing_date": str, "latest_accession": str },
                ...
            ],
            "search_total_hits": int,
            "warnings": [str, ...],
        }
    """

    async def fetch(client: SecEdgarClient) -> dict[str, Any]:
        company_name: str | None = None
        # Resolve the held company's display name (improves recall) but
        # tolerate failure — the ticker alone is a valid full-text query.
        try:
            cik = await resolve_cik(client, args.ticker)
            sub = await client.get_json(f"{DATA_HOST}/submissions/CIK{cik}.json")
            name = sub.get("name")
            company_name = name if isinstance(name, str) else None
        except SecError:
            company_name = None

        end = datetime.now(tz=UTC).date()
        start = end - timedelta(days=args.since_days)
        params: dict[str, Any] = {
            "q": f'"{args.ticker}"',
            "forms": "13F-HR",
            "dateRange": "custom",
            "startdt": start.isoformat(),
            "enddt": end.isoformat(),
            "from": 0,
            "size": _MAX_HITS_PER_PAGE,
        }
        raw = await client.get_json(f"{SEARCH_HOST}/LATEST/search-index", params=params)
        holders, total_hits = _aggregate_holders(raw, limit=args.limit)
        warnings: list[str] = []
        if not holders:
            warnings.append("no_13f_holders_found_in_window")
        return {
            "ticker": args.ticker,
            "company": company_name,
            "since_days": args.since_days,
            "holder_count": len(holders),
            "holders": holders,
            "search_total_hits": total_hits,
            "warnings": warnings,
        }

    def _lookup(cache: Cache) -> dict[str, Any] | None:
        return cache.get_search(_cache_params_holders(args))

    def _store(cache: Cache, raw: dict[str, Any]) -> None:
        cache.put_search(_cache_params_holders(args), raw)

    return await call_with_cache(fetch, cache_lookup=_lookup, cache_store=_store)


def _aggregate_holders(raw: dict[str, Any], *, limit: int) -> tuple[list[dict[str, Any]], int]:
    hits_block = raw.get("hits", {})
    total_block = hits_block.get("total", {}) if isinstance(hits_block, dict) else {}
    total = total_block.get("value", 0) if isinstance(total_block, dict) else total_block
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = 0

    inner = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
    by_cik: dict[str, dict[str, Any]] = {}
    if isinstance(inner, list):
        for h in inner:
            if not isinstance(h, dict):
                continue
            src = h.get("_source", {})
            src = src if isinstance(src, dict) else {}
            cik = _first(src.get("ciks"))
            if not isinstance(cik, str) or not cik:
                continue
            name = _first(src.get("display_names")) or ""
            filing_date = src.get("file_date") or ""
            accession = src.get("adsh") or h.get("_id") or ""
            entry = by_cik.get(cik)
            if entry is None:
                by_cik[cik] = {
                    "cik": cik,
                    "name": name if isinstance(name, str) else str(name),
                    "filings": 1,
                    "latest_filing_date": filing_date,
                    "latest_accession": accession,
                }
            else:
                entry["filings"] += 1
                if str(filing_date) > str(entry["latest_filing_date"]):
                    entry["latest_filing_date"] = filing_date
                    entry["latest_accession"] = accession

    holders = sorted(
        by_cik.values(),
        key=lambda e: (str(e["latest_filing_date"]), e["filings"]),
        reverse=True,
    )
    return holders[:limit], total_int


def _cache_params_holders(args: GetInstitutionalHoldersInput) -> dict[str, Any]:
    return {
        "tool": "get_institutional_holders",
        "ticker": args.ticker,
        "since_days": args.since_days,
        "limit": args.limit,
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def _safe_get(seq: Any, idx: int) -> Any:
    if isinstance(seq, list) and 0 <= idx < len(seq):
        return seq[idx]
    return None


__all__ = [
    "get_13f_holdings_impl",
    "get_institutional_holders_impl",
]
