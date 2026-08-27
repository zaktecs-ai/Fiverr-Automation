"""Tests for tech detection (regex fallback path; wappalyzer may be absent)."""
from scraper.websites.tech_detect import TechDetector


class TestTechDetector:
    def test_wordpress_fallback(self):
        d = TechDetector(use_wappalyzer=False)
        joined, techs = d.detect("https://x.com", '<link href="/wp-content/style.css">')
        assert "WordPress" in joined

    def test_gtm_fallback(self):
        d = TechDetector(use_wappalyzer=False)
        joined, techs = d.detect("https://x.com",
                                 '<script src="https://www.googletagmanager.com/gtm.js"></script>')
        assert "Google Tag Manager" in joined

    def test_classify_sets_columns(self):
        d = TechDetector(use_wappalyzer=False)
        _, techs = d.detect("https://x.com", "wp-content")
        cols = d.classify(techs)
        assert cols["cms"] == "WordPress"

    def test_empty_html(self):
        d = TechDetector(use_wappalyzer=False)
        joined, techs = d.detect("https://x.com", "")
        assert joined == ""
        assert techs == set()
        assert d.classify(techs)["cms"] == "N/A"
