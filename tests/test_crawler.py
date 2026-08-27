"""Regression tests for the smart crawler (guards the len(int) TypeError)."""
from scraper.websites.crawler import SmartCrawler
from scraper.websites.fetcher import FetchResult


class _FakeFetcher:
    def __init__(self, html_by_url=None):
        self._html = html_by_url or {}

    def __call__(self, url):
        if url in self._html:
            return FetchResult(url=url, html=self._html[url],
                               text=self._html[url], failure_reason="")
        return FetchResult(url=url, html="", failure_reason="NOT_FOUND")


def _no_required(texts):
    return False


class TestSmartCrawler:
    def test_single_page_crawl_no_typeerror(self):
        fetcher = _FakeFetcher({"https://x.com": "<html><body>hi</body></html>"})
        c = SmartCrawler(max_pages=3, enable_sitemap=False)
        result = c.crawl("https://x.com", fetcher, _no_required)
        assert result.pages_crawled >= 1
        assert "https://x.com" in result.html_by_url

    def test_page_limit_respected(self):
        pages = {f"https://x.com/p{i}": "<html>p</html>" for i in range(10)}
        c = SmartCrawler(max_pages=3, enable_sitemap=False)
        fetcher = _FakeFetcher(pages)
        # base url returns a page that links to /p0../p9
        base = "<html><body>" + "".join(
            f'<a href="/p{i}">x</a>' for i in range(10)) + "</body></html>"
        fetcher = _FakeFetcher({"https://x.com": base, **pages})
        result = c.crawl("https://x.com", fetcher, _no_required)
        assert result.pages_crawled <= 3

    def test_homepage_failure_reason_captured(self):
        fetcher = _FakeFetcher({})
        def fail(url):
            return FetchResult(url=url, html="", failure_reason="DNS_FAILURE")
        c = SmartCrawler(max_pages=3, enable_sitemap=False)
        result = c.crawl("https://x.com", fail, _no_required)
        assert result.pages_crawled == 0
        assert result.last_failure_reason == "DNS_FAILURE"
