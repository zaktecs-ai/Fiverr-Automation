"""Tests for the signal detection engine (built-in + custom)."""
from scraper.signals import SignalDetector
from scraper.signals.detector import PageContext


def _ctx(text="", html="", scripts=None, urls=None, tech=None):
    return PageContext(text=text, html=html, scripts=scripts or [],
                       urls=urls or [], technologies=tech or set())


class TestBuiltInSignals:
    def test_meta_pixel_detected(self):
        d = SignalDetector()
        out, ev = d.run(_ctx(html='<script>fbq("track")</script>'))
        assert out["meta_pixel"] == "YES"
        assert ev.get("meta_pixel")

    def test_ga4_detected(self):
        d = SignalDetector()
        out, _ = d.run(_ctx(html='<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>'))
        assert out["ga4"] == "YES"

    def test_gtm_detected(self):
        d = SignalDetector()
        out, _ = d.run(_ctx(html='googletagmanager.com/gtm.js?id=GTM-XYZ'))
        assert out["gtm"] == "YES"

    def test_no_signal_is_no(self):
        d = SignalDetector()
        out, _ = d.run(_ctx(text="plain page with nothing"))
        assert out["meta_pixel"] == "NO"
        assert out["gtm"] == "NO"

    def test_established_from_text(self):
        d = SignalDetector()
        out, ev = d.run(_ctx(text="Family owned since 1987"))
        assert out["signal_established"] == "YES"
        assert ev.get("established")


class TestCustomSignals:
    def test_custom_any_keyword(self):
        d = SignalDetector({"family_owned": {
            "enabled": True, "keywords": ["family owned"], "match_logic": "ANY"}})
        out, ev = d.run(_ctx(text="We are a family owned business"))
        assert out["signal_family_owned"] == "YES"
        assert "family owned" in ev.get("family_owned", "")

    def test_custom_regex(self):
        d = SignalDetector({"established": {
            "enabled": True, "regex": [r"since\s+(19|20)\d{2}"], "match_logic": "ANY"}})
        out, ev = d.run(_ctx(text="In business since 2001"))
        assert out["signal_established"] == "YES"

    def test_custom_all_logic_requires_every_group(self):
        d = SignalDetector({"both": {
            "enabled": True, "keywords": ["licensed"], "regex": [r"insured"],
            "match_logic": "ALL"}})
        out, _ = d.run(_ctx(text="licensed and insured"))
        assert out["signal_both"] == "YES"
        out2, _ = d.run(_ctx(text="licensed only"))
        assert out2["signal_both"] == "NO"

    def test_disabled_signal_skipped(self):
        d = SignalDetector({"off": {"enabled": False, "keywords": ["x"]}})
        out, _ = d.run(_ctx(text="x"))
        assert "signal_off" not in out
