"""Proxy abstraction.

Proxy support is DISABLED by default (the operator has no proxies yet), but the
interface is designed so proxies — static HTTP(S), Playwright, a pool/provider,
and rotation — can be added later without touching the scraper core. All
network usages pass through :meth:`ProxyManager.apply`, which is a no-op when
proxies are disabled.
"""
from __future__ import annotations

import dataclasses
import random
import threading
from typing import Any


@dataclasses.dataclass
class ProxyConfig:
    enabled: bool = False
    http: str | None = None            # e.g. "http://user:pass@host:port"
    https: str | None = None
    pool: list[str] = dataclasses.field(default_factory=list)
    rotation: str = "round_robin"      # "round_robin" | "random"

    def from_dict(self, d: dict | None) -> "ProxyConfig":
        if not d:
            return self
        self.enabled = bool(d.get("enabled", False))
        self.http = d.get("http")
        self.https = d.get("https")
        self.pool = list(d.get("pool", []) or [])
        self.rotation = d.get("rotation", "round_robin")
        return self


class ProxyManager:
    """Central proxy resolver for httpx and Playwright clients."""

    def __init__(self, config: ProxyConfig | None = None):
        self.config = config or ProxyConfig()
        self._idx = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def resolve(self) -> str | None:
        """Return a single proxy URL for the current operation (rotation-aware)."""
        if not self.enabled:
            return None
        if self.config.pool:
            with self._lock:
                if self.config.rotation == "random":
                    return random.choice(self.config.pool)
                p = self.config.pool[self._idx % len(self.config.pool)]
                self._idx += 1
                return p
        return self.config.https or self.config.http

    def httpx_proxy(self) -> str | None:
        """Proxy URL suitable for httpx.Proxy."""
        return self.resolve()

    def playwright_proxy(self) -> dict | None:
        """Proxy settings dict suitable for Playwright launch/browser.new_context."""
        url = self.resolve()
        if not url:
            return None
        return {"server": url}

    # Future extension points (documented, not yet wired):
    def health_check(self) -> dict[str, Any]:  # pragma: no cover - future
        return {"enabled": self.enabled, "proxies": len(self.config.pool)}
