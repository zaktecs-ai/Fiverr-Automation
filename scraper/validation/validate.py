"""Per-record validation before commit.

Checks required fields, email syntax, URL shape, status consistency, and
normalized keys. Returns (ok, problems) so the pipeline can record failures
rather than silently committing junk.
"""
from __future__ import annotations

from ..models import (
    OUTPUT_COLUMNS, FailureReason, WebsiteStatus, resolve_website_status,
)
from ..utils.normalize import (
    is_valid_email, normalize_email, normalize_phone, normalize_url,
)


def _looks_like_url(value) -> bool:
    if not value or value == "N/A":
        return True  # absence is fine
    return value.lower().startswith(("http://", "https://"))


def validate_email_field(value) -> tuple[bool, str]:
    """Validate a (possibly comma-separated) email cell."""
    if not value or value == "N/A":
        return True, ""
    emails = [e.strip() for e in value.split(",") if e.strip()]
    bad = [e for e in emails if not is_valid_email(normalize_email(e))]
    if bad:
        return False, f"invalid emails: {', '.join(bad)}"
    return True, ""


def validate_website_status(record: dict) -> tuple[bool, str]:
    status = (record.get("website_status") or "LIVE").upper()
    reason = (record.get("website_failure_reason") or "").upper()

    # A not-assessed status ("N/A" — no website, or not yet fetched) is fine and
    # carries no contradiction regardless of the accompanying reason.
    if status in ("N/A", "NA", ""):
        return True, ""

    # The one genuine contradiction we must catch: marked DEAD for a transient,
    # scraper-side reason. DEAD must mean strong evidence the site is gone.
    if status == WebsiteStatus.DEAD and reason in (
        FailureReason.HTTP_BLOCKED, FailureReason.CAPTCHA_DETECTED,
        FailureReason.JS_REQUIRED, FailureReason.TIMEOUT):
        return False, f"contradiction: status=DEAD but reason={reason} (transient)"

    # If a real (non-placeholder) reason is present, it must map to the status.
    if reason and reason not in ("N/A", "NA", "") and \
            resolve_website_status(reason) != status:
        return False, f"status {status} inconsistent with reason {reason}"
    return True, ""


def validate_record(record: dict, max_email_length: int = 120,
                    require_website: bool = False) -> tuple[bool, list[str]]:
    """Return (ok, problems). problems is a list of human-readable strings."""
    problems: list[str] = []

    # Required schema fields present.
    for col in ("business_name", "source_query"):
        if not record.get(col) or record.get(col) in ("N/A", ""):
            problems.append(f"missing required field: {col}")

    # Website shape.
    website = record.get("website")
    if website and website != "N/A" and not _looks_like_url(website):
        problems.append(f"malformed website URL: {website!r}")
    if require_website and (not website or website == "N/A"):
        problems.append("require_website=true but no website")

    # Email cells.
    ok_emails, eb = validate_email_field(record.get("emails"))
    if not ok_emails:
        problems.append(eb)

    # Status consistency.
    ok_status, sb = validate_website_status(record)
    if not ok_status:
        problems.append(sb)

    # Normalized phone / website sanity.
    if record.get("phone") and record.get("phone") != "N/A":
        np = normalize_phone(record["phone"])
        if len(np) < 6:
            problems.append(f"suspicious phone: {record['phone']!r}")

    # Ensure all canonical columns are present (fill N/A where missing downstream).
    for col in OUTPUT_COLUMNS:
        if col not in record:
            problems.append(f"missing column: {col}")

    return (len(problems) == 0, problems)
