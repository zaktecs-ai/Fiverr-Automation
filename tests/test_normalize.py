"""Tests for URL / phone / email normalization and cleaning."""
from scraper.utils.normalize import (
    canonical_domain, extract_domain, is_usable_email, normalize_email,
    normalize_phone, normalize_text, normalize_url, email_rejection_reason,
)


class TestURLNormalize:
    def test_strips_tracking_params(self):
        assert normalize_url("https://www.example.com/?utm_source=test") == \
            "https://example.com"

    def test_strips_default_ports_and_scheme_case(self):
        assert normalize_url("HTTPS://Example.COM:443/") == "https://example.com"

    def test_preserves_meaningful_path(self):
        assert normalize_url("https://example.com/services/teeth-whitening") == \
            "https://example.com/services/teeth-whitening"

    def test_trailing_slash_removed(self):
        assert normalize_url("https://example.com/about/") == "https://example.com/about"

    def test_adds_scheme_when_missing(self):
        assert normalize_url("www.example.com") == "https://example.com"

    def test_google_redirect_unwrapped(self):
        url = "https://www.google.com/url?q=https://realsite.com/contact"
        assert normalize_url(url) == "https://realsite.com/contact"

    def test_non_http_scheme_returns_na(self):
        assert normalize_url("mailto:foo@bar.com") == "N/A"
        assert normalize_url("") == "N/A"


class TestDomain:
    def test_registrable_domain(self):
        assert canonical_domain("www.example.com") == "example.com"

    def test_country_tld(self):
        assert canonical_domain("sub.example.co.uk") == "example.co.uk"

    def test_known_private_suffix(self):
        assert canonical_domain("foo.github.io") == "foo.github.io"

    def test_extract_domain_from_url(self):
        assert extract_domain("https://www.example.com/about") == "example.com"


class TestPhoneNormalize:
    def test_north_american_equivalence(self):
        a = normalize_phone("+1 (214) 555-1234")
        b = normalize_phone("214-555-1234")
        c = normalize_phone("(214) 555-1234")
        d = normalize_phone("2145551234")
        # All should share the same digits tail.
        assert a.endswith("2145551234")
        assert b in a or a in b
        assert c.endswith("2145551234")
        assert d.endswith("2145551234")

    def test_empty_returns_na(self):
        assert normalize_phone("") == "N/A"
        assert normalize_phone(None) == "N/A"

    def test_strips_non_digits(self):
        assert "2145551234" in normalize_phone("(214) 555-1234")


class TestNormalizeText:
    def test_collapse_whitespace(self):
        assert normalize_text("  Hello   World \n ") == "Hello World"

    def test_na_for_empty(self):
        assert normalize_text("") == "N/A"
        assert normalize_text(None) == "N/A"


class TestEmailCleaning:
    def test_valid_email(self):
        assert is_usable_email("john@business.com")

    def test_dummy_domain_rejected(self):
        assert not is_usable_email("john@example.com")

    def test_dummy_local_rejected(self):
        assert not is_usable_email("info@realdomain.com")

    def test_invalid_syntax_rejected(self):
        assert not is_usable_email("not-an-email")
        assert not is_usable_email("john@domain")

    def test_asset_path_rejected(self):
        assert not is_usable_email("image@example.com.png")

    def test_too_long_rejected(self):
        assert not is_usable_email("a" * 200 + "@domain.com", max_length=120)

    def test_rejection_reason_explicit(self):
        assert email_rejection_reason("x@example.com") == "dummy_domain"

    def test_normalize_lowercase(self):
        assert normalize_email("  JOHN@Example.COM ") == "john@example.com"
