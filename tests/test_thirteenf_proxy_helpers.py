"""Branch-completion tests for 13F + proxy tool helpers.

These exercise the defensive helper branches in
:mod:`sec_edgar_mcp.tools.thirteenf` and :mod:`sec_edgar_mcp.tools.proxy`
directly (malformed SEC payloads, quarter-range fallback, aggregation
ordering) so the suite reaches 100% line + branch coverage without
relying on incidental fixture shapes.
"""

from __future__ import annotations

from sec_edgar_mcp.tools import proxy as proxy_mod
from sec_edgar_mcp.tools import thirteenf as t13

# ===========================================================================
# _filter_13f / _select_quarter / _most_recent
# ===========================================================================


def test_filter_13f_non_dict_recent() -> None:
    assert t13._filter_13f("not a dict") == []


def test_filter_13f_skips_non_str_accession_and_non_13f_form() -> None:
    recent = {
        "accessionNumber": [123, "0001067983-24-000020", "0001067983-24-000030"],
        "form": ["13F-HR", "10-K", "13F-HR"],
        "filingDate": ["2024-01-01", "2024-02-01", "2024-03-01"],
        "reportDate": ["2023-12-31", "", "2024-02-29"],
    }
    rows = t13._filter_13f(recent)
    # idx0 dropped (non-str accession), idx1 dropped (10-K), idx2 kept.
    assert len(rows) == 1
    assert rows[0]["accession_number"] == "0001067983-24-000030"


def test_select_quarter_range_fallback() -> None:
    rows = [{"report_date": "2024-08-31"}]  # not a canonical quarter-end
    chosen = t13._select_quarter(rows, quarter="2024Q3")
    assert chosen is not None


def test_select_quarter_no_match_returns_none() -> None:
    rows = [{"report_date": "2024-03-31"}]
    assert t13._select_quarter(rows, quarter="2024Q4") is None


def test_most_recent_picks_latest_filing_date() -> None:
    rows = [
        {"accession_number": "a", "filing_date": "2024-01-01"},
        {"accession_number": "b", "filing_date": "2024-09-01"},
        {"accession_number": "c", "filing_date": None},
    ]
    assert t13._most_recent(rows)["accession_number"] == "b"


def test_most_recent_empty() -> None:
    assert t13._most_recent([]) is None


def test_is_thousands_branches() -> None:
    assert t13._is_thousands("2020-12-31") is True
    assert t13._is_thousands("2024-06-30") is False
    assert t13._is_thousands(None) is False
    assert t13._is_thousands("") is False


def test_normalise_value_invalid() -> None:
    # An unparseable value degrades to "0" rather than raising.
    assert t13._normalise_value("not-a-number", False) == "0"


def test_safe_get_out_of_range_and_non_list() -> None:
    assert t13._safe_get([1, 2], 5) is None
    assert t13._safe_get("nope", 0) is None
    assert t13._safe_get([1, 2], 1) == 2


def test_select_info_table_non_list_items() -> None:
    assert t13._select_info_table_doc({"directory": {"item": "bad"}}) is None


def test_select_info_table_skips_non_dict_and_non_xml() -> None:
    index = {
        "directory": {
            "item": [
                "bad-entry",
                {"name": "cover.txt"},
                {"name": "primary_doc.xml"},
                {"name": "form13fInfoTable.xml"},
            ]
        }
    }
    assert t13._select_info_table_doc(index) == "form13fInfoTable.xml"


def test_select_info_table_no_candidates() -> None:
    index = {"directory": {"item": [{"name": "primary_doc.xml"}]}}
    assert t13._select_info_table_doc(index) is None


# ===========================================================================
# _aggregate_holders
# ===========================================================================


def test_aggregate_holders_non_dict_hits() -> None:
    holders, total = t13._aggregate_holders({"hits": "bad"}, limit=10)
    assert holders == []
    assert total == 0


def test_aggregate_holders_inner_hits_not_list() -> None:
    """hits.hits being a non-list skips iteration entirely (385->412 branch)."""
    holders, total = t13._aggregate_holders({"hits": {"total": {"value": 5}, "hits": "nope"}}, limit=10)
    assert holders == []
    assert total == 5


def test_aggregate_holders_later_filing_updates_latest() -> None:
    """The newer of two filings for the same CIK updates latest_accession."""
    raw = {
        "hits": {
            "total": {"value": 2},
            "hits": [
                {"_source": {"adsh": "old", "ciks": ["1"], "file_date": "2024-01-01", "display_names": ["X"]}},
                {"_source": {"adsh": "new", "ciks": ["1"], "file_date": "2024-09-01", "display_names": ["X"]}},
            ],
        }
    }
    holders, total = t13._aggregate_holders(raw, limit=10)
    assert total == 2
    assert len(holders) == 1
    assert holders[0]["filings"] == 2
    assert holders[0]["latest_accession"] == "new"
    assert holders[0]["latest_filing_date"] == "2024-09-01"


def test_first_helper_non_list() -> None:
    assert t13._first("scalar") == "scalar"
    assert t13._first([7, 8]) == 7


# ===========================================================================
# proxy _select_proxy / _date_key / _first / _safe_get
# ===========================================================================


def test_proxy_select_non_dict_recent() -> None:
    assert proxy_mod._select_proxy("bad") is None


def test_proxy_select_no_proxy_forms() -> None:
    recent = {
        "accessionNumber": ["0000320193-26-000004"],
        "form": ["10-K"],
        "filingDate": ["2026-01-10"],
        "primaryDocument": ["x.htm"],
    }
    assert proxy_mod._select_proxy(recent) is None


def test_proxy_select_skips_non_str_accession() -> None:
    recent = {
        "accessionNumber": [999, "0000320193-26-000004"],
        "form": ["DEF 14A", "DEF 14A"],
        "filingDate": ["2026-01-01", "2026-01-10"],
        "primaryDocument": ["a.htm", "b.htm"],
    }
    chosen = proxy_mod._select_proxy(recent)
    assert chosen is not None
    assert chosen["accession_number"] == "0000320193-26-000004"


def test_proxy_date_key_non_str() -> None:
    assert proxy_mod._date_key(None) == ""
    assert proxy_mod._date_key("2026-01-01") == "2026-01-01"


def test_proxy_first_and_safe_get() -> None:
    assert proxy_mod._first([]) is None
    assert proxy_mod._first(["a"]) == "a"
    assert proxy_mod._safe_get([1], 9) is None
    assert proxy_mod._safe_get("x", 0) is None
