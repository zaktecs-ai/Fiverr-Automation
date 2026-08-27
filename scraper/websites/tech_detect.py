"""Technology-stack detection.

Primary strategy: use the maintained `wappalyzer` library if available
(Wappalyzer-compatible, open source). Because it must never be a hard
dependency, a built-in regex fallback covers the most common technologies so
the engine degrades gracefully. Results are aggregated into a single readable
string plus an optional set of notable tech names.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Lightweight fallback signatures: tech name -> list of regex patterns (html or headers).
_FALLBACK_SIGNATURES: list[tuple[str, list[str]]] = [
    ("WordPress",         [r"wp-content", r"wp-includes", r"wp-json"]),
    ("Shopify",           [r"cdn\.shopify\.com", r"myshopify\.com"]),
    ("Wix",               [r"static\.wixstatic\.com", r"wix\.com/"]),
    ("Squarespace",       [r"squarespace\.com", r"static1\.squarespace\.com"]),
    ("Webflow",           [r"webflow\.com", r"webflow\.js"]),
    ("Elementor",         [r"elementor"]),
    ("Joomla",            [r"joomla"]),
    ("Drupal",            [r"drupal"]),
    ("Google Tag Manager", [r"googletagmanager\.com"]),
    ("Google Analytics",  [r"google-analytics\.com", r"gtag\("]),
    ("Cloudflare",        [r"cloudflare", r"cf-ray"]),
    ("React",             [r"react(?:\.min)?\.js", r"__REACT_DEVTOOLS"]),
    ("Vue.js",            [r"vue(?:\.min)?\.js", r"__VUE__"]),
    ("Angular",           [r"angular(?:\.min)?\.js", r"ng-version"]),
    ("Next.js",           [r"__NEXT_DATA__", r"/_next/static"]),
    ("Bootstrap",         [r"bootstrap(?:\.min)?\.css", r"bootstrap(?:\.min)?\.js"]),
    ("jQuery",            [r"jquery(?:\.min)?\.js"]),
    ("Google Fonts",      [r"fonts\.googleapis\.com"]),
    ("Font Awesome",      [r"font-awesome", r"fontawesome"]),
    ("Stripe",            [r"js\.stripe\.com"]),
    ("PayPal",            [r"paypal\.com", r"paypalobjects\.com"]),
    ("HubSpot",           [r"hubspot", r"js\.hs-scripts\.com"]),
    ("Tawk.to",           [r"tawk\.to"]),
    ("Intercom",          [r"intercom"]),
    ("Calendly",          [r"calendly\.com"]),
    ("Acuity Scheduling", [r"acuityscheduling\.com"]),
    ("WooCommerce",       [r"woocommerce"]),
    ("BigCommerce",       [r"bigcommerce"]),
    ("Magento",           [r"magento"]),
    ("Django",            [r"csrftoken", r"django"]),
    ("Ruby on Rails",     [r"csrf-param", r"rails"]),
]


def _fallback_detect(html: str, headers: dict[str, str]) -> list[str]:
    text = html or ""
    hdr = " ".join(f"{k}: {v}" for k, v in (headers or {}).items())
    hay = text + " " + hdr
    found: list[str] = []
    for name, patterns in _FALLBACK_SIGNATURES:
        for pat in patterns:
            if re.search(pat, hay, re.I):
                found.append(name)
                break
    return found


def _wappalyzer_detect(url: str, html: str, headers: dict[str, str]) -> list[str]:
    """Use the wappalyzer library if importable. Never raises on failure."""
    try:
        from wappalyzer import Wappalyzer, WebPage  # type: ignore
        from wappalyzer.wappalyzer import logger as wz_logger
        wz_logger.disabled = True

        analyzer = Wappalyzer.latest()
        # Build a WebPage-like object: it expects html, headers, url.
        wp = WebPage(url=url, html=html, headers=headers or {})
        techs = analyzer.analyze(wp)
        return list(techs)
    except Exception as e:  # pragma: no cover - library/env dependent
        log.debug("wappalyzer unavailable or failed: %s", e)
        return []


class TechDetector:
    """Unified tech detection: prefer wappalyzer, fall back to regex."""

    def __init__(self, use_wappalyzer: bool = True):
        self._use_wappalyzer = use_wappalyzer

    def detect(self, url: str, html: str, headers: dict[str, str] | None = None) -> tuple[str, set[str]]:
        headers = headers or {}
        techs: list[str] = []
        if self._use_wappalyzer:
            techs = _wappalyzer_detect(url, html, headers)
        if not techs:
            techs = _fallback_detect(html, headers)
        # De-duplicate preserving order.
        seen: set[str] = set()
        ordered = []
        for t in techs:
            if t.lower() not in seen:
                seen.add(t.lower())
                ordered.append(t)
        joined = ", ".join(ordered)
        return joined, set(ordered)

    @staticmethod
    def classify(tech_set: set[str]) -> dict[str, str]:
        """Map a tech set to individual output columns (cms/analytics/etc.)."""
        def has(*names):
            return any(n.lower() in {t.lower() for t in tech_set} for n in names)

        cms = ""
        for candidate in ("WordPress", "Shopify", "Wix", "Squarespace", "Webflow",
                          "Joomla", "Drupal", "Magento", "BigCommerce", "WooCommerce"):
            if has(candidate):
                cms = candidate
                break
        return {
            "cms": cms or "N/A",
            "analytics": "Google Analytics" if has("Google Analytics") else "N/A",
            "tag_manager": "Google Tag Manager" if has("Google Tag Manager") else "N/A",
        }
