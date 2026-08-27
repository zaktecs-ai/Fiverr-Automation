"""Tests for record validation and website status classification."""
from scraper.models import FailureReason, WebsiteStatus, resolve_website_status
from scraper.validation import validate_record, validate_website_status


class TestStatusClassification:
    def test_transient_reason_is_live(self):
        assert resolve_website_status(FailureReason.HTTP_BLOCKED) == WebsiteStatus.LIVE
        assert resolve_website_status(FailureReason.CAPTCHA_DETECTED) == WebsiteStatus.LIVE
        assert resolve_website_status(FailureReason.JS_REQUIRED) == WebsiteStatus.LIVE
        assert resolve_website_status(FailureReason.TIMEOUT) == WebsiteStatus.LIVE

    def test_dead_reasons_are_dead(self):
        assert resolve_website_status(FailureReason.DNS_FAILURE) == WebsiteStatus.DEAD
        assert resolve_website_status(FailureReason.CONNECTION_REFUSED) == WebsiteStatus.DEAD
        assert resolve_website_status(FailureReason.NOT_FOUND) == WebsiteStatus.DEAD

    def test_detects_contradiction(self):
        rec = {"website_status": "DEAD",
               "website_failure_reason": FailureReason.HTTP_BLOCKED}
        ok, _ = validate_website_status(rec)
        assert ok is False

    def test_consistent_status_ok(self):
        rec = {"website_status": "LIVE", "website_failure_reason": "HTTP_BLOCKED"}
        ok, _ = validate_website_status(rec)
        assert ok is True


class TestValidateRecord:
    def _full(self, **overrides):
        from scraper.models import OUTPUT_COLUMNS
        rec = {c: "N/A" for c in OUTPUT_COLUMNS}
        rec.update({
            "business_name": "Smile Dental",
            "source_query": "dentists in Dallas, TX",
            "website": "https://smiledental.com",
            "phone": "2145551234",
            "emails": "hello@smiledental.com",
        })
        rec.update(overrides)
        return rec

    def test_valid_record(self):
        ok, problems = validate_record(self._full())
        assert ok is True, problems

    def test_malformed_url(self):
        ok, problems = validate_record(self._full(website="not a url"))
        assert ok is False
        assert any("malformed website" in p for p in problems)

    def test_invalid_email(self):
        ok, problems = validate_record(self._full(emails="not-an-email"))
        assert ok is False

    def test_require_website_enforced(self):
        ok, problems = validate_record(self._full(website="N/A"), require_website=True)
        assert ok is False
        assert any("no website" in p for p in problems)

    def test_contradiction_caught(self):
        ok, _ = validate_record(self._full(website_status="DEAD",
                                           website_failure_reason="HTTP_BLOCKED"))
        assert ok is False
