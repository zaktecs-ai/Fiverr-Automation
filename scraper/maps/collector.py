"""Google Maps collector.

Strategy (layered, resilient to selector drift):
  * Navigate to a Google Maps search results URL for a query.
  * Scroll the results feed to reveal listings (bounded, configurable).
  * Extract each listing using PRIMARY selectors, then ALTERNATE selectors,
    then a semantic/text fallback (regex). Missing data -> "N/A".

Selectors are drawn from the field-tested legacy scraper (see docs/architecture.md)
and layered so a single stale class never breaks extraction.

The collector yields normalized dicts; persistence is owned by the pipeline.
Pure parsing helpers (rating/reviews regex, address decomposition, bot-detection
sniffing) are exposed as module-level functions so they are unit-testable
without a live browser.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Iterator
from urllib.parse import quote_plus

from ..utils.text import to_int

log = logging.getLogger(__name__)

MAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}"


def _with_region(url: str, hl: str, gl: str) -> str:
    """Append hl/gl (language/region) params to a Maps URL.

    Google Maps honors `?hl=` for interface language and `?gl=` for region
    defaults. Forcing these decouples results/UI language from the VPS IP
    (e.g. a German server still returns English, US-targeted results when the
    query itself carries a US location). Uses `?` the first time (a bare
    search path has no query string yet), `&` thereafter.
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}hl={hl}&gl={gl}"

# ---- Result-card selectors (layered: primary -> alternate -> fallback) ----
RESULT_CARD_SELECTORS = [
    'a.hfpxzc',                        # legacy-proven result card
    'a[href*="/maps/place/"]',         # generic place link
    'a[aria-label]',                    # semantic fallback (has a name)
]

NAME_SELECTORS = ['h1.DUwDvf', 'h1', 'div[class*="fontHeadline"]']
CATEGORY_SELECTORS = ['button.DkEaL', 'button[class*="category"]',
                      'button[jsaction*="category"]']
ADDRESS_SELECTORS = ['button[data-item-id="address"]', 'div[class*="address"]']
PHONE_SELECTORS = ['button[data-item-id^="phone:tel:"]', 'button[data-item-id^="phone"]']
WEBSITE_SELECTORS = ['a[data-item-id="authority"]', 'a[aria-label*="Website"]']
CLAIM_SELECTOR = 'a[data-item-id="merchant_claim_business"]'
HOURS_SELECTORS = ['div[class*="hours"]', 'tr[class*="hours"]',
                   'button[data-item-id="oh"]', 'div[aria-label*="hours"]']
DESCRIPTION_SELECTORS = ['div[class*="fontBodyMedium"]',
                         'div[data-item-id="editorial_summary"]']


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


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-testable, no browser)
# ---------------------------------------------------------------------------

_REVIEWS_PAREN_RE = re.compile(r"\(([\d,]+)\)")
_REVIEWS_WORD_RE = re.compile(r"([\d,]+)\s*reviews?", re.I)
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\b\s*\d{5}")
_CITY_RE = re.compile(r",\s*([^,]+?),\s*[A-Z]{2}\s*\d{5}")


def parse_rating_reviews(header_text: str) -> tuple:
    """Extract (rating, review_count) from a Maps header text block.

    Layered regexes: "4.8" for rating; "(365)" then "365 reviews" for count.
    Returns (float-or-None, int-or-None).
    """
    if not header_text:
        return (None, None)
    rating = None
    reviews = None

    m = re.search(r"\b([1-5]\.\d)\b", header_text)
    if m:
        try:
            rating = float(m.group(1))
        except ValueError:
            rating = None

    m = _REVIEWS_PAREN_RE.search(header_text)
    if m:
        reviews = to_int(m.group(1))
    else:
        m = _REVIEWS_WORD_RE.search(header_text)
        if m:
            reviews = to_int(m.group(1))
    return (rating, reviews)


def decompose_address(address: str) -> dict:
    """Split a Maps address string into city / state / postal_code.

    Returns keys city, state, postal_code (each 'N/A' when unresolved).
    """
    out = {"city": "N/A", "state": "N/A", "postal_code": "N/A"}
    if not address:
        return out

    zm = _ZIP_RE.search(address)
    if zm:
        out["postal_code"] = zm.group(0).split("-")[0]

    sm = _STATE_ZIP_RE.search(address)
    if sm:
        out["state"] = sm.group(1)

    cm = _CITY_RE.search(address)
    if cm:
        out["city"] = cm.group(1).strip()
    return out


_BOT_MARKERS = [
    "Our systems have detected unusual traffic",
    "unusual traffic from your computer network",
    "CaptchaRedirect",
    "g-recaptcha",
]


def detect_bot_challenge(html_or_text: str) -> bool:
    """Return True if the page looks like a Google bot/captcha challenge."""
    if not html_or_text:
        return False
    low = html_or_text.lower()
    return any(m.lower() in low for m in _BOT_MARKERS)


def split_source_location(query: str) -> tuple[str, str]:
    """Split 'dentists in Dallas, TX' -> ('dentists', 'Dallas, TX')."""
    m = re.split(r"\s+in\s+", query, maxsplit=1, flags=re.I)
    if len(m) == 2:
        return m[0].strip(), m[1].strip()
    return query, "N/A"


# ---------------------------------------------------------------------------
# Browser-bound extraction
# ---------------------------------------------------------------------------

