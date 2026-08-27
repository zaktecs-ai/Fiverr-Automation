"""Tests for tech detection (regex fallback path; wappalyzer may be absent)."""
import pytest

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


class TestWappalyzerIntegration:
    """Real wappalyzer-python3 integration (skipped if the lib isn't installed)."""

    @pytest.fixture
    def wappalyzer_available(self):
        try:
            import Wappalyzer  # noqa: F401
            return True
        except ImportError:
            return False

    def test_detects_wordpress_from_html(self, wappalyzer_available):
        if not wappalyzer_available:
            pytest.skip("wappalyzer-python3 not installed")
        d = TechDetector(use_wappalyzer=True)
        html = ('<html><head>'
                '<meta name="generator" content="WordPress 6.4">'
                '<script src="https://www.googletagmanager.com/gtag/js"></script>'
                '</head><body></body></html>')
        joined, techs = d.detect("https://example.com", html, {"server": "nginx"})
        lowered = joined.lower()
        assert "wordpress" in lowered
        assert "google tag manager" in lowered

    def test_classify_maps_cms(self, wappalyzer_available):
        if not wappalyzer_available:
            pytest.skip("wappalyzer-python3 not installed")
        d = TechDetector(use_wappalyzer=True)
        html = '<meta name="generator" content="WordPress 6.4">'
        _, techs = d.detect("https://example.com", html)
        assert d.classify(techs)["cms"] == "WordPress"
