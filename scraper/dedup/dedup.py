"""Identity resolution and deduplication.

Strategy (documented in docs/architecture.md):
  * Build a composite identity key from the strongest available signals.
  * Precedence: Google place_id > (canonical domain + city) > (normalized
    phone) > (normalized name + city).
  * A duplicate website does NOT by itself mean 'same business' — multi-location
    chains share a domain. We require a co-occurring signal (same city, or same
    phone) before treating two records as the same business.
  * Default policy: FIRST VALID RECORD WINS (the first seen is kept; later
    collisions are dropped as duplicates).
"""
from __future__ import annotations

import hashlib

from ..utils.normalize import (
    extract_domain,
    normalize_phone,
    normalize_text,
    normalize_url,
)

# Thresholds for fuzzy name matching (shared prefix ratio).
_NAME_PREFIX_MIN_LEN = 8


def _name_key(name: str) -> str:
    """A loose name key: lowercased, alphanumeric-only, punctuation stripped."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def resolve_identity(record: dict, default_country: str = "US") -> dict:
    """Compute identity signals + a composite key for a record dict."""
    name = normalize_text(record.get("business_name"))
    raw_url = normalize_url(record.get("website", ""))
    # Extract the registrar-level domain from the (already normalized) URL.
    # canonical_domain() expects a bare hostname; passing it the full URL string
    # produced a garbage key like "https://host/path" and silently broke the
    # domain+city dedup fallback. extract_domain() does hostname extraction then
    # canonicalization and is the correct helper here.
    domain = extract_domain(raw_url) if raw_url != "N/A" else ""
    phone = normalize_phone(record.get("phone"), default_country)
    city = normalize_text(record.get("city")).lower()
    raw_pid = (record.get("place_id") or "").strip()
    place_id = None if (not raw_pid or raw_pid.upper() == "N/A") else raw_pid

    signals = {
        "place_id": place_id,
        "canonical_domain": domain or None,
        "normalized_phone": phone if phone != "N/A" else None,
        "name_key": _name_key(name) if name != "N/A" else None,
        "city": city if city != "n/a" else None,
    }

    key_parts = []
    if place_id:
        key_parts.append(f"pid:{place_id}")
        signals["key_type"] = "place_id"
    elif domain and city:
        key_parts.append(f"dom:{domain}")
        key_parts.append(f"city:{city}")
        signals["key_type"] = "domain+city"
    elif signals["normalized_phone"]:
        key_parts.append(f"ph:{signals['normalized_phone']}")
        signals["key_type"] = "phone"
    elif signals["name_key"] and city:
        key_parts.append(f"name:{signals['name_key']}")
        key_parts.append(f"city:{city}")
        signals["key_type"] = "name+city"
    else:
        signals["key_type"] = "none"

    composite = "|".join(key_parts)
    signals["identity_key"] = hashlib.sha1(composite.encode("utf-8")).hexdigest() if composite else ""
    return signals


class IdentityResolver:
    """Stateful resolver that remembers seen identities/domains/phones and
    decides duplicate-vs-new, backing onto an in-memory set seeded from the
    checkpoint store."""

    def __init__(self, seen_identities: set[str] | None = None,
                 seen_domains: set[str] | None = None,
                 seen_phones: set[str] | None = None,
                 seen_domain_city: set[str] | None = None,
                 default_country: str = "US"):
        self._identities: set[str] = set(seen_identities or set())
        self._domains: set[str] = set(seen_domains or set())
        self._phones: set[str] = set(seen_phones or set())
        self._domain_city: set[str] = set(seen_domain_city or set())
        self._default_country = default_country

    def is_duplicate(self, record: dict) -> tuple[bool, str, dict]:
        """Return (is_dup, reason, signals). New records are recorded as seen."""
        sig = resolve_identity(record, self._default_country)
        key = sig["identity_key"]
        domain = sig["canonical_domain"]
        phone = sig["normalized_phone"]
        place_id = sig["place_id"]
        city = sig["city"]

        if key and key in self._identities:
            return True, f"duplicate_identity:{sig['key_type']}", sig

        # A Google place_id is authoritative: a record that carries one is
        # uniquely identified by it. Shared phone / domain+city must NOT merge
        # two listings that carry different place_ids — a franchise front-desk
        # line or a shared corporate domain is common, and collapsing them
        # silently drops a real business (a false merge). So the weaker
        # phone / domain+city fallback guards apply — and register — ONLY for
        # records that LACK a place_id (the genuinely ambiguous case where
        # those signals are the strongest remaining evidence). A place_id
        # record never poisons the fallback sets, and a place_id-less record
        # sharing a phone with a place_id record is left as a distinct listing
        # rather than merged on a weak, possibly-shared signal.
        if place_id is None:
            if domain and city:
                domain_city_key = f"{domain}|{city}"
                if domain_city_key in self._domain_city:
                    return True, "duplicate_domain+city", sig
            if phone and phone in self._phones:
                return True, "duplicate_phone", sig

        # Record as seen (first valid record wins). Only place_id-less records
        # feed the fallback sets, mirroring the guard logic above.
        if key:
            self._identities.add(key)
        if domain:
            self._domains.add(domain)
            if city and place_id is None:
                self._domain_city.add(f"{domain}|{city}")
        if phone and place_id is None:
            self._phones.add(phone)
        return False, "", sig

    def _seen_domain_city(self) -> set[str]:
        return self._domain_city

    def rollback(self, record: dict) -> None:
        """Remove a record's identity signals from the seen sets.

        Used when a record is later rejected/filtered so a legitimate
        re-discovery of the same business later in the same run is not wrongly
        dropped as a duplicate (the rejected-record dedup leak).
        """
        sig = resolve_identity(record, self._default_country)
        key = sig["identity_key"]
        domain = sig["canonical_domain"]
        phone = sig["normalized_phone"]
        city = sig["city"]
        if key:
            self._identities.discard(key)
        if domain:
            self._domains.discard(domain)
            if city:
                self._domain_city.discard(f"{domain}|{city}")
        if phone:
            self._phones.discard(phone)
