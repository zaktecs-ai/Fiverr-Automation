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

    def test_distinct_place_id_same_phone_not_merged(self):
        # A franchise front-desk line shared by two locations must not merge
        # them: distinct place_ids are authoritative evidence of distinct
        # listings. (Regression: previously a shared phone falsely merged them.)
        r = IdentityResolver()
        dup, _, _ = r.is_duplicate(_rec(place_id="0xAAA", website=None,
                                        phone="214-555-0000"))
        assert dup is False
        dup, reason, _ = r.is_duplicate(_rec(place_id="0xBBB", website=None,
                                             phone="214-555-0000"))
        assert dup is False

    def test_distinct_place_id_same_domain_city_not_merged(self):
        # Two branches of a chain in the SAME city sharing one corporate domain
        # are distinct listings when their place_ids differ.
        r = IdentityResolver()
        dup, _, _ = r.is_duplicate(_rec(place_id="0xCCC", city="Dallas"))
        assert dup is False
        dup, reason, _ = r.is_duplicate(_rec(place_id="0xDDD", city="Dallas",
                                             phone="214-555-0001"))
        assert dup is False

    def test_place_id_record_does_not_poison_phone_fallback(self):
        # A place_id-less record sharing a phone with an earlier place_id record
        # must not be merged: the shared phone is weak evidence, and the
        # place_id-bearing record was already uniquely identified.
        r = IdentityResolver()
        r.is_duplicate(_rec(place_id="0xEEE", website=None))
        dup, _, _ = r.is_duplicate(_rec(place_id=None, website=None,
                                        city="Houston"))
        assert dup is False
