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


class ZeroListingsError(RuntimeError):
    """Raised when a non-empty Maps search returns zero listing URLs.

    Signals a collector/extraction failure (selector drift, interstitial,
    bot challenge, consent wall) as opposed to a genuinely empty query. The
    pipeline catches this and marks the query `failed` rather than `done`.
    """

    def __init__(self, query: str, diagnostic: str = ""):
        self.query = query
        self.diagnostic = diagnostic
        detail = f" — diagnostic: {diagnostic}" if diagnostic else ""
        super().__init__(
            f"0 listing URLs extracted for query '{query}'{detail} — likely "
            f"consent wall / selector drift / interstitial (not 'no results'). "
            f"Query left un-done so it can be retried."
        )


# EU GDPR consent-wall markers + the buttons that dismiss them. On a German (or
# any EU) IP, Google's first visit shows a consent screen ("Accept all" /
# "Alle akzeptieren") that hides the results feed until dismissed.
_CONSENT_MARKERS = [
    "consent.google", "before you continue", "accept all", "alle akzeptieren",
    "zustimmen", "i agree", "wtm", "reject all",
]
_CONSENT_BUTTON_SELECTORS = [
    'button:has-text("Accept all")',
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Zustimmen")',
    'button:has-text("I agree")',
    'form[action*="consent"] button',
    'div[role="dialog"] button:has-text("Accept")',
    'div[role="none"] button:has-text("Accept")',
    # Google consent form (EU): accept-all button by aria/name
    'button[aria-label*="Accept all"]',
    'form[action*="ConsentRedirect"] button[jsname]',
]


def handle_consent_wall(page) -> bool:
    """Dismiss the EU consent screen if present. Returns True if it acted."""
    try:
        content = page.content()
    except Exception:
        return False
    low = content.lower()
    if not any(m in low for m in _CONSENT_MARKERS):
        return False
    for sel in _CONSENT_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _page_diagnostic(page) -> str:
    """Capture a short, safe diagnostic snippet of the current page for logs."""
    try:
        text = page.inner_text("body") or ""
    except Exception:
        text = ""
    # Collapse whitespace, take the first 220 chars, keep it on one line.
    snippet = " ".join(text.split())[:220]
    url = ""
    try:
        url = page.url
    except Exception:
        pass
    return f"url={url[:120]} text={snippet!r}"


def _with_region(url: str, hl: str, gl: str) -> str:
    """Append hl/gl (language/region) params to a Maps URL.

    Google Maps honors `?hl=` for interface language and `?gl=` for region
    defaults. Forcing these decouples results/UI language from the VPS IP
    (e.g. a German server still returns English, US-targeted results when the
    query itself carries a US location). Uses `?` the first time (a bare
    search path has no query string yet), `&` thereafter.
    """
    # Remove any existing hl/gl (and their values) so we don't append duplicates
    # (e.g. "?hl=fr&gl=fr&hl=en&gl=us") which Google may resolve unpredictably.
    base = re.sub(r"([&?])(hl|gl)=[^&]*", "", url, flags=re.I)
    # Restore the query delimiter correctly after stripping.
    if "?" in base:
        if base.endswith("&"):
            base = base.rstrip("&") + "&"
        elif not base.endswith("?"):
            base += "&"
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}hl={hl}&gl={gl}"

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
# Live-verified (2026-08): hours live in a table (class eK4R0e) whose rows are
# buttons carrying an aria-label like "Monday, 10 AM to 6 PM, Copy open hours".
HOURS_TABLE_SELECTORS = ['table.eK4R0e', 'table[class*="hours"]']
HOURS_ROW_SELECTOR = 'button[aria-label*="Copy open hours"]'
# Open/Closed status span (live-verified) + aria fallback.
STATUS_SELECTORS = ['span.ZDu9vd', 'div.o0Svhf span',
                    '[aria-label="Open"], [aria-label="Closed"]']
DESCRIPTION_SELECTORS = ['div[class*="fontBodyMedium"]',
                         'div[data-item-id="editorial_summary"]',
                         'div.PYvSYb']


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
    # Google Maps place pages carry coordinates as !3d<lat>!4d<lng> tokens when
    # the /maps/@lat,lng viewport form is absent. Capture that as a fallback so
    # latitude/longitude are not left empty for place URLs.
    m3 = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url or "")
    if m3 and "lat" not in out:
        out["lat"] = float(m3.group(1))
        out["lng"] = float(m3.group(2))
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

def _open_business_page(ctx, place_url: str, hl: str = "en", gl: str = "us",
                        nav_timeout_ms: int = 30_000) -> dict:
    """Open a single listing and extract fields (layered selectors + regex)."""
    data: dict = {}
    if not place_url:
        return data
    page = ctx.new_page()
    page.set_default_timeout(nav_timeout_ms)
    try:
        # Force language/region on the business page too, so labels stay English.
        page.goto(_with_region(place_url, hl, gl),
                  wait_until="domcontentloaded", timeout=nav_timeout_ms)
        time.sleep(1.5)

        data["business_name"] = _first(page, NAME_SELECTORS)
        data["category"] = _first(page, CATEGORY_SELECTORS)
        data["full_address"] = _first(page, ADDRESS_SELECTORS)
        data["address"] = data["full_address"]
        data["phone"] = _first(page, PHONE_SELECTORS)
        data["website"] = _first_attr(page, WEBSITE_SELECTORS[0], "href") or \
            _first_attr(page, WEBSITE_SELECTORS[1], "href")
        data["business_hours"] = _extract_hours(page)
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
    for sel in STATUS_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=1500).strip()
                if txt:
                    # Status reads like "Closed · Opens 10 AM Fri".
                    if txt.lower().startswith("open"):
                        return "Open"
                    if txt.lower().startswith("closed"):
                        return "Closed"
                    return txt.split("·")[0].strip()
        except Exception:
            continue
    return "Open"


