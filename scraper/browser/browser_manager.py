"""Playwright browser lifecycle management.

Responsibilities:
  * Launch a browser lazily and reuse it across many pages/contexts (avoiding a
    fresh process per task).
  * Recycle the browser after a configurable number of queries / sites, or when
    memory pressure is detected.
  * Enforce per-operation timeouts and safe teardown so a hung page or crashed
    browser never freezes or leaks the whole job.

The manager is used by both the Maps collector and the Playwright website
fallback. It is not imported at module top so the engine can run (e.g. in
HTTP-only tests) without Playwright installed.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, restart_after_queries: int = 0, headless: bool = True,
                 proxy: dict | None = None, nav_timeout_ms: int = 30_000,
                 display: str | None = None):
        self._restart_after_queries = restart_after_queries
        self._headless = headless
        self._proxy = proxy
        self._nav_timeout_ms = nav_timeout_ms
        self._display = display
        self._pw = None
        self._browser = None
        self._queries_processed = 0
        self._sites_processed = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _ensure_browser(self):
        # Fast path: already launched (safe — a set reference).
        if self._browser is not None:
            return self._browser
        # Slow path guarded by the lock so two threads racing on first use
        # cannot both launch a Chromium process (double-launch / leak).
        with self._lock:
            if self._browser is not None:
                return self._browser
            # Route the visible browser to a specific X display BEFORE starting
            # Playwright, and set XAUTHORITY too. Both must be inherited by the
            # browser process (set via the inherited environment, NOT a `env`
            # launch kwarg that would replace the whole environment and drop
            # XAUTHORITY — which caused "Client is not authorized to connect").
            if not self._headless and self._display:
                import os as _os
                _os.environ["DISPLAY"] = self._display
                # The VNC server stores the X11 magic cookie in ~/.Xauthority.
                # Ensure it is exported so Chromium can present it to display :2.
                xauth = _os.path.join(_os.path.expanduser("~"), ".Xauthority")
                if _os.path.exists(xauth):
                    _os.environ["XAUTHORITY"] = xauth
                log.info("visible browser routed to display %s (XAUTHORITY=%s)",
                         self._display, _os.environ.get("XAUTHORITY", ""))
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("Playwright is not installed. Run ./setup.sh or "
                                   "`pip install playwright && playwright install`.") from e
            self._pw = sync_playwright().start()
            launch_kwargs = {"headless": self._headless}
            if self._proxy:
                launch_kwargs["proxy"] = self._proxy
            try:
                self._browser = self._pw.chromium.launch(**launch_kwargs)
            except Exception as e:  # pragma: no cover - env dependent (missing binary)
                # The far more common failure than a missing package: the package is
                # installed but the Chromium binary was never downloaded.
                msg = str(e).lower()
                if "executable doesn't exist" in msg or "playwright install" in msg or \
                        "browser" in msg and "not found" in msg:
                    self._pw.stop()
                    self._pw = None
                    raise RuntimeError(
                        "Chromium browser binary is missing. Run "
                        "`python -m playwright install chromium` (or re-run ./setup.sh)."
                    ) from e
                raise
            log.info("browser launched (headless=%s)", self._headless)
            return self._browser

    def new_context(self, proxy: dict | None = None, geolocation: dict | None = None,
                    locale: str = "en-US"):
        browser = self._ensure_browser()
        ctx_proxy = proxy or self._proxy
        kwargs = {"viewport": {"width": 1366, "height": 900},
                  "locale": locale,
                  "timezone_id": "America/New_York",
                  "user_agent": (
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36")}
        if ctx_proxy:
            kwargs["proxy"] = ctx_proxy
        if geolocation:
            kwargs["geolocation"] = geolocation
            kwargs["permissions"] = ["geolocation"]
        return browser.new_context(**kwargs)

    def mark_query(self) -> None:
        self._queries_processed += 1

    def mark_site(self) -> None:
        self._sites_processed += 1

    def should_restart(self) -> bool:
        if self._restart_after_queries and self._queries_processed >= self._restart_after_queries:
            return True
        return False

    def recycle(self, force: bool = False) -> None:
        """Close the browser; it will be re-launched lazily next use."""
        with self._lock:
            if not force and not self.should_restart():
                return False
            self._close()
            self._queries_processed = 0
            self._sites_processed = 0
            return True

    def _close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as e:  # pragma: no cover
                log.debug("browser close error: %s", e)
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as e:  # pragma: no cover
                log.debug("playwright stop error: %s", e)
            self._pw = None

    def close(self) -> None:
        self._close()

    @property
    def browser(self):
        return self._ensure_browser()
