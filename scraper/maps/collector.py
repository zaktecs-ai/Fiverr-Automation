"""Google Maps collector.

Strategy (layered, resilient to selector drift):
  * Navigate to a Google Maps search results URL for a query.
  * Scroll the results feed to reveal listings (bounded, configurable).
  * Extract each listing's data using PRIMARY selectors, then ALTERNATE
    selectors, then a semantic/text fallback. Missing data -> "N/A".

The collector yields normalized dicts; it does NOT write output (the pipeline
owns persistence). This keeps the collector a pure, testable unit — tests can
feed it fixture HTML through `parse_page` without a live browser.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator
from urllib.parse import quote_plus

from ..utils.normalize import normalize_text
from ..utils.text import to_float, to_int

log = logging.getLogger(__name__)

MAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}"


def parse_google_maps_url(url: str) -> dict:
    """Extract place_id / coordinates / query from a Google Maps URL."""
    out = {"query": None}
    m = re.search(r"/maps/search/([^/]+)", url or "")
    if m:
        out["query"] = m.group(1)
    m = re.search(r"/maps/place/([^/]+)", url or "")
    if m:
        out["place_name"] = m.group(1)
    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", url or "")
    if m:
        out["place_id"] = m.group(1)
    m = re.search(r"/maps/@(-?\d+\.\d+),(-?\d+\.\d+)", url or "")
    if m:
        out["lat"] = float(m.group(1))
        out["lng"] = float(m.group(2))
    return out


def _open_business_page(ctx, place_url: str) -> dict:
    """Open a single business listing page and extract fields (layered)."""
    data: dict = {}
    if not place_url:
        return data
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    try:
        page.goto(place_url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(1.5)  # allow JS to settle
        html = page.content()

        data["business_name"] = _first(page, [
            "h1",  # primary
            'h1[class*="fontHeadline"]',  # alternate
            'div[class*="fontHeadline"]',
        ])

        # Address
        data["full_address"] = _first(page, [
            'button[data-item-id="address"]',
            'div[class*="address"]',
            'span[jsan*="address"]',
        ])

        # Website
        data["website"] = _first_attr(page, 'a[data-item-id="authority"]', "href")

        # Phone
        data["phone"] = _first(page, ['button[data-item-id^="phone"]'])

        # Hours / open status
        data["business_hours"] = _first(page, ['div[class*="hours"]',
                                               'tr[class*="hours"]'])

        # Description
        data["business_description"] = _first(page, ['div[class*="fontBodyMedium"]'])

        # Rating / reviews
        rating = _first(page, ['div[class*="rating"]', 'span[aria-label*="stars"]', 'span[aria-label*="star"]'])
        data["rating"] = to_float(rating)
        reviews = _first(page, ['span[aria-label*="reviews"]', 'button[jsaction*="review"]'])
        data["review_count"] = to_int(reviews)

        # Coordinates from the page URL if present.
        parsed = parse_google_maps_url(page.url)
        for k in ("lat", "lng"):
            if k in parsed:
                data["latitude" if k == "lat" else "longitude"] = parsed[k]
        data["google_maps_url"] = page.url

        return data
    finally:
        try:
            page.close()
        except Exception:
            pass


def _first(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=2000).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return None


def _first_attr(page, selector: str, attr: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count() > 0:
            return loc.get_attribute(attr, timeout=2000)
    except Exception:
        return None
    return None


class MapsCollector:
    """Streams normalized business dicts from Google Maps for a query.

    Uses a shared BrowserManager. Yields parsed records; limits and pagination
    are enforced here so the caller stays simple.
    """

    def __init__(self, browser_manager, *, max_results_per_query: int = 0,
                 max_total_results: int = 0, include_permanently_closed: bool = False,
                 scroll_delay: tuple[int, int] = (800, 1600),
                 cooldown_seconds: float = 0.0):
        self._bm = browser_manager
        self._max_per_query = max_results_per_query
        self._max_total = max_total_results
        self._include_closed = include_permanently_closed
        self._scroll_delay = scroll_delay
        self._cooldown = cooldown_seconds

    def collect(self, query: str) -> Iterator[dict]:
        """Yield normalized business dicts for a single query."""
        ctx = self._bm.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        url = MAPS_SEARCH_URL.format(query=quote_plus(query))
        yielded = 0
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            time.sleep(3.0)
            # Scroll the results feed to reveal more listings (bounded).
            self._scroll_results(page)
            listing_links = self._extract_listing_links(page)
            log.info("query '%s': found %d listing place URLs", query, len(listing_links))

            for place_url in listing_links:
                if self._max_total and yielded >= self._max_total:
                    break
                if self._max_per_query and yielded >= self._max_per_query:
                    break
                data = _open_business_page(ctx, place_url)
                if not data.get("business_name"):
                    # Fallback: parse whatever name is in the URL.
                    parsed = parse_google_maps_url(place_url)
                    data["business_name"] = parsed.get("place_name") or "N/A"
                data["source_query"] = query
                data = self._parse_source_location(data, query)
                # Skip permanently-closed listings unless configured otherwise.
                status = (data.get("business_status") or "").lower()
                if "permanently closed" in status and not self._include_closed:
                    continue
                yielded += 1
                yield data
                self._delay(scroll=False)
        finally:
            try:
                page.close()
                ctx.close()
            except Exception:
                pass

    def _scroll_results(self, page) -> None:
        """Scroll the results feed a bounded number of times to load listings."""
        for i in range(12):
            page.mouse.wheel(0, 1200)
            lo, hi = self._scroll_delay
            time.sleep((lo + hi) / 2 / 1000.0)
            if self._has_no_more_results(page):
                break

    def _has_no_more_results(self, page) -> bool:
        try:
            txt = page.content()
            return "You've reached the end of the list" in txt
        except Exception:
            return False

    def _extract_listing_links(self, page) -> list[str]:
        """Collect place URLs from the results feed using layered selectors."""
        links: set[str] = set()
        for sel in ['a[href*="/maps/place/"]', 'a[aria-label]', 'a[jsaction*="place"]']:
            try:
                locs = page.locator(sel)
                n = locs.count()
                for i in range(min(n, 200)):
                    href = locs.nth(i).get_attribute("href", timeout=1500)
                    if href and "/maps/place/" in href:
                        links.add(href)
            except Exception:
                continue
        return list(links)

    def _parse_source_location(self, data: dict, query: str) -> dict:
        """Heuristically split 'dentists in Dallas, TX' into keyword + location."""
        m = re.split(r"\s+in\s+", query, maxsplit=1, flags=re.I)
        if len(m) == 2:
            data["source_keyword"] = m[0].strip()
            data["source_location"] = m[1].strip()
        else:
            data["source_keyword"] = query
            data["source_location"] = "N/A"
        return data

    def _delay(self, scroll: bool) -> None:
        if self._cooldown:
            time.sleep(self._cooldown)
