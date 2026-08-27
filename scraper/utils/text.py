"""Small text helpers shared across extractors."""
from __future__ import annotations

import re


def collapse_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def to_int(value) -> int | None:
    """Parse an integer, tolerating '1,234' and '1.2K' style strings."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    # Handle "1.2k" / "350k" suffixes.
    m = re.match(r"^([\d.]+)\s*([km])?$", s.replace(",", ""))
    if not m:
        m = re.search(r"[\d,]+", s)
        if not m:
            return None
        s = m.group(0).replace(",", "")
        try:
            return int(float(s))
        except ValueError:
            return None
    num = float(m.group(1))
    suffix = m.group(2)
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


def to_float(value) -> float | None:
    if value is None:
        return None
    s = re.search(r"[\d]+(?:\.\d+)?", str(value))
    if not s:
        return None
    try:
        return float(s.group(0))
    except ValueError:
        return None
