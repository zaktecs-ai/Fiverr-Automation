"""Idempotent, severity-aware logging.

Provides custom log levels so a long-running job can be diagnosed without
flooding the console. Each category maps to a level number and a prefix.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Custom severity levels (higher = more severe). Values chosen to interleave
# cleanly with stdlib levels (DEBUG=10, INFO=20, WARNING=30, ERROR=40, CRITICAL=50).
RETRY = 25
TIMEOUT = 26
BLOCKED = 27
CAPTCHA = 28
RECOVERED = 29
CHECKPOINT = 21
MEMORY_WARNING = 31

_LEVEL_NAMES = {
    CHECKPOINT: "CHECKPOINT",
    RETRY: "RETRY",
    TIMEOUT: "TIMEOUT",
    BLOCKED: "BLOCKED",
    CAPTCHA: "CAPTCHA",
    RECOVERED: "RECOVERED",
    MEMORY_WARNING: "MEMORY_WARNING",
}

for _name, _level in {
    "CHECKPOINT": CHECKPOINT,
    "RETRY": RETRY,
    "TIMEOUT": TIMEOUT,
    "BLOCKED": BLOCKED,
    "CAPTCHA": CAPTCHA,
    "RECOVERED": RECOVERED,
    "MEMORY_WARNING": MEMORY_WARNING,
}.items():
    logging.addLevelName(_level, _name)


def _log(self, level, msg, *args, **kwargs):  # pragma: no cover - stdlib seam
    if self.isEnabledFor(level):
        self._log(level, msg, args, **kwargs)


logging.Logger.checkpoint = lambda self, msg, *a, **k: _log(self, CHECKPOINT, msg, *a, **k)
logging.Logger.retry = lambda self, msg, *a, **k: _log(self, RETRY, msg, *a, **k)
logging.Logger.timeout = lambda self, msg, *a, **k: _log(self, TIMEOUT, msg, *a, **k)
logging.Logger.blocked = lambda self, msg, *a, **k: _log(self, BLOCKED, msg, *a, **k)
logging.Logger.captcha = lambda self, msg, *a, **k: _log(self, CAPTCHA, msg, *a, **k)
logging.Logger.recovered = lambda self, msg, *a, **k: _log(self, RECOVERED, msg, *a, **k)
logging.Logger.memory_warning = lambda self, msg, *a, **k: _log(self, MEMORY_WARNING, msg, *a, **k)


class _NoConsoleFmt(logging.Formatter):
    """Console formatter: short, no timestamps (they go to the file log)."""

    def format(self, record):
        if record.levelno in _LEVEL_NAMES:
            return "%s [%s] %s" % (record.levelname, record.name.split(".")[-1], record.getMessage())
        return "%s: %s" % (record.levelname, record.getMessage())


class _FileFmt(logging.Formatter):
    def format(self, record):
        tag = _LEVEL_NAMES.get(record.levelno, record.levelname)
        return "%(asctime)s %(levelname)s [%(name)s] %(message)s" % {
            "asctime": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "levelname": tag,
            "name": record.name,
            "message": record.getMessage(),
        }


def setup_logging(log_dir: str | Path, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger: console (human) + rotating-ish file log.

    The file handler uses a custom formatter so custom levels render legibly.
    Returns the root logger; modules should use ``logging.getLogger(__name__)``.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Idempotent: avoid stacking handlers if called twice in-process.
    if getattr(root, "_b2b_configured", False):
        return root
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_NoConsoleFmt())
    root.addHandler(console)

    file_path = log_dir / "scraper.log"
    fileh = logging.FileHandler(file_path, encoding="utf-8")
    fileh.setLevel(logging.DEBUG)  # keep full detail in the file
    fileh.setFormatter(_FileFmt())
    root.addHandler(fileh)

    root._b2b_configured = True  # type: ignore[attr-defined]
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
