"""Tests for email extraction and cleaning."""
from scraper.email.extract import (
    clean_emails, extract_emails, extract_emails_from_text,
)


class TestExtract:
    def test_mailto_priority(self):
        html = '<a href="mailto:hello@business.com">Contact</a>'
        out = extract_emails(html)
        assert "hello@business.com" in out

    def test_jsonld(self):
        html = ('<script type="application/ld+json">'
                '{"email": "info@business.com"}</script>')
        assert "info@business.com" in extract_emails(html)

    def test_visible_text(self):
        html = "<p>Reach us at sales@business.com or support@business.com</p>"
        out = extract_emails(html)
        assert "sales@business.com" in out
        assert "support@business.com" in out

    def test_obfuscated_email_decoded(self):
        text = "contact [at] business [dot] com"
        assert "contact@business.com" in extract_emails_from_text(text)

    def test_rendered_text_source(self):
        out = extract_emails("", rendered_text="hi team@x.com")
        assert "team@x.com" in out


class TestClean:
    def test_dedup_and_dummy_filtered(self):
        candidates = ["a@x.com", "a@x.com", "b@example.com", "info@x.com"]
        out = clean_emails(candidates)
        # "info@x.com" is a legitimate role address (kept); "b@example.com" is a
        # dummy domain (rejected); the duplicate "a@x.com" is deduped.
        assert out == ["a@x.com", "info@x.com"]

    def test_empty(self):
        assert clean_emails([]) == []
