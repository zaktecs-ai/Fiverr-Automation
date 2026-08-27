"""Tests for the quality gate."""
import csv

from scraper.models import OUTPUT_COLUMNS
from scraper.validation.quality import run_quality_gate


def _write_csv(tmp_path, rows, header=None):
    p = tmp_path / "test.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header or OUTPUT_COLUMNS)
        w.writerows(rows)
    return p


def _row(**overrides):
    r = {c: "N/A" for c in OUTPUT_COLUMNS}
    r.update({"business_name": "X", "website": "https://x.com",
              "emails": "a@x.com", "phone": "2145551234"})
    r.update(overrides)
    return [r.get(c, "N/A") for c in OUTPUT_COLUMNS]


class TestQualityGate:
    def test_passes_clean_data(self, tmp_path):
        _write_csv(tmp_path, [_row()])
        report = run_quality_gate(tmp_path)
        assert report.passed is True, report.issues

    def test_flags_duplicate_phone(self, tmp_path):
        r = _write_csv(tmp_path, [_row(), _row(website="https://y.com")])
        report = run_quality_gate(tmp_path)
        # Same phone in two different records => duplicate phone flag.
        assert any("phone" in i for i in report.issues)

    def test_flags_contradiction(self, tmp_path):
        _write_csv(tmp_path, [_row(website_status="DEAD",
                                   website_failure_reason="HTTP_BLOCKED")])
        report = run_quality_gate(tmp_path)
        assert any("contradict" in i or "status" in i for i in report.issues)

    def test_flags_bad_email(self, tmp_path):
        _write_csv(tmp_path, [_row(emails="not-an-email")])
        report = run_quality_gate(tmp_path)
        assert any("email" in i for i in report.issues)

    def test_no_csv_means_not_passed(self, tmp_path):
        report = run_quality_gate(tmp_path)
        assert report.passed is False
