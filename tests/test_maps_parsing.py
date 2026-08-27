"""Tests for the pure Maps parsing helpers (no browser required)."""
from scraper.maps.collector import (
    decompose_address, detect_bot_challenge, parse_rating_reviews,
    split_source_location,
)


class TestParseRatingReviews:
    def test_rating_and_paren_reviews(self):
        rating, reviews = parse_rating_reviews("Smile Dental 4.8 (365)")
        assert rating == 4.8
        assert reviews == 365

    def test_rating_and_word_reviews(self):
        rating, reviews = parse_rating_reviews("4.5 1,200 reviews")
        assert rating == 4.5
        assert reviews == 1200

    def test_rating_only(self):
        rating, reviews = parse_rating_reviews("3.9")
        assert rating == 3.9
        assert reviews is None

    def test_empty(self):
        assert parse_rating_reviews("") == (None, None)
        assert parse_rating_reviews(None) == (None, None)

    def test_invalid_rating_range_ignored(self):
        # A "9.9" should not parse as a rating (Maps ratings are 1-5).
        rating, _ = parse_rating_reviews("Some text 9.9 and more")
        assert rating is None


class TestDecomposeAddress:
    def test_full_address(self):
        out = decompose_address("123 Main St, Dallas, TX 75201")
        assert out["city"] == "Dallas"
        assert out["state"] == "TX"
        assert out["postal_code"] == "75201"

    def test_zip_with_plus4(self):
        out = decompose_address("123 Main St, Austin, TX 78701-1234")
        assert out["postal_code"] == "78701"

    def test_missing_parts_are_na(self):
        out = decompose_address("123 Main St")
        assert out["city"] == "N/A"
        assert out["state"] == "N/A"
        assert out["postal_code"] == "N/A"

    def test_empty(self):
        out = decompose_address("")
        assert out["city"] == "N/A"


class TestDetectBotChallenge:
    def test_unusual_traffic(self):
        assert detect_bot_challenge(
            "<html>Our systems have detected unusual traffic from your computer network</html>")

    def test_captcha_redirect(self):
        assert detect_bot_challenge('<form action="CaptchaRedirect">')

    def test_recaptcha(self):
        assert detect_bot_challenge('<div class="g-recaptcha"></div>')

    def test_clean_page(self):
        assert detect_bot_challenge("<html>dentist listings</html>") is False

    def test_empty(self):
        assert detect_bot_challenge("") is False
        assert detect_bot_challenge(None) is False


class TestSplitSourceLocation:
    def test_with_in(self):
        kw, loc = split_source_location("dentists in Dallas, TX")
        assert kw == "dentists"
        assert loc == "Dallas, TX"

    def test_without_in(self):
        kw, loc = split_source_location("plumbers")
        assert kw == "plumbers"
        assert loc == "N/A"

    def test_niche_with_spaces(self):
        kw, loc = split_source_location("luxury pool designers in Austin, TX")
        assert kw == "luxury pool designers"
        assert loc == "Austin, TX"
