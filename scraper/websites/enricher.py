"""Website enrichment orchestrator.

Coordinates:
  fetch (HTTP-first) -> escalate to Playwright when needed -> smart crawl ->
  aggregate page context -> email extraction -> tech detection -> signal
  detection -> social link detection -> compose an enriched record dict.

This is the single seam the pipeline calls; it returns a fully-populated
enrichment dict that the pipeline merges onto a BusinessRecord.
"""
from __future__ import annotations

import logging
import re

from ..email.extract import clean_emails, extract_emails
from ..models import FailureReason
from ..signals.detector import PageContext, SignalDetector
from ..utils.normalize import normalize_url
from .crawler import SmartCrawler
from .fetcher import FetchResult, WebsiteFetcher
from .tech_detect import TechDetector

log = logging.getLogger(__name__)

# Official social platform URL patterns.
_SOCIAL_PATTERNS = {
    "facebook": re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/", re.I),
    "linkedin": re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/", re.I),
    "youtube": re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/", re.I),
    "twitter_x": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/", re.I),
    "pinterest": re.compile(r"(?:https?://)?(?:www\.)?pinterest\.com/", re.I),
}


def detect_social_links(html: str, urls: list[str]) -> dict[str, str]:
    """Return major social profile URLs (or 'N/A') from HTML + discovered URLs."""
    out = {k: "N/A" for k in _SOCIAL_PATTERNS}
    hay = "\n".join(urls or []) + "\n" + (html or "")
    for platform, pat in _SOCIAL_PATTERNS.items():
        m = pat.search(hay)
        if m:
            # Extract the full URL if possible.
            url_m = re.search(rf"(?:https?://)?(?:www\.)?[^\s\"'>]*?(?:{platform}|{'tiktok' if platform=='tiktok' else ''})[^\s\"'>]*", hay, re.I)
            out[platform] = url_m.group(0) if url_m else "YES"
    return out


