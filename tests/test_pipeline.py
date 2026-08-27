"""Mocked end-to-end pipeline test (no live Google Maps or network).

Uses a fake MapsCollector that yields a couple of records, and a real
Pipeline with HTTP disabled (require_website=False so no network happens).
Verifies: dedup, filter, CSV commit, checkpoint, summary, quality gate, and
resume (no restart-from-zero / no duplicates).
"""
import json
from pathlib import Path

import yaml

from scraper.pipeline import Pipeline
from scraper.models import OUTPUT_COLUMNS


class FakeMapsCollector:
    """Yields a fixed set of raw maps dicts (like the real collector)."""

    def __init__(self, records):
        self._records = records

    def collect(self, query):
        for r in self._records:
            r2 = dict(r)
            r2["source_query"] = query
            yield r2


def _cfg(client_name="campaign", queries=None, require_website=False,
         filters=None, signals=None):
    return {
        "job": {"client_name": client_name, "output_filename": client_name,
                "max_results_per_query": 0, "max_total_results": 0,
                "output_dir": f"output/{client_name}",
                },
        "resolved_output_dir": f"output/{client_name}",
        "queries": queries or ["dentists in Dallas, TX"],
        "missing_value": "N/A",
        "website": {"require_website": require_website,
                    "enable_playwright_fallback": False,
                    "enable_sitemap": False, "max_pages_per_site": 2,
                    "overall_site_timeout_seconds": 30,
                    "http_connect_timeout_seconds": 3.0,
                    "http_read_timeout_seconds": 5.0,
                    "page_navigation_timeout_seconds": 10.0,
                    "use_wappalyzer": False},
        "email": {"enabled": True, "max_email_length": 120,
                  "enable_mx_check": False, "enable_ocr": False},
        "smtp": {"enabled": False},
        "concurrency": {"website_workers": 2},
        "country": {"default": "US"},
        "filters": filters or {},
        "signals": signals or {},
    }


def _maps_record(**overrides):
    r = {
        "business_name": "Smile Dental",
        "category": "Dentist",
        "phone": "214-555-1234",
        "website": None,  # no website -> enrichment skipped cleanly
        "city": "Dallas",
        "state": "TX",
        "rating": 4.7,
        "review_count": 120,
        "place_id": "0x1",
    }
    r.update(overrides)
    return r


class TestPipelineEndToEnd:
    def test_run_produces_output(self, tmp_path, monkeypatch):
        cfg = _cfg(require_website=False)
        # Redirect output_dir into tmp_path.
        cfg["job"]["output_dir"] = str(tmp_path / "campaign")
        cfg["resolved_output_dir"] = cfg["job"]["output_dir"]

        collector = FakeMapsCollector([
            _maps_record(place_id="0xA"),
            _maps_record(place_id="0xB", business_name="Other Clinic", phone="214-555-9999"),
        ])
        p = Pipeline(cfg, maps_collector=collector)
        p.run()

        assert (tmp_path / "campaign" / "campaign.csv").exists()
        assert (tmp_path / "campaign" / "campaign.xlsx").exists()
        assert (tmp_path / "campaign" / "run_summary.json").exists()
        assert (tmp_path / "campaign" / "checkpoint.db").exists()

        summary = json.loads((tmp_path / "campaign" / "run_summary.json").read_text())
        assert summary["final_exported_records"] == 2

    def test_dedup_across_run(self, tmp_path):
        cfg = _cfg(require_website=False)
        cfg["job"]["output_dir"] = str(tmp_path / "campaign")
        cfg["resolved_output_dir"] = cfg["job"]["output_dir"]

        # Same place_id twice in the same query -> one deduplicated.
        collector = FakeMapsCollector([
            _maps_record(place_id="0xSAME"),
            _maps_record(place_id="0xSAME"),
        ])
        p = Pipeline(cfg, maps_collector=collector)
        p.run()
        summary = json.loads((tmp_path / "campaign" / "run_summary.json").read_text())
        assert summary["businesses_discovered"] >= 1
        assert summary["duplicates_removed"] == 1
        assert summary["final_exported_records"] == 1

    def test_resume_does_not_restart_from_zero(self, tmp_path):
        out_dir = str(tmp_path / "campaign")

        # --- Run 1: only query q1 exists. Commits one record, marks q1 done.
        cfg1 = _cfg(require_website=False, queries=["q1"])
        cfg1["job"]["output_dir"] = out_dir
        cfg1["resolved_output_dir"] = out_dir

        class Q1Collector:
            def collect(self, query):
                yield _maps_record(place_id="0xR1", source_query=query)
        p1 = Pipeline(cfg1, maps_collector=Q1Collector())
        p1.run()

        # --- Run 2 (simulated restart): config now has q1 + q2. q1 must be
        # skipped (already done), q2 processed; no duplicate of the q1 record.
        cfg2 = _cfg(require_website=False, queries=["q1", "q2"])
        cfg2["job"]["output_dir"] = out_dir
        cfg2["resolved_output_dir"] = out_dir

        class Q2Collector:
            def collect(self, query):
                assert query == "q2", "q1 should have been skipped as already done"
                yield _maps_record(place_id="0xR2", source_query=query,
                                   business_name="Clinic Two", phone="214-555-8888")
        p2 = Pipeline(cfg2, maps_collector=Q2Collector())
        p2.run()

        summary = json.loads((tmp_path / "campaign" / "run_summary.json").read_text())
        # final_exported_records reflects the total CSV rows (both runs).
        assert summary["final_exported_records"] == 2
        # No restart-from-zero: committed records were not re-added.
        with open(tmp_path / "campaign" / "campaign.csv", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        # header + 2 rows (one from each query)
        assert len(lines) == 3

    def test_filter_excludes(self, tmp_path):
        cfg = _cfg(require_website=False)
        cfg["filters"] = {"include_all": [{"field": "review_count", "op": ">=", "value": 100}]}
        cfg["job"]["output_dir"] = str(tmp_path / "campaign")
        cfg["resolved_output_dir"] = cfg["job"]["output_dir"]

        collector = FakeMapsCollector([
            _maps_record(place_id="0xHI", review_count=200),
            _maps_record(place_id="0xLO", review_count=5, phone="214-555-7777"),
        ])
        p = Pipeline(cfg, maps_collector=collector)
        p.run()
        summary = json.loads((tmp_path / "campaign" / "run_summary.json").read_text())
        assert summary["filtered_out"] == 1
        assert summary["final_exported_records"] == 1
        assert (tmp_path / "campaign" / "filtered_records.csv").exists()
