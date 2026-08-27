"""Tests for the enriched email filtering (legacy-proven heuristics)."""
from scraper.utils.normalize import (
    email_rejection_reason, is_usable_email,
)


class TestDomainRelationship:
    def test_matching_domain_ok(self):
        assert is_usable_email("hello@smiledental.com",
                               website_url="https://smiledental.com")

    def test_public_provider_exempt_from_mismatch(self):
        # Gmail is a legitimate contact even though it's not the website domain.
        assert is_usable_email("drjane@gmail.com",
                               website_url="https://smiledental.com")

    def test_unrelated_domain_with_suspicious_word_rejected(self):
        assert not is_usable_email("contact@template-site.com",
                                   website_url="https://smiledental.com")

    def test_unrelated_domain_clean_is_kept(self):
        # A different-but-plausible domain without suspicious words is kept
        # (conservative — do not over-reject).
        assert is_usable_email("frontdesk@partnerclinic.com",
                               website_url="https://smiledental.com")


class TestDisposableDomains:
    def test_disposable_rejected(self):
        assert not is_usable_email("x@mailinator.com")
        assert not is_usable_email("x@10minutemail.com")

    def test_rejection_reason_disposable(self):
        assert email_rejection_reason("x@yopmail.com") == "disposable_domain"


class TestNoWebsiteContext:
    def test_usable_without_website(self):
        # Without website context, no domain-relationship check occurs.
        assert is_usable_email("jane.doe@realbusiness.net")

    def test_dummy_domain_still_rejected(self):
        assert not is_usable_email("x@example.com")
