"""Data model: the master output schema and internal record representation.

The record is a thin dict with a fixed, documented schema. Every column is
assigned a value; unavailable data gets the configured missing-value ("N/A").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Canonical output column order. This is the single source of truth for the
# CSV header, XLSX columns, and validation.
OUTPUT_COLUMNS: list[str] = [
    # --- Identity ---
    "business_name", "category", "subcategory", "phone", "website",
    "address", "full_address", "city", "state", "postal_code", "country",
    "latitude", "longitude", "google_maps_url", "place_id", "plus_code",
    # --- Maps intelligence ---
    "rating", "review_count", "claimed_status", "business_status",
    "business_hours", "business_description",
    # --- Provenance (removable via config) ---
    "source_query", "source_location", "source_keyword",
    # --- Website intelligence ---
    "website_status", "website_failure_reason",
    "emails", "email_count",
    "facebook", "instagram", "linkedin", "youtube", "twitter_x",
    "tiktok", "pinterest",
    "tech_stack",
    # Technologies / signals (individual)
    "cms", "analytics", "tag_manager", "meta_pixel", "ga4", "gtm",
    "advertising", "booking_system", "chat_widget", "ssl",
    # Signals
    "signal_pricing", "signal_financing", "signal_licensed_insured",
    "signal_established", "signal_portfolio", "signal_mobile_service",
    "signal_membership",
    # --- Verification ---
    "mx_enabled", "mx_status", "mx_reason",
    "smtp_enabled", "smtp_status", "smtp_reason",
    # --- Housekeeping ---
    "filtered_out_reason",
    "record_id",
]


@dataclass
class BusinessRecord:
    """Internal record before commit. `data` holds all OUTPUT_COLUMNS keys."""
    data: dict[str, Any] = field(default_factory=dict)
    # Evidence map for signals (name -> evidence string); emitted when configured.
    evidence: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.data.setdefault("record_id", "")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def as_row(self, missing: str = "N/A") -> list[str]:
        """Return a list of values in canonical column order, strings with
        the configured missing-value applied."""
        return [_to_cell(self.data.get(col), missing) for col in OUTPUT_COLUMNS]


def _to_cell(value, missing: str) -> str:
    if value is None or value == "":
        return missing
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# Business status classification (website)
# ---------------------------------------------------------------------------

class WebsiteStatus:
    LIVE = "LIVE"
    DEAD = "DEAD"


class FailureReason:
    HTTP_BLOCKED = "HTTP_BLOCKED"
    CAPTCHA_DETECTED = "CAPTCHA_DETECTED"
    JS_REQUIRED = "JS_REQUIRED"
    DNS_FAILURE = "DNS_FAILURE"
    TIMEOUT = "TIMEOUT"
    TLS_ERROR = "TLS_ERROR"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    NOT_FOUND = "NOT_FOUND"
    UNREACHABLE = "UNREACHABLE"
    UNKNOWN = "UNKNOWN"


# Failure reasons that are *temporary/scraper-side* and must never imply a dead site.
_TRANSIENT = {FailureReason.HTTP_BLOCKED, FailureReason.CAPTCHA_DETECTED,
              FailureReason.JS_REQUIRED, FailureReason.TIMEOUT, FailureReason.UNKNOWN}


def is_dead_signal(reason: str) -> bool:
    """True only when the reason is strong evidence the site is truly gone."""
    return reason in {FailureReason.DNS_FAILURE, FailureReason.CONNECTION_REFUSED,
                      FailureReason.NOT_FOUND, FailureReason.TLS_ERROR}


def resolve_website_status(reason: str) -> str:
    """Map a failure reason to a website_status, never conflating transient
    failures with a dead site."""
    if reason in _TRANSIENT:
        return WebsiteStatus.LIVE
    if is_dead_signal(reason):
        return WebsiteStatus.DEAD
    return WebsiteStatus.LIVE


# SMTP statuses (explicit; never collapse uncertainty into false certainty).
SMTP_STATUSES = {"Verified", "Invalid", "Catch-All", "Connection Failed",
                 "Blocked", "Inconclusive", "Timeout", "Not Checked"}