def _has_required_email_or_contact(text_by_url: dict) -> bool:
    """Crawl stopping heuristic: stop once an email or contact info is present."""
    joined = " ".join(text_by_url.values())
    return bool(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", joined)) and \
        bool(re.search(r"(contact|email|tel:|@)", joined, re.I))


class WebsiteEnricher:
    def __init__(self, cfg: dict, fetcher: WebsiteFetcher | None = None,
                 tech: TechDetector | None = None, signals: SignalDetector | None = None,
                 browser_manager=None):
        site = cfg.get("website", {})
        self._cfg = cfg
        self._fetcher = fetcher or WebsiteFetcher(
            connect_timeout=site.get("http_connect_timeout_seconds", 10.0),
            read_timeout=site.get("http_read_timeout_seconds", 20.0),
            retries=site.get("http_retries", 1),
        )
        self._tech = tech or TechDetector(use_wappalyzer=site.get("use_wappalyzer", True))
        self._signals = signals or SignalDetector(cfg.get("signals", {}))
        self._bm = browser_manager
        self._playwright_enabled = site.get("enable_playwright_fallback", True)
        self._max_pages = site.get("max_pages_per_site", 8)
        self._overall_timeout = site.get("overall_site_timeout_seconds", 120.0)
        self._enable_sitemap = site.get("enable_sitemap", True)

    def enrich(self, website: str) -> dict:
        """Enrich a single website; returns a dict of website-intelligence fields."""
        out = {
            "website_status": "LIVE", "website_failure_reason": "",
            "emails": "N/A", "email_count": 0,
            "facebook": "N/A", "instagram": "N/A", "linkedin": "N/A",
            "youtube": "N/A", "twitter_x": "N/A", "tiktok": "N/A", "pinterest": "N/A",
            "tech_stack": "N/A", "cms": "N/A", "analytics": "N/A", "tag_manager": "N/A",
            "meta_pixel": "NO", "ga4": "NO", "gtm": "NO", "advertising": "N/A",
            "booking_system": "NO", "chat_widget": "NO", "ssl": "N/A",
            "signal_pricing": "NO", "signal_financing": "NO",
            "signal_licensed_insured": "NO", "signal_established": "NO",
            "signal_portfolio": "NO", "signal_mobile_service": "NO",
            "signal_membership": "NO",
        }
        if not website or website in (None, "", "N/A"):
            out["website_status"] = "N/A"
            out["website_failure_reason"] = "no_website"
            return out

        url = normalize_url(website)
        if url == "N/A":
            out["website_status"] = "N/A"
            out["website_failure_reason"] = "invalid_url"
            return out

        def fetch_fn(u: str) -> FetchResult:
            fr = self._fetcher.fetch(u)
            if fr.failure_reason and not fr.html and self._playwright_enabled and self._bm:
                fr = self._playwright_fetch(u)
            return fr

        crawler = SmartCrawler(max_pages=self._max_pages,
                               overall_timeout=self._overall_timeout,
                               enable_sitemap=self._enable_sitemap)
        result = crawler.crawl(url, fetch_fn, _has_required_email_or_contact)

        # Aggregate page context.
        all_html = "\n".join(result.html_by_url.values())
        all_text = "\n".join(result.text_by_url.values())
        all_urls = result.urls_seen
        headers = result.headers or {}

        emails = clean_emails(extract_emails(all_html, rendered_text=all_text),
                              self._cfg.get("email", {}).get("max_email_length", 120))
        out["emails"] = ", ".join(emails) if emails else "N/A"
        out["email_count"] = len(emails)

        soc = detect_social_links(all_html, all_urls)
        out.update(soc)

        tech_str, tech_set = self._tech.detect(url, all_html, headers)
        out["tech_stack"] = tech_str or "N/A"
        out.update(self._tech.classify(tech_set))
        out["ssl"] = "YES" if url.startswith("https://") else self._ssl_from_headers(headers)

        ctx = PageContext(text=all_text, html=all_html, url=url, urls=all_urls,
                          scripts=self._extract_scripts(all_html), meta={},
                          technologies=tech_set, structured=self._extract_jsonld(all_html))
        outcomes, evidence = self._signals.run(ctx)
        for k, v in outcomes.items():
            if k in out:
                out[k] = v
        # Attach evidence for key signals (best-effort, kept internally).
        out["_evidence"] = evidence

        # Final status determination.
        if result.pages_crawled == 0:
            # No page was fetched at all.
            reason = result.last_failure_reason or FailureReason.UNKNOWN
            out["website_status"] = "DEAD" if reason in (
                FailureReason.DNS_FAILURE, FailureReason.CONNECTION_REFUSED,
                FailureReason.NOT_FOUND) else "LIVE"
            out["website_failure_reason"] = reason
        else:
            out["website_status"] = "LIVE"
            out["website_failure_reason"] = ""
        return out

    def _playwright_fetch(self, url: str) -> FetchResult:
        """Escalate to Playwright for a JS-required/blocked page."""
        try:
            ctx = self._bm.new_context()
            page = ctx.new_page()
            page.set_default_timeout(self._cfg.get("website", {}).get(
                "page_navigation_timeout_seconds", 30.0) * 1000)
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=self._cfg.get("website", {}).get(
                              "page_navigation_timeout_seconds", 30.0) * 1000)
                html = page.content()
                text = page.inner_text("body")
                return FetchResult(url=url, status_code=200, html=html, text=text,
                                   headers={}, final_url=page.url,
                                   website_status="LIVE", failure_reason="",
                                   used_playwright=True)
            finally:
                try:
                    page.close()
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            log.debug("playwright fallback failed %s: %s", url, e)
            return FetchResult(url=url, website_status="LIVE",
                               failure_reason=FailureReason.JS_REQUIRED)

    def _extract_scripts(self, html: str) -> list[str]:
        return re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html or "")

    def _extract_jsonld(self, html: str) -> str:
        blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                            html or "", re.S)
        return "\n".join(blocks)

    def _ssl_from_headers(self, headers: dict) -> str:
        # Usually derivable from URL scheme; keep 'N/A' as a safe fallback.
        return "N/A"
