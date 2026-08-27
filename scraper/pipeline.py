"""The job pipeline: sequential query processing with resumable state.

Owns the top-level orchestration and every modular stage in order:

    config → query → maps collect → normalize → dedup → filter →
    website enrich → email/MX/SMTP → validate → CSV commit → checkpoint →
    (after all queries) quality gate → XLSX → summary → logs

It is intentionally sequential per query (per spec) while using bounded
thread pools for the *enrichment* of records within a query (website workers).
Every record commit updates durable state so a crash resumes at the right place.
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .checkpoint import CheckpointStore
from .dedup import IdentityResolver, resolve_identity
from .email import MXChecker, SMTPVerifier
from .export import AtomicCSVWriter, RunSummary, write_xlsx
from .filters import FilterEngine, require_website_filter
from .models import OUTPUT_COLUMNS, BusinessRecord, WebsiteStatus
from .utils.normalize import (
    canonical_domain, normalize_email, normalize_phone, normalize_text, normalize_url,
)
from .validation import run_quality_gate, validate_record, write_quality_report
from .websites.enricher import WebsiteEnricher

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg: dict, maps_collector=None, browser_manager=None):
        self.cfg = cfg
        self.maps = maps_collector
        self._bm = browser_manager

        job = cfg["job"]
        self.output_dir = Path(job["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.missing = cfg.get("missing_value", "N/A")

        checkpoint_path = self.output_dir / "checkpoint.db"
        self.store = CheckpointStore(checkpoint_path)

        # Outputs
        base = job["output_filename"]
        self.csv_path = self.output_dir / f"{base}.csv"
        self.xlsx_path = self.output_dir / f"{base}.xlsx"
        self.filtered_path = self.output_dir / "filtered_records.csv"
        self.failed_path = self.output_dir / "failed_records.csv"
        self.summary_path = self.output_dir / "run_summary.json"

        self.csv = AtomicCSVWriter(self.csv_path, OUTPUT_COLUMNS)

        # Dedup seeded from checkpoint.
        self.resolver = IdentityResolver(
            seen_identities=self.store._identities,
            seen_domains=self.store._domains,
            seen_phones=self.store._phones,
            default_country=cfg.get("country", {}).get("default", "US"),
        )

        # Filters (maps + website-inclusion).
        base_filters = FilterEngine(cfg.get("filters"))
        self.filters = base_filters
        self.require_web = require_website_filter(
            cfg.get("website", {}).get("require_website", False))

        # Enrichment workers.
        website_cfg = cfg.get("website", {})
        self.website_workers = cfg.get("concurrency", {}).get("website_workers", 4)
        self.enricher = WebsiteEnricher(cfg, browser_manager=self._bm)

        # Email verification (optional).
        email_cfg = cfg.get("email", {})
        self.mx = MXChecker(enabled=email_cfg.get("enable_mx_check", False))
        self.smtp = SMTPVerifier(enabled=cfg.get("smtp", {}).get("enabled", False))

        self.summary = RunSummary()
        self._query_keys: dict[str, str] = {}
        self._lock = threading.Lock()

        # Counters seed: record IDs are incremental via checkpoint.
        self._counter_offset = self.store.committed_count()

    # ------------------------------------------------------------------
    def run(self) -> None:
        queries = list(self.cfg["queries"])
        self.summary.set("total_queries", len(queries))
        remaining = self.store.remaining_queries(queries)
        self.summary.set("remaining_queries", len(remaining))
        # Recovered records: those already committed.
        self.summary.set("recovered_records", self.store.committed_count())

        log.info("job start: %d queries, %d already done, %d committed records",
                 len(queries), len(queries) - len(remaining), self.store.committed_count())

        for query in queries:
            if self.store.query_status(query) == "done":
                log.checkpoint("query already completed, skipping: %s", query)
                continue
            self._process_query(query)

        # Finalize.
        self.csv.close()
        self._finalize()

    # ------------------------------------------------------------------
    def _process_query(self, query: str) -> None:
        log.info("processing query: %s", query)
        self.store.set_query_status(query, "running")

        records = []
        for raw in self.maps.collect(query):
            self.summary.bump("businesses_discovered")
            rec = self._normalize_maps(raw)
            # Early dedup.
            is_dup, reason, sig = self.resolver.is_duplicate(rec.data)
            if is_dup:
                self.summary.bump("duplicates_removed")
                log.info("duplicate removed (%s): %s", reason, rec.data.get("business_name"))
                continue

            rec_id = str(uuid.uuid4())
            rec.set("record_id", rec_id)
            self.store.register_record(
                rec_id, sig.get("identity_key", ""), sig.get("place_id") or "",
                sig.get("canonical_domain") or "", sig.get("normalized_phone") or "",
                query, self._json_dump(rec.data))

            # Early filter (before expensive enrichment).
            ok, freason = self.filters.evaluate(rec.data)
            if not ok or not self.require_web_ok(rec.data):
                if not ok:
                    reason_text = freason or "filtered"
                else:
                    reason_text = "website_missing"
                rec.set("filtered_out_reason", reason_text)
                self.store.set_stage(rec_id, "filtered")
                self._append_row(self.filtered_path, rec.data)
                self.summary.bump("filtered_out")
                continue

            self.store.set_stage(rec_id, "accepted")
            records.append(rec)

        # Enrich accepted records (bounded website workers).
        self._enrich_records(records)

        # Mark query done.
        self.store.set_query_status(query, "done")
        self.summary.bump("completed_queries")
        self.store.write_json_mirror()

    def require_web_ok(self, data: dict) -> bool:
        ok, reason = self.require_web.evaluate(data)
        if not ok:
            # reason will be 'failed_include_all' — treat as website_missing.
            return False
        return True

    # ------------------------------------------------------------------
    def _normalize_maps(self, raw: dict) -> BusinessRecord:
        rec = BusinessRecord()
        d = rec.data
        d["business_name"] = normalize_text(raw.get("business_name"))
        d["category"] = normalize_text(raw.get("category"))
        d["subcategory"] = normalize_text(raw.get("subcategory"))
        d["phone"] = normalize_phone(raw.get("phone")) if raw.get("phone") else "N/A"
        d["website"] = normalize_url(raw.get("website")) if raw.get("website") else "N/A"
        d["address"] = normalize_text(raw.get("address", raw.get("full_address")))
        d["full_address"] = normalize_text(raw.get("full_address"))
        d["city"] = normalize_text(raw.get("city"))
        d["state"] = normalize_text(raw.get("state"))
        d["postal_code"] = normalize_text(raw.get("postal_code"))
        d["country"] = normalize_text(raw.get("country"))
        d["latitude"] = raw.get("latitude") or "N/A"
        d["longitude"] = raw.get("longitude") or "N/A"
        d["google_maps_url"] = raw.get("google_maps_url") or "N/A"
        d["place_id"] = raw.get("place_id") or "N/A"
        d["plus_code"] = normalize_text(raw.get("plus_code"))
        d["rating"] = raw.get("rating") or "N/A"
        d["review_count"] = raw.get("review_count") or "N/A"
        d["claimed_status"] = normalize_text(raw.get("claimed_status"))
        d["business_status"] = normalize_text(raw.get("business_status"))
        d["business_hours"] = normalize_text(raw.get("business_hours"))
        d["business_description"] = normalize_text(raw.get("business_description"))
        d["source_query"] = raw.get("source_query") or "N/A"
        d["source_location"] = normalize_text(raw.get("source_location"))
        d["source_keyword"] = normalize_text(raw.get("source_keyword"))
        # Website-intelligence columns start empty; enrichment fills them.
        for col in OUTPUT_COLUMNS:
            d.setdefault(col, "N/A")
        # Ensure numeric-ish defaults so filters operate sanely.
        d.setdefault("meta_pixel", "NO")
        d.setdefault("ga4", "NO")
        d.setdefault("gtm", "NO")
        return rec

    def _enrich_records(self, records: list[BusinessRecord]) -> None:
        if not records:
            return
        # Sequential is safest for sites but bounded pool enables modest parallel.
        workers = max(1, min(self.website_workers, 8))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for rec in records:
                website = rec.data.get("website")
                if not website or website == "N/A":
                    rec.data["website_status"] = "N/A"
                    rec.data["website_failure_reason"] = "no_website"
                    self._finalize_record(rec, None)
                    continue
                futures[pool.submit(self._enrich_one, rec, website)] = rec
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    rich = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.error("enrichment crashed for %s: %s", rec.data.get("business_name"), e)
                    self.summary.bump("errors")
                    rich = None
                self._finalize_record(rec, rich)

    def _enrich_one(self, rec: BusinessRecord, website: str) -> dict:
        self.summary.bump("websites_processed")
        rich = self.enricher.enrich(website)
        # Track status stats.
        status = rich.get("website_status")
        if status == WebsiteStatus.LIVE:
            self.summary.bump("websites_live")
        elif status == "DEAD":
            self.summary.bump("websites_dead")
        reason = rich.get("website_failure_reason", "")
        if reason in ("HTTP_BLOCKED", "CAPTCHA_DETECTED"):
            self.summary.bump("websites_blocked")
        elif reason == "JS_REQUIRED":
            self.summary.bump("websites_js_required")
        elif reason == "TIMEOUT":
            self.summary.bump("websites_timed_out")
        return rich

    def _finalize_record(self, rec: BusinessRecord, rich: dict | None) -> None:
        if rich:
            evidence = rich.pop("_evidence", {})
            for k, v in rich.items():
                rec.set(k, v)
        # Email verification (optional).
        self._apply_email_verification(rec)

        # Validate.
        ok, problems = validate_record(rec.data,
                                       max_email_length=self.cfg.get("email", {}).get("max_email_length", 120),
                                       require_website=self.cfg.get("website", {}).get("require_website", False))
        if not ok:
            rec.set("filtered_out_reason", "validation_failed: " + "; ".join(problems[:3]))
            self.store.set_stage(rec.get("record_id"), "rejected")
            self._append_row(self.failed_path, rec.data)
            self.summary.bump("errors")
            return
        rec.set("filtered_out_reason", "")

        # Commit atomically: CSV row then checkpoint stage.
        row_index = self.csv.append(rec.data)
        self.store.mark_committed(rec.get("record_id"), row_index)
        self.store.set_stage(rec.get("record_id"), "committed")
        self.summary.bump("final_exported_records")

    def _apply_email_verification(self, rec: BusinessRecord) -> None:
        email_cfg = self.cfg.get("email", {})
        emails = rec.get("emails") or "N/A"
        primary = emails.split(",")[0].strip() if emails != "N/A" else ""

        # always record mx_enabled/smtp_enabled flags.
        rec.set("mx_enabled", "true" if self.mx.enabled else "false")
        rec.set("smtp_enabled", "true" if self.smtp.enabled else "false")
        if not primary:
            rec.set("mx_status", "N/A")
            rec.set("mx_reason", "no_email")
            rec.set("smtp_status", "Not Checked")
            rec.set("smtp_reason", "no_email")
            return

        if self.mx.enabled:
            status, reason = self.mx.check(primary)
            rec.set("mx_status", status)
            rec.set("mx_reason", reason)
            self.summary.bump("mx_checked")
            if status == "PASS":
                self.summary.bump("mx_passed")
        else:
            rec.set("mx_status", "NOT_CHECKED")
            rec.set("mx_reason", "mx_disabled")

        if self.smtp.enabled:
            status, reason = self.smtp.verify(primary)
            rec.set("smtp_status", status)
            rec.set("smtp_reason", reason)
            self.summary.bump("smtp_checked")
            if status == "Verified":
                self.summary.bump("smtp_verified")
            elif status in ("Inconclusive", "Catch-All"):
                self.summary.bump("smtp_inconclusive")
        else:
            rec.set("smtp_status", "Not Checked")
            rec.set("smtp_reason", "smtp_disabled")

    # ------------------------------------------------------------------
    def _append_row(self, path: Path, data: dict) -> None:
        """Append a row to an auxiliary CSV (filtered/failed). Thread-safe."""
        with self._lock:
            existed = path.exists()
            with open(path, "a", encoding="utf-8", newline="") as fh:
                import csv as _csv
                w = _csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
                if not existed or path.stat().st_size == 0:
                    w.writeheader()
                w.writerow({c: data.get(c, "") for c in OUTPUT_COLUMNS})

    def _json_dump(self, data: dict) -> str:
        import json
        return json.dumps(data, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    def _finalize(self) -> None:
        self.store.write_json_mirror()
        self.store.backup_checkpoint()

        # Quality gate (technical data-quality check).
        report = run_quality_gate(self.output_dir, checkpoint_store=self.store)
        write_quality_report(self.output_dir, report)
        if not report.passed:
            log.warning("quality gate FAILED: %s", "; ".join(report.issues))
        else:
            log.info("quality gate passed.")

        # XLSX from CSV.
        try:
            write_xlsx(self.csv_path, self.xlsx_path, OUTPUT_COLUMNS)
        except Exception as e:  # noqa: BLE001
            log.error("XLSX export failed: %s", e)

        self.summary.finish()
        self.summary.set("final_exported_records", self.csv.row_count)
        self.summary.write(self.summary_path)
        self.store.close()
        log.info("job complete. output directory: %s (quality %s)",
                 self.output_dir, "PASS" if report.passed else "ISSUES")
