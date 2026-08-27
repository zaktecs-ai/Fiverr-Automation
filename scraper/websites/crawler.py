"""Smart website crawler: prioritized, bounded, sitemap-aware.

Crawls a SMALL, controlled number of relevant pages (in priority order) rather
than the whole site. Stops early once the "required" information is present, or
when page/time limits are hit. Uses HTTP-first and escalates to Playwright only
when HTTP is insufficient, via an injected fetch callback.

Priority order (configurable via `page_priority`):
    homepage, contact, about, services, team, locations, pricing/booking,
    then sitemap-derived relevant pages.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

_DEFAULT_PRIORITY = ["", "/contact", "/contact-us", "/about", "/about-us",
                     "/services", "/team", "/staff", "/our-team", "/locations",
                     "/pricing", "/book", "/booking", "/book-now"]

_KEYWORD_HINTS = {
    "contact": re.compile(r"(contact|get.?in.?touch|reach?.?us)", re.I),
    "about": re.compile(r"(about|our.?story)", re.I),
    "services": re.compile(r"(services|what.?we.?do|solutions)", re.I),
    "team": re.compile(r"(team|staff|our.?people)", re.I),
    "locations": re.compile(r"(location|office|find.?us)", re.I),
    "pricing": re.compile(r"(pricing|plans|book|booking|appointment|quote)", re.I),
}


@dataclass
class CrawlResult:
    pages_crawled: int = 0
    html_by_url: dict = field(default_factory=dict)
    text_by_url: dict = field(default_factory=dict)
    urls_seen: list = field(default_factory=list)
    stopped_reason: str = ""  # "complete" | "page_limit" | "timeout"
    last_failure_reason: str = ""   # authoritative reason when the homepage failed
    headers: dict = field(default_factory=dict)


class SmartCrawler:
    def __init__(self, *, max_pages: int = 8, overall_timeout: float = 120.0,
                 enable_sitemap: bool = True, page_priority: list[str] | None = None):
        self._max_pages = max_pages
        self._overall_timeout = overall_timeout
        self._enable_sitemap = enable_sitemap
        self._priority = page_priority or _DEFAULT_PRIORITY

    def crawl(self, base_url: str, fetch_fn: Callable[[str], object],
              has_required_fn: Callable[[dict], bool]) -> CrawlResult:
        """Crawl around `base_url`.

        fetch_fn(url) -> a FetchResult-like object with .html, .text, .final_url,
        .failure_reason. has_required_fn(accumulated_text_dict) -> bool.
        """
        import time as _t
        start = _t.monotonic()
        result = CrawlResult()
        origin = _origin(base_url)
        queue: list[str] = self._build_queue(base_url, fetch_fn)
        visited: set[str] = set()

        while queue and result.pages_crawled < self._max_pages:
            if _t.monotonic() - start > self._overall_timeout:
                result.stopped_reason = "timeout"
                break
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                fr = fetch_fn(url)
            except Exception as e:  # noqa: BLE001
                log.debug("crawl fetch error %s: %s", url, e)
                continue
            if getattr(fr, "failure_reason", "") and not getattr(fr, "html", ""):
                # Homepage failure is authoritative for classification.
                if url == base_url:
                    result.last_failure_reason = getattr(fr, "failure_reason", "")
                    result.headers = getattr(fr, "headers", {}) or {}
                continue

            html = getattr(fr, "html", "") or ""
            text = getattr(fr, "text", "") or html
            result.html_by_url[url] = html
            result.text_by_url[url] = text
            result.urls_seen.append(url)
            result.pages_crawled += 1
            if url == base_url:
                result.headers = getattr(fr, "headers", {}) or {}

            # Discover internal links to add to the queue.
            if result.pages_crawled < self._max_pages:
                for link in self._discover_links(html, origin):
                    if link not in visited and link not in queue:
                        queue.append(link)

            # Early stop if required info is satisfied.
            if has_required_fn(result.text_by_url):
                result.stopped_reason = "complete"
                break
        else:
            result.stopped_reason = "page_limit"

        if result.stopped_reason == "":
            result.stopped_reason = "complete"
        return result

    def _build_queue(self, base_url: str, fetch_fn) -> list[str]:
        origin = _origin(base_url)
        queue: list[str] = []
        # 1. Homepage first.
        queue.append(base_url)
        # 2. Common priority paths.
        for slug in self._priority:
            if not slug or slug == "/":
                continue
            queue.append(urljoin(base_url, slug))
        # 3. Sitemap-derived relevant URLs (targeted discovery).
        if self._enable_sitemap:
            sitemap_urls = self._discover_sitemap_urls(base_url, fetch_fn)
            queue.extend(sitemap_urls)
        # De-dupe, keep order, drop external URLs.
        seen: set[str] = set()
        deduped = []
        for u in queue:
            if u in seen:
                continue
            if _origin(u) != origin:
                continue
            seen.add(u)
            deduped.append(u)
        return deduped

    def _discover_sitemap_urls(self, base_url: str, fetch_fn) -> list[str]:
        """Fetch sitemap.xml / robots sitemap and return relevant internal URLs."""
        origin = _origin(base_url)
        candidates = [urljoin(base_url, "/sitemap.xml"),
                      urljoin(base_url, "/sitemap_index.xml")]
        urls: list[str] = []
        for sm in candidates:
            try:
                fr = fetch_fn(sm)
                xml = getattr(fr, "html", "") or getattr(fr, "text", "") or ""
                if "<urlset" not in xml and "<sitemapindex" not in xml:
                    continue
                # Extract <loc> entries.
                locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.I)
                for loc in locs:
                    if _origin(loc) == origin:
                        urls.append(loc)
                    # cap discovery
                    if len(urls) >= self._max_pages * 3:
                        return urls
                break
            except Exception:
                continue
        # Rank sitemap URLs toward contact/about/services.
        return self._rank_relevant(urls)

    def _rank_relevant(self, urls: list[str]) -> list[str]:
        def score(u):
            path = urlparse(u).path.lower()
            s = 0
            for key, pat in _KEYWORD_HINTS.items():
                if pat.search(path):
                    s += 5
            # shallow paths more likely relevant
            s += max(0, 6 - path.count("/"))
            return -s  # negative so sort ascending puts high score first
        return sorted(urls, key=score)

    def _discover_links(self, html: str, origin: str) -> list[str]:
        links: list[str] = []
        for m in re.finditer(r'href=["\']([^"\'#]+)', html or ""):
            href = m.group(1)
            joined = urljoin(origin + "/", href)
            if _origin(joined) == origin:
                links.append(joined)
        # Only keep suggestive links (contact/about/etc.) to avoid crawling everything.
        filtered = []
        for u in links:
            path = urlparse(u).path.lower()
            if any(pat.search(path) for pat in _KEYWORD_HINTS.values()):
                filtered.append(u)
        return filtered


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
