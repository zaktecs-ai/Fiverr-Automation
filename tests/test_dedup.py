"""Tests for identity resolution and deduplication."""
from scraper.dedup import IdentityResolver, resolve_identity


def _rec(**overrides):
    base = {
        "business_name": "Smile Dental",
        "website": "https://www.smiledental.com",
        "phone": "214-555-1234",
        "city": "Dallas",
        "place_id": "0x1",
    }
    base.update(overrides)
    return base


class TestResolveIdentity:
    def test_place_id_is_strongest(self):
        sig = resolve_identity(_rec(place_id="0xPLACE1"))
        assert sig["key_type"] == "place_id"

    def test_domain_city_fallback(self):
        sig = resolve_identity(_rec(place_id=None))
        assert sig["key_type"] == "domain+city"

    def test_phone_fallback(self):
        sig = resolve_identity(_rec(place_id=None, website=None))
        assert sig["key_type"] == "phone"


class TestIdentityResolver:
    def test_first_record_wins_same_place_id(self):
        r = IdentityResolver()
        dup, reason, _ = r.is_duplicate(_rec())
        assert dup is False
        dup, reason, _ = r.is_duplicate(_rec())
        assert dup is True
        assert "place_id" in reason or "identity" in reason

    def test_same_domain_different_city_is_not_duplicate(self):
        r = IdentityResolver()
        r.is_duplicate(_rec(city="Dallas", place_id=None))
        # A different branch in another city with the same domain is a new business.
        dup, reason, _ = r.is_duplicate(_rec(city="Houston", place_id=None,
                                             phone="214-555-9999"))
        assert dup is False

    def test_same_domain_same_city_is_duplicate(self):
        r = IdentityResolver()
        r.is_duplicate(_rec(city="Dallas", place_id=None))
        dup, reason, _ = r.is_duplicate(_rec(city="Dallas", place_id=None,
                                             phone="214-555-0000"))
        assert dup is True

    def test_same_phone_is_duplicate(self):
        r = IdentityResolver()
        r.is_duplicate(_rec(place_id=None, website=None))
        dup, _, _ = r.is_duplicate(_rec(place_id=None, website=None,
                                        city="Houston"))
        assert dup is True
