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
from .filters import FilterEngine, POST_ENRICHMENT_FIELDS, require_website_filter
from .maps.collector import ZeroListingsError
from .models import OUTPUT_COLUMNS, BusinessRecord, WebsiteStatus
from .utils.normalize import (
    canonical_domain, normalize_email, normalize_phone, normalize_text, normalize_url,
)
from .validation import run_quality_gate, validate_record, write_quality_report
from .websites.enricher import WebsiteEnricher

log = logging.getLogger(__name__)

# Google Maps `gl` region code -> a display country name, used to fill the
# `country` column when Maps does not expose one on the place page.
_GL_COUNTRY = {
    "us": "United States", "uk": "United Kingdom", "gb": "United Kingdom",
    "ca": "Canada", "au": "Australia", "nz": "New Zealand",
    "de": "Germany", "fr": "France", "nl": "Netherlands", "ae": "United Arab Emirates",
    "in": "India", "pk": "Pakistan", "sg": "Singapore", "ie": "Ireland",
}
_GL_COUNTRY_UPPER = {k.upper(): v for k, v in _GL_COUNTRY.items()}
_GL_COUNTRY.update(_GL_COUNTRY_UPPER)


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

        # Dedup seeded from checkpoint (COMMITTED records only).
        self.resolver = IdentityResolver(
            seen_identities=self.store._identities,
            seen_domains=self.store._domains,
            seen_phones=self.store._phones,
            seen_domain_city=self.store._domain_city,
            default_country=cfg.get("country", {}).get("default", "US"),
        )

        # Filters (maps + website-inclusion).
        base_filters = FilterEngine(cfg.get("filters"))
        self.filters = base_filters
        # Pre-enrichment pass: only conditions on Maps-populated fields.
        # Enrichment-dependent conditions (ga4/gtm/emails/signals) run later.
        self.pre_filters = FilterEngine(self._pre_filter_dict(base_filters))
        self.post_filters = base_filters.split_by_enrichment(POST_ENRICHMENT_FIELDS)
        self.require_web = require_website_filter(
            cfg.get("website", {}).get("require_website", False))

        # Enrichment workers.
        website_cfg = cfg.get("website", {})
        self.website_workers = cfg.get("concurrency", {}).get("website_workers", 4)
        self.enricher = WebsiteEnricher(cfg, browser_manager=self._bm)

        # Email verification (optional).
        email_cfg = cfg.get("email", {})
        smtp_cfg = cfg.get("smtp", {})
        self.mx = MXChecker(enabled=email_cfg.get("enable_mx_check", False),
                            timeout=email_cfg.get("mx_timeout_seconds", 5.0))
        self.smtp = SMTPVerifier(
            enabled=smtp_cfg.get("enabled", False),
            timeout=smtp_cfg.get("verification_timeout_seconds", 20),
            retries=smtp_cfg.get("retries", 1),
        )
        # Cap concurrent SMTP checks to `smtp.workers` (was validated but never
        # applied; checks ran unbounded inside the enrichment pool).
        smtp_workers = min(max(smtp_cfg.get("workers", 3), 1), 8)
        self._smtp_sem = threading.Semaphore(smtp_workers)

        self.summary = RunSummary()
        self._query_keys: dict[str, str] = {}
        self._lock = threading.Lock()

        # Counters seed: record IDs are incremental via checkpoint.
        self._counter_offset = self.store.committed_count()

    # ------------------------------------------------------------------
    def _progress(self, line: str) -> None:
        """Print a clean, human-readable progress line (no log prefix/IDs).

        Written straight to the terminal so an operator watching a tmux session
        sees a simple running tally: which query is in progress, how many records
        have been found/exported, and when the run finishes.
        """
        print(line, flush=True)

    def run(self) -> None:
        queries = list(self.cfg["queries"])
        total = len(queries)
        self.summary.set("total_queries", total)
        remaining = self.store.remaining_queries(queries)
        self.summary.set("remaining_queries", len(remaining))
        # Recovered records: those already committed.
        self.summary.set("recovered_records", self.store.committed_count())

        already_done = total - len(remaining)
        client = self.cfg["job"].get("client_name", "default")

        self._progress("=" * 58)
        self._progress(f"  B2B LEAD SCRAPER  —  {client}")
        self._progress(f"  Total searches : {total}")
        self._progress(f"  Already done   : {already_done}")
        self._progress(f"  To do now      : {len(remaining)}")
        self._progress("=" * 58)

        for idx, query in enumerate(queries, 1):
            if self.store.query_status(query) == "done":
                self._progress(f"[{idx}/{total}] SKIP (already done)  {query}")
                continue
            self._process_query(query, idx, total)

        # Finalize.
        self.csv.close()
        # Reflect the run's true end-state: after all queries are processed,
        # nothing remains. (Previously remaining_queries kept its start-of-run
        # value, so a completed job misleadingly reported "remaining: 3".)
        self.summary.set("remaining_queries", 0)
        self._finalize()

        s = self.summary.to_dict()
        self._progress("=" * 58)
        self._progress("  DONE.")
        self._progress(f"  Records exported : {s['final_exported_records']}")
        self._progress(f"  Duplicates removed: {s['duplicates_removed']}")
        self._progress(f"  Filtered out     : {s['filtered_out']}")
        self._progress(f"  Queries completed: {s['completed_queries']}/{total}")
        self._progress("=" * 58)

    # ------------------------------------------------------------------
    def _process_query(self, query: str, qidx: int = 0, qtotal: int = 0) -> None:
        tag = f"[{qidx}/{qtotal}] " if qtotal else ""
        self._progress(f"{tag}RUNNING  {query}")
        log.info("processing query: %s", query)
        self.store.set_query_status(query, "running")

        records = []
        try:
            for raw in self.maps.collect(query):
                self.summary.bump("businesses_discovered")
                self.store.bump_query_count(query, "discovered", 1)
                rec = self._normalize_maps(raw)
                # Live per-record tally (what the operator asked to see).
                name = rec.data.get("business_name") or "?"
                self._progress(
                    f"    + found #{self.summary.stats['businesses_discovered']}: "
                    f"{name}")
                # Early dedup.
                is_dup, reason, sig = self.resolver.is_duplicate(rec.data)
                if is_dup:
                    self.summary.bump("duplicates_removed")
                    log.info("duplicate removed (%s): %s",
                             reason, rec.data.get("business_name"))
                    continue

                rec_id = str(uuid.uuid4())
                rec.set("record_id", rec_id)
                self.store.register_record(
                    rec_id, sig.get("identity_key", ""), sig.get("place_id") or "",
                    sig.get("canonical_domain") or "", sig.get("normalized_phone") or "",
                    sig.get("city") or "", query, self._json_dump(rec.data))

                # Pre-enrichment filter: Maps-populated fields only.
                ok, freason = self.pre_filters.evaluate(rec.data)
                if not ok or not self.require_web_ok(rec.data):
                    if not ok:
                        reason_text = freason or "filtered"
                    else:
                        reason_text = "website_missing"
                    rec.set("filtered_out_reason", reason_text)
                    self.store.set_stage(rec_id, "filtered")
                    self._rollback_identity(rec)  # pre-filter rejection must undo dedup registration (see #1)
                    self._append_row(self.filtered_path, rec.data)
                    self.summary.bump("filtered_out")
                    continue

                self.store.set_stage(rec_id, "accepted")
                records.append(rec)
        except ZeroListingsError as e:
            # Non-empty search yielded zero links: leave query `failed` so it is
            # retried on the next run, not silently marked done.
            log.error("query failed (collector extracted 0 listings): %s", e)
            self.store.set_query_status(query, "failed")
            self.summary.bump("queries_failed", 1)
            self.store.write_json_mirror()
            self._recycle_browser_if_needed(query)
            return

        # Enrich accepted records (bounded website workers), then run the
        # post-enrichment filters and finally commit — in that order so filters
        # see uncommitted records (previously they ran after commit and skipped
        # everything, making post-enrichment filters dead code).
        self._enrich_records(records)
        self._apply_post_filters(records)
        self._commit_accepted(records)

        # Mark query done.
        self.store.set_query_status(query, "done")
        self.summary.bump("completed_queries")
        self.store.write_json_mirror()
        # Snapshot the SQLite checkpoint after every completed query so a
        # mid-run crash that corrupts the main DB can recover from the backup,
        # rather than losing the whole run (previously the .bak was only
        # written at job end).
        self.store.backup_checkpoint()
        self._recycle_browser_if_needed(query)

        s = self.summary.to_dict()
        tag = f"[{qidx}/{qtotal}] " if qtotal else ""
        self._progress(
            f"{tag}DONE     {query}  →  discovered {s['businesses_discovered']}, "
            f"exported {s['final_exported_records']}")

    def _apply_post_filters(self, records: list[BusinessRecord]) -> None:
        """Re-check enrichment-dependent filters; reject records that fail.

        Records that fail are marked `filtered` and their dedup signals are
        rolled back from the resolver so a later re-discovery in-session is not
        blocked (fixes the rejected-record dedup leak).
        """
        if not self.post_filters._filters:
            return
        for rec in records:
            # Only run on records that survived enrichment/validation (not yet
            # rejected); never skip them because of a prior commit.
            if self.store._stage(rec.get("record_id")) in ("rejected", "filtered"):
                continue
            ok, freason = self.post_filters.evaluate(rec.data)
            if not ok:
                rec.set("filtered_out_reason", freason or "post_filtered")
                self.store.set_stage(rec.get("record_id"), "filtered")
                self._rollback_identity(rec)
                self._append_row(self.filtered_path, rec.data)
                self.summary.bump("filtered_out")

    def _commit_accepted(self, records: list[BusinessRecord]) -> None:
        """Commit records that passed enrichment, validation, and post-filters."""
        for rec in records:
            if self.store._stage(rec.get("record_id")) in ("rejected", "filtered"):
                continue
            row_index = self.csv.append(rec.data)
            self.store.mark_committed(rec.get("record_id"), row_index)
            self.store.set_stage(rec.get("record_id"), "committed")
            # Per-query committed tally (previously always 0).
            q = rec.data.get("source_query")
            if q:
                self.store.bump_query_count(q, "committed", 1)
            self.summary.bump("final_exported_records")

    def _recycle_browser_if_needed(self, query: str) -> None:
        if self._bm is not None:
            try:
                self._bm.mark_query()
                self._bm.recycle()
            except Exception as e:  # noqa: BLE001
                log.debug("browser recycle skipped: %s", e)

    def _pre_filter_dict(self, base: FilterEngine) -> dict:
        """Return filters containing only conditions that do NOT depend on
        enrichment-populated fields (so they can run before the expensive
        website step)."""
        from .filters.engine import _normalize_conds, _ALIASES
        post = POST_ENRICHMENT_FIELDS
        out: dict = {}
        for group in ("include_all", "include_any", "exclude_all", "exclude_any"):
            conds = _normalize_conds(group, base._filters.get(group))
            kept = []
            for c in conds:
                fld = _ALIASES.get(c["field"], c["field"])
                fld_real = {"website": "website", "review_count": "review_count",
                            "rating": "rating", "email_found": "emails"}.get(
                                c["field"], fld)
                if fld_real not in post and c["field"] != "email_found":
                    kept.append(c)
            if kept:
                out[group] = kept
        return out

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
        # Phone must be country-aware so the stored value matches the identity
        # key used by dedup (both derive from country.default). Previously the
        # default country was ignored here, so a non-US job stored a US-coded
        # phone that no longer matched its own dedup key on resume.
        d["phone"] = normalize_phone(
            raw.get("phone"), self.cfg.get("country", {}).get("default", "US")
        ) if raw.get("phone") else "N/A"
        d["website"] = normalize_url(raw.get("website")) if raw.get("website") else "N/A"
        d["address"] = normalize_text(raw.get("address", raw.get("full_address")))
        d["full_address"] = normalize_text(raw.get("full_address"))
        d["city"] = normalize_text(raw.get("city"))
        d["state"] = normalize_text(raw.get("state"))
        d["postal_code"] = normalize_text(raw.get("postal_code"))
        # Country: Google Maps place pages rarely expose a standalone country
        # field; infer it from the region (`gl`) when missing so the column is
        # not left all-"N/A". Mapping covers the common target regions.
        country = normalize_text(raw.get("country"))
        if not country or country == "N/A":
            country = _GL_COUNTRY.get(self.cfg.get("maps", {}).get("gl", ""), "N/A")
        d["country"] = country
        d["latitude"] = raw.get("latitude") or "N/A"
        d["longitude"] = raw.get("longitude") or "N/A"
        d["google_maps_url"] = raw.get("google_maps_url") or "N/A"
        # Missing place_id stays None for identity resolution (so a missing ID
        # never collides in dedup); the CSV writer renders None as the configured
        # missing value ("N/A") for display. Fixes critical dedup collision.
        d["place_id"] = raw.get("place_id") if raw.get("place_id") else None
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
        """Enrich + email-verify + validate each record (but do NOT commit).

        Commit is deferred to `_commit_accepted` so post-enrichment filters can
        reject a record before it is written, fixing the dead-code bug where
        filters ran after every record was already committed.
        """
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
        # Track email extraction counters (previously dead stats that always
        # reported 0 despite emails being found in the CSV).
        self.summary.bump("emails_found", int(rich.get("email_count", 0) or 0))
        self.summary.bump("emails_rejected", int(rich.get("_emails_rejected", 0) or 0))
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
        evidence = {}
        if rich:
            evidence = rich.pop("_evidence", {})
            rich.pop("_emails_rejected", None)  # internal counter, not a schema field
            for k, v in rich.items():
                rec.set(k, v)
        # Retain signal evidence for auditability (logged, not in CSV).
        if evidence:
            rec.evidence = evidence
            log.debug("signals for %s: %s",
                      rec.data.get("business_name"), ",".join(evidence))
        # Email verification (optional).
        self._apply_email_verification(rec)

        # Validate.
        ok, problems = validate_record(rec.data,
                                       max_email_length=self.cfg.get("email", {}).get("max_email_length", 120),
                                       require_website=self.cfg.get("website", {}).get("require_website", False))
        if not ok:
            rec.set("filtered_out_reason", "validation_failed: " + "; ".join(problems[:3]))
            self.store.set_stage(rec.get("record_id"), "rejected")
            self._rollback_identity(rec)
            self._append_row(self.failed_path, rec.data)
            self.summary.bump("errors")
            return
        rec.set("filtered_out_reason", "")
        # NOTE: commit (CSV + checkpoint 'committed' stage) is deferred to
        # `_commit_accepted`, which runs after post-enrichment filters.

    def _rollback_identity(self, rec: BusinessRecord) -> None:
        """Undo a record's dedup registration when it is rejected/filtered."""
        try:
            self.resolver.rollback(rec.data)
        except Exception as e:  # noqa: BLE001
            log.debug("dedup rollback skipped: %s", e)

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
            with self._smtp_sem:
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
