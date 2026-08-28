"""Regression tests for the 2026-08-29 reliability/hardening audit.

Each test locks in one specific fix so future refactors don't silently
reintroduce the bug. See Fiverr_Automation_Hardening_Report.md.

Fix #1  : pre-filter rejection rolls back dedup identity (HIGH silent-loss).
Fix #2  : canonical_domain must not mangle IP-address hosts.
Fix #3  : normalize_url must not crash on a bare-IPv6 host.
Fix #8  : _to_cell must not emit "nan"/"inf" for non-finite floats.
Fix #4/5: dead config knobs (google_maps_workers, enable_ocr) removed.
"""
import math

import pytest

from scraper.dedup import IdentityResolver, resolve_identity
from scraper.models import _to_cell
from scraper.pipeline import Pipeline
from scraper.utils.normalize import canonical_domain, normalize_url


# --- #1 pre-filter rejection must roll back dedup identity -------------------
class _PreFilterCollector:
    """A Maps collector yielding a single record (source_query applied)."""

    def __init__(self, records):
        self._records = records

    def collect(self, query):
        for r in self._records:
            r2 = dict(r)
            r2["source_query"] = query
            yield r2


def _hardening_cfg(tmp_path, filters=None):
    return {
        "job": {"client_name": "c", "output_filename": "c",
                "max_results_per_query": 0, "max_total_results": 0,
                "output_dir": str(tmp_path / "out")},
        "resolved_output_dir": str(tmp_path / "out"),
        "queries": ["dentists in Dallas, TX"],
        "missing_value": "N/A",
        "website": {"require_website": False, "enable_playwright_fallback": False,
                    "enable_sitemap": False, "max_pages_per_site": 2,
                    "overall_site_timeout_seconds": 30,
                    "http_connect_timeout_seconds": 3.0,
                    "http_read_timeout_seconds": 5.0,
                    "page_navigation_timeout_seconds": 10.0,
                    "use_wappalyzer": False},
        "email": {"enabled": True, "max_email_length": 120, "enable_mx_check": False},
        "smtp": {"enabled": False},
        "concurrency": {"website_workers": 2},
        "country": {"default": "US"},
        "filters": filters or {},
        "signals": {},
    }


class TestPreFilterRollback:
    def test_resolver_release_on_rollback(self):
        """A rejected record must be re-discoverable (rollback releases identity)."""
        r = IdentityResolver()
        rec = {"business_name": "Smile Dental", "place_id": "0x1",
               "city": "Dallas", "website": "https://smile.example",
               "phone": "2145551234"}
        is_dup, _, _ = r.is_duplicate(rec)
        assert is_dup is False          # registered as seen
        # Pre-filter rejects -> pipeline rolls back.
        r.rollback(rec)
        is_dup2, reason, _ = r.is_duplicate(rec)
        assert is_dup2 is False, f"still seen as duplicate after rollback: {reason}"

    def test_prefilter_rejection_rolls_back_dedup(self, tmp_path):
        """The pre-filter rejection branch must actually call rollback.

        A strict pre-filter rejects the record; re-running the same query must
        rediscover the business (not silently drop it as a duplicate)."""
        # review_count >= 1000 rejects the record (it has 120 reviews).
        cfg = _hardening_cfg(tmp_path, filters={
            "include_all": [{"field": "review_count", "op": ">=", "value": 1000}],
        })
        rec = {"business_name": "Smile Dental", "category": "Dentist",
               "phone": "214-555-1234", "website": None, "city": "Dallas",
               "state": "TX", "rating": 4.7, "review_count": 120, "place_id": "0x1"}
        p = Pipeline(cfg, maps_collector=_PreFilterCollector([rec]))
        p._process_query("dentists in Dallas, TX")
        # The record was rejected by the pre-filter...
        assert p.summary.stats["filtered_out"] >= 1
        assert p.summary.stats["final_exported_records"] == 0
        # ...so a later re-discovery of the same business must NOT be a dup.
        is_dup, reason, _ = p.resolver.is_duplicate(rec)
        assert is_dup is False, f"rejected record leaked into seen sets: {reason}"


# --- #2 canonical_domain must not mangle IP hosts -----------------------------
class TestCanonicalDomainIPGuard:
    @pytest.mark.parametrize("host,want", [
        ("1.1.1.1", "1.1.1.1"),
        ("10.0.0.1", "10.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.1.1", "192.168.1.1"),
        ("::1", "::1"),
    ])
    def test_ip_host_is_identity(self, host, want):
        assert canonical_domain(host) == want

    def test_hostname_still_reduced(self):
        assert canonical_domain("www.example.com") == "example.com"
        assert canonical_domain("sub.example.co.uk") == "example.co.uk"


# --- #3 normalize_url must not crash on bare IPv6 -----------------------------
class TestNormalizeBareIPv6:
    def test_bare_ipv6_does_not_raise(self):
        # Previously raised ValueError: Port could not be cast...
        out = normalize_url("http://2606:4700::6810")
        assert out == "http://[2606:4700::6810]"

    def test_bracketed_ipv6_preserved(self):
        assert normalize_url("http://[2606:4700::6810:84e5]") == \
            "http://[2606:4700::6810:84e5]"

    def test_bare_ipv6_with_path(self):
        out = normalize_url("http://2606:4700::6810/contact")
        assert out == "http://[2606:4700::6810]/contact"


# --- #8 _to_cell must not emit nan/inf ----------------------------------------
class TestToCellNonFinite:
    def test_nan_is_missing(self):
        assert _to_cell(float("nan"), "N/A") == "N/A"

    def test_inf_is_missing(self):
        assert _to_cell(float("inf"), "N/A") == "N/A"
        assert _to_cell(float("-inf"), "N/A") == "N/A"

    def test_finite_float_preserved(self):
        assert _to_cell(4.7, "N/A") == "4.7"