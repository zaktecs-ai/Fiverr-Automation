"""Tests for the filter engine."""
from scraper.filters import FilterEngine


def _rec(review_count=20, rating=4.5, website="https://x.com", emails="ok@x.com",
         meta_pixel="YES", ga4="YES", tag_manager="YES"):
    return {
        "website": website,
        "review_count": review_count,
        "rating": rating,
        "emails": emails,
        "meta_pixel": meta_pixel,
        "ga4": ga4,
        "tag_manager": tag_manager,
    }


class TestFilterEngine:
    def test_include_all_passes(self):
        e = FilterEngine({"include_all": [
            {"field": "website", "op": "=", "value": "yes"},
            {"field": "review_count", "op": ">=", "value": 15},
        ]})
        ok, _ = e.evaluate(_rec())
        assert ok is True

    def test_include_all_fails(self):
        e = FilterEngine({"include_all": [
            {"field": "review_count", "op": ">=", "value": 50},
        ]})
        ok, reason = e.evaluate(_rec(review_count=20))
        assert ok is False
        assert reason == "failed_include_all"

    def test_include_any(self):
        e = FilterEngine({"include_any": [
            {"field": "meta_pixel", "op": "=", "value": "yes"},
            {"field": "ga4", "op": "=", "value": "yes"},
        ]})
        assert e.evaluate(_rec(meta_pixel="NO", ga4="YES"))[0] is True
        assert e.evaluate(_rec(meta_pixel="NO", ga4="NO"))[0] is False

    def test_exclude_any(self):
        e = FilterEngine({"exclude_any": [
            {"field": "review_count", "op": "<", "value": 10},
        ]})
        assert e.evaluate(_rec(review_count=5))[0] is False
        assert e.evaluate(_rec(review_count=30))[0] is True

    def test_website_yes_requires_website(self):
        e = FilterEngine({"include_all": [{"field": "website", "op": "=", "value": "yes"}]})
        assert e.evaluate(_rec(website="N/A"))[0] is False
        assert e.evaluate(_rec(website="https://x.com"))[0] is True

    def test_email_found(self):
        e = FilterEngine({"include_all": [{"field": "email_found", "op": "=", "value": "yes"}]})
        assert e.evaluate(_rec(emails="N/A"))[0] is False
        assert e.evaluate(_rec(emails="a@b.com"))[0] is True

    def test_rating_comparison_coerces_floats(self):
        e = FilterEngine({"include_all": [{"field": "rating", "op": ">=", "value": 4.0}]})
        assert e.evaluate(_rec(rating="4.7"))[0] is True
        assert e.evaluate(_rec(rating="3.2"))[0] is False
