"""Bounded retry helper with jitter and callback hooks."""
from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)


def retry_call(fn, *, attempts: int, base_delay: float, jitter: float = 0.5,
               retry_on: tuple = (Exception,), label: str = "operation"):
    """Call `fn()` up to `attempts` times, sleeping with exponential backoff.

    Only exceptions in `retry_on` trigger a retry. Returns fn()'s value, or
    re-raises the last exception after exhausting attempts.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            delay = delay * (1 - jitter) + delay * jitter * random.random()
            log.retry("%s failed (attempt %d/%d): %s — retrying in %.2fs",
                      label, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]
