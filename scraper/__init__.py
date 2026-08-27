"""B2B Lead Scraper Engine — modular, resumable, production-grade."""

__version__ = "1.0.0"

# Register custom log levels (CHECKPOINT, RETRY, TIMEOUT, ...) on the stdlib
# Logger so every module can use log.checkpoint()/log.timeout()/etc. without
# each one importing the logging utils explicitly.
from .utils import logging_utils as _logging  # noqa: F401