def _extract_hours(page) -> str:
    """Extract business hours as one human-readable string.

    Live-verified format: a table (class eK4R0e) whose rows are buttons with
    aria-label "Monday, 10 AM to 6 PM, Copy open hours". Falls back to the raw
    table text if the row buttons are absent, then to 'N/A'.
    """
    try:
        rows = page.locator(HOURS_ROW_SELECTOR)
        if rows.count() > 0:
            labels = []
            for i in range(rows.count()):
                aria = rows.nth(i).get_attribute("aria-label", timeout=1500) or ""
                # "Monday, 10 AM to 6 PM, Copy open hours" -> "Monday: 10 AM to 6 PM"
                label = aria.split(", Copy open hours")[0]
                label = re.sub(r",\s*(?=\d)", ": ", label, count=1)
                labels.append(label)
            if labels:
                return "; ".join(labels)
    except Exception:
        pass
    # Fallback: raw table text.
    for sel in HOURS_TABLE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=1500).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return "N/A"


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
                 cooldown_seconds: float = 0.0, hl: str = "en", gl: str = "us",
                 nav_timeout_ms: int = 30_000,
                 maps_delay: tuple[float, float] = (0.0, 0.0)):
        self._bm = browser_manager
        self._max_per_query = max_results_per_query
        self._max_total = max_total_results
        self._include_closed = include_permanently_closed
        self._scroll_delay = scroll_delay
        self._cooldown = cooldown_seconds
        self._hl = hl
        self._gl = gl
        self._nav_timeout_ms = nav_timeout_ms
        self._maps_delay = maps_delay
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

            # EU GDPR consent wall: dismiss it so the feed can load. On an EU
            # IP this appears on first visit and otherwise hides every result.
            if handle_consent_wall(page):
                time.sleep(3.0)

            # Bot / captcha detection: cooldown, then fail-closed so the
            # pipeline marks the query `failed` (retry-able) rather than `done`.
            if detect_bot_challenge(page.content()):
                log.captcha("bot challenge for query '%s' — cooling down %.0fs",
                            query, self._cooldown)
                if self._cooldown:
                    time.sleep(self._cooldown)
                raise ZeroListingsError(
                    query, "bot challenge / CAPTCHA detected on the search page")

            self._scroll_results(page)
            listing_links = self._extract_listing_links(page)
            log.info("query '%s': found %d listing place URLs", query, len(listing_links))

            # A genuinely empty result set is distinct from a broken collector.
            # Distinguish them on explicit "no results" copy so a legitimately
            # empty query is marked done (and never re-retried forever) while a
            # selector-drift / interstitial / bot-challenge still fails closed.
            if not listing_links:
                try:
                    body_text = page.locator("body").inner_text(timeout=2000).lower()
                except Exception:
                    body_text = ""
                if "we could not find any results" in body_text or \
                        "no results found" in body_text:
                    log.info("query '%s' has genuinely no results — marking done", query)
                    return
                # Fail-closed: a non-empty search that yields 0 listing links,
                # with no explicit "no results" copy, means the collector is
                # broken, NOT that the query is empty. Attach a diagnostic
                # snippet so the log explains *what* was on the page.
                raise ZeroListingsError(query, _page_diagnostic(page))

            for place_url in listing_links:
                if self._max_total and self._yielded_total >= self._max_total:
                    break
                if self._max_per_query and yielded >= self._max_per_query:
                    break
                data = _open_business_page(ctx, place_url, self._hl, self._gl,
                                           self._nav_timeout_ms)
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
                self._maps_pacing_pause()
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
        """Scroll the results feed (div[role="feed"]) to load more listings.

        Targets the feed element itself (live-verified); falls back to global
        mouse wheel if the feed locator is missing.
        """
        feed = None
        try:
            loc = page.locator('div[role="feed"]')
            if loc.count() > 0:
                feed = loc.first
        except Exception:
            feed = None

        for _ in range(12):
            try:
                if feed is not None:
                    feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                else:
                    page.mouse.wheel(0, 1200)
            except Exception:
                page.mouse.wheel(0, 1200)
            lo, hi = self._scroll_delay
            time.sleep(random.uniform(lo, hi) / 1000.0)
            if self._has_no_more_results(page):
                break

    def _has_no_more_results(self, page) -> bool:
        # Light check via inner_text (cheap) instead of full page.content()
        # serialization, which was called every scroll step (up to 12x/query).
        try:
            body = page.locator("body")
            if body.count() == 0:
                return False
            text = body.inner_text(timeout=1500)
            return "You've reached the end of the list" in text
        except Exception:
            return False

    def _extract_listing_links(self, page) -> list[str]:
        """Collect place URLs from the results feed using layered selectors."""
        links: set[str] = set()
        for sel in RESULT_CARD_SELECTORS:
            try:
                locs = page.locator(sel)
                n = locs.count()
                for i in range(n):
                    href = locs.nth(i).get_attribute("href", timeout=1500)
                    if href and "/maps/place/" in href:
                        links.add(href)
            except Exception:
                continue
        return list(links)

    def _small_pause(self) -> None:
        """A small randomized pause between records to keep pacing gentle."""
        lo, hi = self._scroll_delay
        time.sleep(random.uniform(lo, hi) / 1000.0)

    def _maps_pacing_pause(self) -> None:
        """Apply `delays.maps_*` seconds between Maps actions (was dead config)."""
        lo, hi = self._maps_delay
        if hi > 0:
            time.sleep(random.uniform(lo, hi) if hi > lo else hi)
