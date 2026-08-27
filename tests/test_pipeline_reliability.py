"""Reliability-focused pipeline tests (zero-listings fail-closed + post-filter)."""
from scraper.pipeline import Pipeline


class _ZeroCollector:
    """A Maps collector that yields nothing (simulating selector drift)."""
    def __init__(self, raise_on_collect=False):
        self.raise_on_collect = raise_on_collect

    def collect(self, query):
        if self.raise_on_collect:
            from scraper.maps.collector import ZeroListingsError
            raise ZeroListingsError(query)
        if False:
            yield  # pragma: no cover


def _cfg(tmp_path, filters=None):
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


class TestZeroListingsFailClosed:
    def test_zero_listings_marks_query_failed_not_done(self, tmp_path):
        cfg = _cfg(tmp_path)
        collector = _ZeroCollector(raise_on_collect=True)
        p = Pipeline(cfg, maps_collector=collector)
        # Drive the query directly (store stays open for assertions).
        p._process_query("dentists in Dallas, TX")
        # Query should NOT be marked done; it should be 'failed' so it retries.
        assert p.store.query_status("dentists in Dallas, TX") == "failed"
        assert p.summary.stats["queries_failed"] == 1
        assert p.summary.stats["completed_queries"] == 0


class TestGA4GTMExport:
    def test_ga4_gtm_in_output_columns(self):
        from scraper.models import OUTPUT_COLUMNS
        assert "ga4" in OUTPUT_COLUMNS
        assert "gtm" in OUTPUT_COLUMNS
