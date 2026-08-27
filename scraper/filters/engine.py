"""Deterministic filter engine.

Supports conditions expressed over record fields with AND / OR / NOT
semantics. Filters run AFTER maps collection/normalization/deduplication and
BEFORE expensive website enrichment. Rejected records are preserved in a
separate output with a `filtered_out_reason`.

Configuration shapes:
    filters:
      include_all:            # record passes only if ALL hold (AND)
        - website: yes
        - reviews: ">= 15"
      include_any:            # record passes if ANY hold (OR)
        - field: email_found
          op: "="
          value: "yes"
      exclude_all:            # record is rejected if ALL hold
        - ...
      exclude_any:            # record is rejected if ANY hold

Condition forms:
  * {field: value}                                  -> equality (with type coercion)
  * {field: value, op: ">="}                        -> comparison operators
  * {field: "yes", negate: true}                    -> NOT
"""
from __future__ import annotations

from typing import Any

_OPS = {"=", "!=", ">", "<", ">=", "<=", "in", "notin", "contains"}

_ALIASES = {
    "reviews": "review_count",
    "email_found": "email_found",
    "require_email": "email_found",
    "has_website": "website",
    "gtm": "tag_manager",
    "ga4": "ga4",
    "meta_pixel": "meta_pixel",
}


def _coerce(value: Any):
    """Coerce a string to int/float/bool when unambiguous; else return as-is."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    low = s.lower()
    if low in ("yes", "true", "y", "1"):
        return True
    if low in ("no", "false", "n", "0", ""):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _field_value(record: dict, field: str):
    """Resolve a config field name to a comparable value, with semantic helpers."""
    key = _ALIASES.get(field, field)
    if field == "website":
        return record.get(key) not in (None, "", "N/A")
    if field == "email_found":
        return record.get("emails") not in (None, "", "N/A")
    if field == "meta_pixel":
        return (record.get("meta_pixel") or "").strip().lower() in ("yes", "true", "detected")
    if field == "ga4":
        return (record.get("ga4") or "").strip().lower() in ("yes", "true", "detected")
    if field == "gtm":
        return (record.get("tag_manager") or "").strip().lower() not in ("", "no", "false", "n/a")
    return record.get(key)


def _compare(record: dict, cond: dict) -> bool:
    """Evaluate a single normalized condition {field, op, value, negate}."""
    field = cond["field"]
    op = cond.get("op", "=")
    value = cond.get("value")
    negate = bool(cond.get("negate", False))
    if op not in _OPS:
        raise ValueError(f"unknown filter op: {op!r} (allowed: {sorted(_OPS)})")

    actual = _coerce(_field_value(record, field))
    expected = _coerce(value)

    if op in (">", "<", ">=", "<="):
        try:
            a = float(actual) if actual is not None else 0.0
            b = float(expected)
            result = {
                ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b,
            }[op]
        except (TypeError, ValueError):
            result = False
    elif op == "!=":
        result = actual != expected
    elif op == "in":
        result = expected in (actual if isinstance(actual, (list, tuple)) else [actual])
    elif op == "notin":
        result = expected not in (actual if isinstance(actual, (list, tuple)) else [actual])
    elif op == "contains":
        result = str(expected) in str(actual)
    else:  # "="
        result = actual == expected

    return (not result) if negate else result


def _normalize_conds(name: str, conds) -> list[dict]:
    """Normalize shorthand ({rating: 4}) and explicit forms to a uniform list."""
    if conds is None:
        return []
    if isinstance(conds, dict):
        conds = [conds]
    out: list[dict] = []
    for c in conds:
        if not isinstance(c, dict):
            raise ValueError(f"filters.{name}: each condition must be a map, got {c!r}")
        if "field" in c:
            fld = c["field"]
            values = {k: v for k, v in c.items() if k not in ("field", "op", "negate")}
            if not values:
                raise ValueError(f"filters.{name}: condition {c!r} has no value")
            op = c.get("op", "=")
            neg = bool(c.get("negate", False))
            if "value" in values:
                out.append({"field": fld, "op": op, "value": values["value"], "negate": neg})
            else:
                for k, v in values.items():
                    out.append({"field": fld if k == fld else k, "op": op,
                                "value": v, "negate": neg})
        else:
            for k, v in c.items():
                if k in ("op", "negate"):
                    continue
                out.append({"field": k, "op": "=", "value": v, "negate": False})
    return out


class FilterEngine:
    def __init__(self, filters: dict | None):
        self._filters = filters or {}

    def evaluate(self, record: dict) -> tuple[bool, str]:
        """Return (accepted, reason). Reason is '' when accepted."""
        f = self._filters

        include_all = _normalize_conds("include_all", f.get("include_all"))
        include_any = _normalize_conds("include_any", f.get("include_any"))
        exclude_all = _normalize_conds("exclude_all", f.get("exclude_all"))
        exclude_any = _normalize_conds("exclude_any", f.get("exclude_any"))

        if include_all and not all(_compare(record, c) for c in include_all):
            return False, "failed_include_all"
        if include_any and not any(_compare(record, c) for c in include_any):
            return False, "failed_include_any"
        if exclude_all and all(_compare(record, c) for c in exclude_all):
            return False, "excluded_by_all"
        if exclude_any and any(_compare(record, c) for c in exclude_any):
            return False, "excluded_by_any"
        return True, ""

    def split_by_enrichment(self, post_enrichment_fields: set[str]) -> "FilterEngine":
        """Return a NEW engine holding only conditions that depend on fields
        populated during website enrichment (emails, ga4, gtm, meta_pixel, …).

        Conditions are routed by the fields they reference. A condition group
        (include_all etc.) is split so that pre-enrichment conditions can run
        early (before expensive enrichment) while enrichment-dependent
        conditions run afterwards. The original engine is left unchanged.
        """
        post_fields = post_enrichment_fields or set()

        def _depends_on_post(cond: dict) -> bool:
            # Resolve the alias of the field to the real record key.
            fld = _ALIASES.get(cond["field"], cond["field"])
            return fld in post_fields

        post_filters: dict = {}
        for group in ("include_all", "include_any", "exclude_all", "exclude_any"):
            conds = _normalize_conds(group, self._filters.get(group))
            if not conds:
                continue
            post_conds = [c for c in conds if _depends_on_post(c)]
            if post_conds:
                post_filters[group] = post_conds
        return FilterEngine(post_filters)


# Fields that are always populated only after website enrichment. Filter
# conditions referencing these must run post-enrichment, not in the early
# (pre-enrichment) filter pass.
POST_ENRICHMENT_FIELDS = {
    "emails", "email_found", "email_count",
    "ga4", "gtm", "meta_pixel", "analytics", "tag_manager", "cms",
    "advertising", "booking_system", "chat_widget", "ssl",
    "facebook", "instagram", "linkedin", "youtube", "twitter_x",
    "tiktok", "pinterest", "tech_stack",
    "website_status", "website_failure_reason",
    "signal_pricing", "signal_financing", "signal_licensed_insured",
    "signal_established", "signal_portfolio", "signal_mobile_service",
    "signal_membership",
}


def require_website_filter(require: bool) -> FilterEngine:
    """Helper: engine that rejects records without a website when `require`."""
    if require:
        return FilterEngine({"include_all": [{"field": "website", "op": "=", "value": "yes"}]})
    return FilterEngine({})
