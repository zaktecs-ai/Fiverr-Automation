"""Run summary (run_summary.json) generation."""
from __future__ import annotations

import json
import time
from pathlib import Path


class RunSummary:
    def __init__(self):
        self.start_ts = time.time()
        self.end_ts = None
        self.stats = {
            "total_queries": 0, "completed_queries": 0, "remaining_queries": 0,
            "businesses_discovered": 0, "duplicates_removed": 0, "filtered_out": 0,
            "websites_processed": 0, "websites_live": 0, "websites_dead": 0,
            "websites_blocked": 0, "websites_js_required": 0, "websites_timed_out": 0,
            "emails_found": 0, "emails_rejected": 0,
            "mx_checked": 0, "mx_passed": 0,
            "smtp_checked": 0, "smtp_verified": 0, "smtp_inconclusive": 0,
            "playwright_fallbacks": 0, "retries": 0, "errors": 0,
            "recovered_records": 0, "final_exported_records": 0,
            "execution_duration_seconds": 0.0,
            "peak_memory_mb": 0.0,
        }

    def bump(self, key: str, amount: int = 1) -> None:
        if key in self.stats:
            self.stats[key] += amount

    def set(self, key: str, value) -> None:
        self.stats[key] = value

    def finish(self) -> None:
        self.end_ts = time.time()
        self.stats["execution_duration_seconds"] = round(self.end_ts - self.start_ts, 2)
        self._capture_memory()

    def _capture_memory(self) -> None:
        try:
            import resource
            self.stats["peak_memory_mb"] = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
        except Exception:
            pass

    def to_dict(self) -> dict:
        return self.stats

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path
