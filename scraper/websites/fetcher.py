"""HTTP-first website fetcher with careful status classification.

Default flow:
  1. HTTP GET (httpx) with independent connect/read timeouts.
  2. If HTTP worked, return contents + status LIVE.
  3. If HTTP is blocked/unsuitable, classify the *failure reason* precisely —
     never as a generic "dead". Then optionally escalate to Playwright.

Status model (see models.py):
  * website_status in {LIVE, DEAD}
  * website_failure_reason in a rich set; only DNS/connection-refused/tls/404
    are strong "dead" signals. HTTP-blocked / CAPTCHA / JS-required / timeout
    are treated as "live but not fetched" — the record is preserved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from ..models import FailureReason, WebsiteStatus, resolve_website_status
from ..utils.normalize import normalize_url

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_BLOCKED_MARKERS = [
    "captcha", "verify you are human", "are you a robot", "cf-challenge",
    "access denied", "cloudflare ray id", "attention required",
    "enable javascript and cookies to continue", "incapsula",
]


@dataclass
class FetchResult:
    url: str = ""
    status_code: int | None = None
    html: str = ""
    text: str = ""
    headers: dict = field(default_factory=dict)
    final_url: str = ""
    website_status: str = WebsiteStatus.LIVE
    failure_reason: str = ""
    used_playwright: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.html) and self.failure_reason == ""


def classify_html_block(html: str) -> str | None:
    """Return a failure reason if the HTML looks like a bot/CAPTCHA challenge."""
    if not html:
        return None
    low = html.lower()
    for marker in _BLOCKED_MARKERS:
        if marker in low:
            if "captcha" in marker or "robot" in marker or "human" in marker:
                return FailureReason.CAPTCHA_DETECTED
            return FailureReason.HTTP_BLOCKED
    return None


class WebsiteFetcher:
    def __init__(self, *, connect_timeout: float = 10.0, read_timeout: float = 20.0,
                 proxy: httpx.Proxy | None = None, max_redirects: int = 5,
                 retries: int = 1, retry_base_delay: float = 1.0):
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._proxy = proxy
        self._max_redirects = max_redirects
        self._retries = retries
        self._retry_base_delay = retry_base_delay

    def fetch(self, url: str) -> FetchResult:
        url = normalize_url(url)
        if url == "N/A":
            return FetchResult(url=url, website_status=WebsiteStatus.LIVE,
                               failure_reason=FailureReason.UNKNOWN)
        # Retry transient timeouts/network errors before accepting a final result.
        for attempt in range(self._retries + 1):
            result = self._fetch_once(url)
            if result.ok or attempt >= self._retries:
                return result
            if result.failure_reason in (FailureReason.TIMEOUT,
                                         FailureReason.UNREACHABLE,
                                         FailureReason.UNKNOWN):
                log.retry("fetch %s transient (%s), retry %d/%d",
                          url, result.failure_reason, attempt + 1, self._retries)
                import time as _t
                _t.sleep(self._retry_base_delay * (2 ** attempt))
            else:
                # Non-transient (403/404/DNS dead, block, captcha): don't retry.
                return result
        return result

    def _fetch_once(self, url: str) -> FetchResult:
        follow = {"max_redirects": self._max_redirects} if self._max_redirects > 0 else {}
        result = FetchResult(url=url, final_url=url)
        try:
            with httpx.Client(timeout=httpx.Timeout(self._connect_timeout,
                                                     read=self._read_timeout),
                              proxy=self._proxy, follow_redirects=True,
                              **follow) as client:
                r = client.get(url, headers=DEFAULT_HEADERS)
                result.status_code = r.status_code
                result.headers = dict(r.headers)
                result.final_url = str(r.url)
                result.html = r.text
                result.text = _html_to_text_light(r.text)

                if r.status_code == 200:
                    block = classify_html_block(r.text)
                    if block:
                        result.failure_reason = block
                    else:
                        result.failure_reason = ""
                elif r.status_code == 404:
                    result.failure_reason = FailureReason.NOT_FOUND
                elif r.status_code == 403:
                    result.failure_reason = FailureReason.HTTP_BLOCKED
                elif r.status_code == 429:
                    result.failure_reason = FailureReason.HTTP_BLOCKED
                elif r.status_code >= 500:
                    result.failure_reason = FailureReason.UNREACHABLE
                else:
                    result.failure_reason = FailureReason.UNKNOWN
        except httpx.ConnectTimeout:
            result.failure_reason = FailureReason.TIMEOUT
        except httpx.ReadTimeout:
            result.failure_reason = FailureReason.TIMEOUT
        except httpx.ConnectError as e:
            result.failure_reason = FailureReason.CONNECTION_REFUSED
            log.debug("connect error %s: %s", url, e)
        except httpx.SSLError:
            result.failure_reason = FailureReason.TLS_ERROR
        except Exception as e:  # noqa: BLE001 - intentional last-resort
            result.failure_reason = FailureReason.UNKNOWN
            log.debug("fetch error %s: %s", url, e)

        result.website_status = resolve_website_status(result.failure_reason)
        return result


def _html_to_text_light(html: str) -> str:
    """A cheap, dependency-light text extraction (used to seed PageContext.text
    without pulling all of BeautifulSoup when not needed)."""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml").get_text(" ")
    except Exception:
        return html
