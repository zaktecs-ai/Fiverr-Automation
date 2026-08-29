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
import random
import re
import threading

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


# Each platform maps to a domain-anchored extraction pattern. Extraction is
# anchored at the platform's own domain so a link can never be attributed to
# the wrong social column (the previous unanchored regex matched any URL whose
# tail looked like another platform's name).
_SOCIAL_DOMAIN_PATTERNS = {
    "facebook": re.compile(r"(https?://)?(?:www\.)?facebook\.com/[^\s\"'<>]*", re.I),
    "instagram": re.compile(r"(https?://)?(?:www\.)?instagram\.com/[^\s\"'<>]*", re.I),
    "linkedin": re.compile(r"(https?://)?(?:www\.)?linkedin\.com/[^\s\"'<>]*", re.I),
    "youtube": re.compile(r"(https?://)?(?:www\.)?(youtube\.com|youtu\.be)/[^\s\"'<>]*", re.I),
    "twitter_x": re.compile(r"(https?://)?(?:www\.)?(twitter\.com|x\.com)/[^\s\"'<>]*", re.I),
    "tiktok": re.compile(r"(https?://)?(?:www\.)?tiktok\.com/[^\s\"'<>]*", re.I),
    "pinterest": re.compile(r"(https?://)?(?:www\.)?pinterest\.com/[^\s\"'<>]*", re.I),
}


def detect_social_links(html: str, urls: list[str]) -> dict[str, str]:
    """Return major social profile URLs (or 'N/A') from HTML + discovered URLs.

    Each platform is matched by its own domain-anchored pattern and the URL is
    extracted from that exact match, so a Facebook link never lands in the
    Instagram column (or vice-versa).
    """
    out = {k: "N/A" for k in _SOCIAL_PATTERNS}
    hay = "\n".join(urls or []) + "\n" + (html or "")
    for platform, pat in _SOCIAL_DOMAIN_PATTERNS.items():
        m = pat.search(hay)
        if m:
            url = m.group(0)
            # Keep a bare scheme-less match but prefer to add a scheme.
            out[platform] = url if "://" in url else "https://" + url
    return out


def _has_required_email_or_contact(text_by_url: dict) -> bool:
    """Crawl stopping heuristic: stop early once lead-valuable signals are found.

    General-purpose (category-agnostic): as soon as we have an email, OR contact
    info, OR a social profile, OR a decisiv booking/chat signal, we have enough
    to enrich — there's no need to keep crawling deep pages. This is what keeps
    the per-site crawl small without losing data quality.
    """
    joined = " ".join(text_by_url.values())
    if not joined.strip():
        return False
    # Email present -> enough (with or without a surrounding marker).
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", joined):
        return True
    # Social profile present -> strong signal on its own.
    if re.search(
        r"(facebook\.com|instagram\.com|linkedin\.com|youtube\.com|"
        r"(twitter\.com|x\.com)/|tiktok\.com|pinterest\.com)",
        joined, re.I):
        return True
    # Decisive business signals (booking/chat/financing — any category).
    if re.search(
        r"(calendly\.com|acuityscheduling\.com|booksy\.com|tawk\.to|intercom|"
        r"livechatinc\.com|setmore\.com|financing|payment plan|book now|book online)",
        joined, re.I):
        return True
    # Contact info (phone/tel/contact form) is a useful middle-tier signal.
    return bool(re.search(r"(contact|tel:|email us|call us)", joined, re.I))


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
        self._email_enabled = cfg.get("email", {}).get("enabled", True)
        # Site pacing (delay between site fetches) — was validated but never
        # applied; now enforced as a randomized sleep around each fetch.
        dl = cfg.get("delays", {})
        self._site_min = float(dl.get("site_min_seconds", 0.0))
        self._site_max = float(dl.get("site_max_seconds", 0.0))
        # Reusable lock so all worker threads share one pacing clock.
        self._sleep_lock = threading.Lock()
        self._last_fetch_ts = 0.0
        # Cap concurrent Playwright fallback browser tabs (was dead config).
        pw_workers = min(max(int(cfg.get("concurrency", {}).get(
            "playwright_workers", 2)), 1), 8)
        self._pw_sem = threading.Semaphore(pw_workers)

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
            return self._site_paced_fetch(u)

        crawler = SmartCrawler(max_pages=self._max_pages,
                               overall_timeout=self._overall_timeout,
                               enable_sitemap=self._enable_sitemap)
        result = crawler.crawl(url, fetch_fn, _has_required_email_or_contact)

        # Aggregate page context.
        all_html = "\n".join(result.html_by_url.values())
        all_text = "\n".join(result.text_by_url.values())
        all_urls = result.urls_seen
        headers = result.headers or {}

        # Gate email extraction behind `email.enabled` so disabling it actually
        # stops the emails/email_count columns from being populated.
        if self._email_enabled:
            raw_candidates = extract_emails(all_html, rendered_text=all_text)
            emails = clean_emails(raw_candidates,
                                  self._cfg.get("email", {}).get("max_email_length", 120),
                                  website_url=url)
            out["emails"] = ", ".join(emails) if emails else "N/A"
            out["email_count"] = len(emails)
            # Internal counter: candidates that were cleaned away, so the
            # pipeline can report emails_rejected (previously a dead stat).
            out["_emails_rejected"] = len(raw_candidates) - len(emails)
        else:
            out["emails"] = "N/A"
            out["email_count"] = 0
            out["_emails_rejected"] = 0

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

    def _site_paced_fetch(self, url: str) -> FetchResult:
        """Fetch with an optional randomized delay between site requests.

        Applies `delays.site_min_seconds`/`site_max_seconds` (previously dead
        config) while staying thread-safe across the enrichment pool.
        """
        import time as _time
        sleep_time = 0.0
        if self._site_max > 0:
            # Compute the required delay and update the shared clock UNDER the
            # lock, then sleep OUTSIDE it. Sleeping while holding the lock
            # serialized the whole enrichment thread pool and destroyed
            # concurrency (every worker waited on every other worker's pause).
            with self._sleep_lock:
                now = _time.time()
                if self._last_fetch_ts:
                    elapsed = now - self._last_fetch_ts
                    want = random.uniform(self._site_min, self._site_max)
                    if elapsed < want:
                        sleep_time = want - elapsed
                self._last_fetch_ts = _time.time()
        if sleep_time > 0:
            _time.sleep(sleep_time)
        fr = self._fetcher.fetch(url)
        if fr.failure_reason and not fr.html and self._playwright_enabled and self._bm:
            fr = self._playwright_fetch(url)
        return fr

    def _playwright_fetch(self, url: str) -> FetchResult:
        """Escalate to Playwright for a JS-required/blocked page."""
        try:
            ctx = self._bm.new_context()
            page = ctx.new_page()
            # Bound browser-tab concurrency to `concurrency.playwright_workers`.
            with self._pw_sem:
                return self._playwright_scrape(ctx, page, url)
        except Exception as e:
            log.debug("playwright fallback failed %s: %s", url, e)
            return FetchResult(url=url, website_status="LIVE",
                               failure_reason=FailureReason.JS_REQUIRED)

    def _playwright_scrape(self, ctx, page, url: str) -> FetchResult:
        """Run the browser fetch (page already opened) under the semaphore."""
        try:
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
