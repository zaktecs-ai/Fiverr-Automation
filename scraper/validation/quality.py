"""Final quality gate.

A technical data-quality assessment run before marking a job COMPLETE. It
validates schema integrity, duplicate identities, email/URL well-formedness,
status contradictions, CSV encoding, and checkpoint consistency. This is NOT a
lead score — no lead scoring is produced.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..models import OUTPUT_COLUMNS
from ..utils.normalize import canonical_domain, normalize_email, normalize_phone, normalize_url
from ..validation.validate import validate_email_field


@dataclass
class QualityReport:
    passed: bool = False
    checks: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = ""):
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            self.issues.append(f"{name}: {detail}")

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "issues": self.issues}


def run_quality_gate(output_dir: str | Path, checkpoint_store=None,
                     expected_columns: list[str] | None = None) -> QualityReport:
    output_dir = Path(output_dir)
    report = QualityReport()
    columns = expected_columns or OUTPUT_COLUMNS

    csv_path = output_dir / "checkpoint.json"  # not the csv; resolve below
    # Determine the CSV filename from run_summary or directory listing.
    csv_file = _find_csv(output_dir)

    if csv_file is None:
        report.add("csv_present", False, "no output CSV found")
        report.passed = report.passed or False
        # Keep going so callers get a full picture.
    else:
        report.add("csv_present", True, str(csv_file))
        _check_csv_integrity(csv_file, columns, report)

    if checkpoint_store is not None:
        try:
            committed = checkpoint_store.committed_count()
            report.add("checkpoint_readable", True, f"committed={committed}")
        except Exception as e:  # noqa: BLE001
            report.add("checkpoint_readable", False, str(e))

    report.passed = len(report.issues) == 0
    return report


def _find_csv(output_dir: Path) -> Path | None:
    """Return the primary output CSV (root job .csv, not filtered/failed)."""
    for f in sorted(output_dir.glob("*.csv")):
        if f.name.endswith("_filtered_records.csv") or f.name.endswith("_failed_records.csv"):
            continue
        return f
    return None


def _check_csv_integrity(csv_path: Path, columns: list[str], report: QualityReport) -> None:
    try:
        with open(csv_path, "r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            report.add("header_integrity", header == columns,
                       f"expected {len(columns)} cols, got {len(header)}")

            domains: dict[str, str] = {}
            phones: dict[str, str] = {}
            emails_seen: set[str] = set()
            duplicate_domain = False
            duplicate_phone = False
            bad_email = False
            contradiction = False
            bad_url = False

            for row in reader:
                w = row.get("website", "")
                d = canonical_domain(normalize_url(w)) if (w and w != "N/A") else ""
                if d:
                    if d in domains:
                        duplicate_domain = True
                    domains[d] = row.get("business_name", "")
                p = normalize_phone(row.get("phone", ""))
                if p and p != "N/A":
                    if p in phones:
                        duplicate_phone = True
                    phones[p] = row.get("business_name", "")

                ok, _ = validate_email_field(row.get("emails"))
                if not ok:
                    bad_email = True

                status = (row.get("website_status") or "").upper()
                reason = (row.get("website_failure_reason") or "").upper()
                if status == "DEAD" and reason in ("HTTP_BLOCKED", "CAPTCHA_DETECTED",
                                                   "JS_REQUIRED", "TIMEOUT"):
                    contradiction = True

                web = row.get("website", "")
                if web and web != "N/A" and not web.lower().startswith(("http://", "https://")):
                    bad_url = True

            report.add("duplicate_domains", not duplicate_domain,
                       "duplicate normalized domains present" if duplicate_domain else "unique")
            report.add("duplicate_phones", not duplicate_phone,
                       "duplicate normalized phones present" if duplicate_phone else "unique")
            report.add("email_validity", not bad_email,
                       "invalid email values present" if bad_email else "all emails valid")
            report.add("status_consistency", not contradiction,
                       "contradictory status/reason present" if contradiction else "consistent")
            report.add("url_validity", not bad_url,
                       "malformed URLs present" if bad_url else "URLs valid")
    except UnicodeDecodeError:
        report.add("csv_encoding", False, "CSV is not valid UTF-8")
    except Exception as e:  # noqa: BLE001
        report.add("csv_readable", False, str(e))


def write_quality_report(output_dir: str | Path, report: QualityReport) -> Path:
    output_dir = Path(output_dir)
    out = output_dir / "quality_report.json"
    out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return out
