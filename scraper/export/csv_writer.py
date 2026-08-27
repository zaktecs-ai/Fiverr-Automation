"""Append-safe CSV writer with atomic row commits and recovery.

CSV is the primary source of truth. Each row is written, flushed, and fsync'd
before the checkpoint is updated, so a crash never loses a committed row nor
produces a malformed trailing partial row.

Recovery: on open, the writer validates the existing CSV and trims any
malformed trailing line (from a partial write) back to the last complete row.
"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class AtomicCSVWriter:
    def __init__(self, path: str | Path, columns: list[str]):
        self.path = Path(path)
        self.columns = columns
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._writer = None
        self._row_count = 0
        self._open()

    def _open(self) -> None:
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        if is_new:
            self._fh = open(self.path, "w", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._fh, fieldnames=self.columns)
            self._writer.writeheader()
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._row_count = 0
        else:
            self._recover()
            self._fh = open(self.path, "a", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._fh, fieldnames=self.columns)
            self._row_count = self._count_rows()

    def _recover(self) -> None:
        """Validate the existing CSV; trim a partial trailing line if present."""
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh)
                rows = list(reader)
            if not rows:
                return
            # Header must match.
            if rows[0] != [str(c) for c in self.columns]:
                log.warning("CSV header mismatch; expected %s, found %s. Not rewriting.",
                            self.columns, rows[0])
                return
            # If the last line is incomplete (fewer fields), drop it.
            expected = len(self.columns)
            if rows and len(rows[-1]) != expected:
                log.warning("trimming malformed trailing row (%d fields vs %d)",
                            len(rows[-1]), expected)
                with open(self.path, "w", encoding="utf-8", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerows(rows[:-1])
        except Exception as e:  # noqa: BLE001
            log.warning("CSV recovery failed (will append): %s", e)

    def _count_rows(self) -> int:
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as fh:
                return max(0, sum(1 for _ in fh) - 1)  # minus header
        except Exception:
            return 0

    def append(self, row: dict) -> int:
        """Append a row, flush + fsync. Returns the new row index (0-based)."""
        # Normalize to column order; missing -> ''.
        ordered = {c: row.get(c, "") for c in self.columns}
        # Ensure legal cell values (str only).
        for k in list(ordered.keys()):
            v = ordered[k]
            if v is None:
                ordered[k] = ""
            elif not isinstance(v, str):
                ordered[k] = str(v)
        self._writer.writerow(ordered)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._row_count += 1
        return self._row_count - 1

    @property
    def row_count(self) -> int:
        return self._row_count

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except Exception:
                pass
            self._fh.close()
            self._fh = None
