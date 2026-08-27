"""Thread-safe DNS/MX cache to avoid repeated lookups for the same domain."""
from __future__ import annotations

import threading
import time


class DNSCache:
    """An in-memory TTL cache with a size cap to bound memory growth."""

    def __init__(self, max_size: int = 50_000, ttl: int = 3600):
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl

    def get(self, domain: str):
        with self._lock:
            item = self._cache.get(domain)
            if item is None:
                return None
            ts, val = item
            if time.time() - ts > self._ttl:
                self._cache.pop(domain, None)
                return None
            return val

    def set(self, domain: str, value) -> None:
        with self._lock:
            if len(self._cache) >= self._max_size:
                # Simple FIFO eviction by dropping the oldest inserted key.
                try:
                    self._cache.pop(next(iter(self._cache)))
                except StopIteration:
                    pass
            self._cache[domain] = (time.time(), value)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
