"""Generate XLSX from the final CSV (or a list of rows).

XLSX is a convenience output, NOT the persistence mechanism. CSV remains the
append-safe source of truth; XLSX is produced after the job completes.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def write_xlsx(csv_path: str | Path, xlsx_path: str | Path,
               columns: list[str] | None = None) -> Path:
    """Convert a CSV into an XLSX file. Returns the xlsx path."""
    csv_path = Path(csv_path)
    xlsx_path = Path(xlsx_path)
    try:
        from openpyxl import Workbook
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("openpyxl is required for XLSX export. "
                           "Run `pip install openpyxl`.") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            ws.append(row)

    # Basic usability: freeze header, bold it, autofilter width hint.
    ws.freeze_panes = "A2"
    from openpyxl.styles import Font
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
    ws.auto_filter.ref = ws.dimensions

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(xlsx_path))
    log.info("XLSX written: %s", xlsx_path)
    return xlsx_path
