"""Optional MX and SMTP verification.

MX:           DNS lookup cached per-domain. No smtp dependency.
SMTP:         Direct socket verification (HELO -> MAIL FROM -> RCPT TO),
              explicit statuses, never converts uncertainty into certainty.
              Not aggressively parallelized; failure-safe.

Both are OFF by default and the engine runs normally without them.
"""
from __future__ import annotations

import logging
import random
import smtplib
import time

from ..utils.dns_cache import DNSCache
from ..utils.normalize import extract_domain

try:
    import dns.resolver
    _HAS_DNS = True
except ImportError:  # pragma: no cover
    _HAS_DNS = False

log = logging.getLogger(__name__)

_MX_CACHE = DNSCache(max_size=50_000, ttl=3600)


class MXChecker:
    """Cached MX lookup using dnspython (no safe fallback without it)."""

    def __init__(self, enabled: bool = False, timeout: float = 5.0):
        self.enabled = enabled
        self.timeout = timeout

    def check(self, email: str) -> tuple[str, str]:
        """Return (status, reason). status in {'PASS','FAIL','INCONCLUSIVE'}."""
        if not self.enabled:
            return "NOT_CHECKED", "mx_disabled"
        domain = extract_domain("https://" + email.rsplit("@", 1)[-1]) if "@" in email else ""
        if not domain:
            return "FAIL", "no_domain"
        key = domain
        cached = _MX_CACHE.get(key)
        if cached is not None:
            return cached
        result = self._lookup(domain)
        _MX_CACHE.set(key, result)
        return result

    def _lookup(self, domain: str) -> tuple[str, str]:
        if _HAS_DNS:
            try:
                answers = dns.resolver.resolve(domain, "MX", lifetime=self.timeout)
                if answers:
                    return "PASS", f"mx_records={len(answers)}"
                return "FAIL", "no_mx_records"
            except dns.resolver.NoAnswer:
                return "FAIL", "no_mx_records"
            except dns.resolver.NXDOMAIN:
                return "FAIL", "nxdomain"
            except Exception as e:
                return "INCONCLUSIVE", f"dns_error:{type(e).__name__}"
        # Without dnspython there is no reliable MX lookup: an A-record connect
        # to port 25 is NOT an MX check and would wrongly "PASS" any host with
        # SMTP open. Fail honestly instead.
        return "INCONCLUSIVE", "no_dns_library"


class SMTPVerifier:
    """Direct SMTP verification with explicit, non-guaranteed statuses."""

    def __init__(self, enabled: bool = False, timeout: float = 15.0,
                 from_email: str = "verify@example.com", retries: int = 1):
        self.enabled = enabled
        self.timeout = timeout
        self.from_email = from_email
        self.retries = retries
        self._mx_cache: dict[str, tuple[str, str]] = {}

    def verify(self, email: str, mx: tuple[str, str] | None = None) -> tuple[str, str]:
        """Return (status, reason). See models.SMTP_STATUSES for the status set."""
        if not self.enabled:
            return "Not Checked", "smtp_disabled"
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if not domain:
            return "Invalid", "no_domain"

        mx_status, mx_reason = self._mx_status(domain, mx)
        if mx_status != "PASS":
            return "Invalid", f"mx_{mx_reason}"

        hosts = self._mx_hosts(domain)
        if not hosts:
            return "Invalid", "no_mx_hosts"

        # Retry transient/inconclusive outcomes up to `retries` extra times.
        last_status = "Inconclusive"
        last_reason = "unreachable"
        for attempt in range(self.retries + 1):
            outcome = self._try_hosts(hosts, email)
            status, reason = outcome
            if status in ("Verified", "Invalid"):
                return status, reason
            last_status, last_reason = status, reason
            if attempt < self.retries:
                log.retry("smtp %s inconclusive (%s), retry %d/%d",
                          email, status, attempt + 1, self.retries)
        return last_status, last_reason

    def _try_hosts(self, hosts: list[str], email: str) -> tuple[str, str]:
        last_status, last_reason = "Inconclusive", "unreachable"
        for host in hosts:
            try:
                status, reason = self._smtp_transaction(host, email)
                if status in ("Verified", "Invalid"):
                    return status, reason
                last_status, last_reason = status, reason
            except Exception as e:
                last_status, last_reason = "Connection Failed", type(e).__name__
        return last_status, last_reason

    def _mx_status(self, domain: str, mx: tuple[str, str] | None) -> tuple[str, str]:
        if mx is not None:
            return mx
        if domain in self._mx_cache:
            return self._mx_cache[domain]
        checker = MXChecker(enabled=True, timeout=5.0)
        result = checker.check("x@" + domain)
        self._mx_cache[domain] = result
        return result

    def _mx_hosts(self, domain: str) -> list[str]:
        if _HAS_DNS:
            try:
                import dns.resolver
                answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
                # Sort by MX preference (lower = higher priority), then exchange.
                return [str(r.exchange).rstrip(".") for r in
                        sorted(answers, key=lambda r: (r.preference, str(r.exchange)))]
            except Exception:
                return []
        return []

    def _smtp_transaction(self, host: str, email: str) -> tuple[str, str]:
        with smtplib.SMTP(timeout=self.timeout) as smtp:
            smtp.set_debuglevel(0)
            code, _ = smtp.connect(host, 25)
            if code != 220:
                return "Connection Failed", f"greeting_{code}"
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            smtp.mail(self.from_email)
            code, resp = smtp.rcpt(email)
            if code == 250:
                return "Verified", "rcpt_250"
            if code == 550:
                return "Invalid", "rcpt_550"
            if code == 452:
                return "Inconclusive", "greylisted_452"
            return "Inconclusive", f"rcpt_{code}"