def _open_business_page(ctx, place_url: str, hl: str = "en", gl: str = "us") -> dict:
    """Open a single listing and extract fields (layered selectors + regex)."""
    data: dict = {}
    if not place_url:
        return data
    page = ctx.new_page()
    page.set_default_timeout(30_000)
    try:
        # Force language/region on the business page too, so labels stay English.
        page.goto(_with_region(place_url, hl, gl),
                  wait_until="domcontentloaded", timeout=30_000)
        time.sleep(1.5)

        data["business_name"] = _first(page, NAME_SELECTORS)
        data["category"] = _first(page, CATEGORY_SELECTORS)
        data["full_address"] = _first(page, ADDRESS_SELECTORS)
        data["address"] = data["full_address"]
        data["phone"] = _first(page, PHONE_SELECTORS)
        data["website"] = _first_attr(page, WEBSITE_SELECTORS[0], "href") or \
            _first_attr(page, WEBSITE_SELECTORS[1], "href")
        data["business_hours"] = _first(page, HOURS_SELECTORS)
        data["business_description"] = _first(page, DESCRIPTION_SELECTORS)

        # Rating / reviews: legacy-proven grandparent header trick + text fallback.
        rating, reviews = None, None
        try:
            header_locator = page.locator('h1.DUwDvf').locator('xpath=../..').first
            if header_locator.count() > 0:
                rating, reviews = parse_rating_reviews(header_locator.inner_text())
        except Exception:
            pass
        if rating is None:
            header_text = _first(page, ['h1.DUwDvf', 'h1'])
            rating, reviews = parse_rating_reviews(header_text or "")
        data["rating"] = rating if rating is not None else "N/A"
        data["review_count"] = reviews if reviews is not None else "N/A"

        # Address decomposition -> city/state/zip.
        data.update(decompose_address(data.get("full_address") or ""))

        data["claimed_status"] = _claimed_status(page)
        data["business_status"] = _business_status(page)

        parsed = parse_google_maps_url(page.url)
        if parsed.get("lat"):
            data["latitude"] = parsed["lat"]
        if parsed.get("lng"):
            data["longitude"] = parsed["lng"]
        if parsed.get("place_id"):
            data["place_id"] = parsed["place_id"]
        data["google_maps_url"] = page.url

        return data
    finally:
        try:
            page.close()
        except Exception:
            pass


def _claimed_status(page) -> str:
    try:
        if page.locator(CLAIM_SELECTOR).count() > 0:
            return "Unclaimed"
    except Exception:
        pass
    return "Claimed"


def _business_status(page) -> str:
    try:
        if page.locator("text=Permanently closed").count() > 0:
            return "Permanently closed"
    except Exception:
        pass
    return "Open"


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

    Uses a shared BrowserManager. Limits, bot-cooldown, and pagination are
    enforced here so the caller stays simple.
    """

    def __init__(self, browser_manager, *, max_results_per_query: int = 0,
                 max_total_results: int = 0, include_permanently_closed: bool = False,
                 scroll_delay: tuple[int, int] = (800, 1600),
                 cooldown_seconds: float = 0.0, hl: str = "en", gl: str = "us"):
        self._bm = browser_manager
        self._max_per_query = max_results_per_query
        self._max_total = max_total_results
        self._include_closed = include_permanently_closed
        self._scroll_delay = scroll_delay
        self._cooldown = cooldown_seconds
        self._hl = hl
        self._gl = gl
        self._yielded_total = 0

    def collect(self, query: str) -> Iterator[dict]:
        """Yield normalized business dicts for a single query."""
        ctx = self._bm.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        url = _with_region(MAPS_SEARCH_URL.format(query=quote_plus(query)),
                           self._hl, self._gl)
        yielded = 0
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            time.sleep(3.0)

            # Bot / captcha detection: cooldown + skip query, never mark dead.
            if detect_bot_challenge(page.content()):
                log.captcha("bot challenge for query '%s' — cooling down %.0fs",
                            query, self._cooldown)
                if self._cooldown:
                    time.sleep(self._cooldown)
                return

            self._scroll_results(page)
            listing_links = self._extract_listing_links(page)
            log.info("query '%s': found %d listing place URLs", query, len(listing_links))

            for place_url in listing_links:
                if self._max_total and self._yielded_total >= self._max_total:
                    break
                if self._max_per_query and yielded >= self._max_per_query:
                    break
                data = _open_business_page(ctx, place_url, self._hl, self._gl)
                if not data.get("business_name"):
                    parsed = parse_google_maps_url(place_url)
                    data["business_name"] = parsed.get("place_name") or "N/A"
                data = self._attach_query(data, query)
                status = (data.get("business_status") or "").lower()
                if "permanently closed" in status and not self._include_closed:
                    continue
                yielded += 1
                self._yielded_total += 1
                yield data
                self._small_pause()
        finally:
            try:
                page.close()
                ctx.close()
            except Exception:
                pass

    def _attach_query(self, data: dict, query: str) -> dict:
        data["source_query"] = query
        kw, loc = split_source_location(query)
        data["source_keyword"] = kw
        data["source_location"] = loc
        return data

    def _scroll_results(self, page) -> None:
        """Scroll the results feed a bounded number of times to load listings."""
        for _ in range(12):
            page.mouse.wheel(0, 1200)
            lo, hi = self._scroll_delay
            time.sleep((lo + hi) / 2 / 1000.0)
            if self._has_no_more_results(page):
                break

    def _has_no_more_results(self, page) -> bool:
        try:
            return "You've reached the end of the list" in page.content()
        except Exception:
            return False

    def _extract_listing_links(self, page) -> list[str]:
        """Collect place URLs from the results feed using layered selectors."""
        links: set[str] = set()
        for sel in RESULT_CARD_SELECTORS:
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

    def _small_pause(self) -> None:
        """A small randomized pause between records to keep pacing gentle."""
        lo, hi = self._scroll_delay
        time.sleep((lo + hi) / 2 / 1000.0)
