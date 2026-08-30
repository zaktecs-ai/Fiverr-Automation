"""Normalization primitives for URLs, phones, emails, and text.

These are pure functions so they are trivially unit-testable and have no
global state. They are the single source of truth for canonical keys used by
deduplication and validation.
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl

# Tracking / analytics query parameters that never contribute identity.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "mc_cid",
    "mc_eid", "igshid", "ref", "ref_src", "source", "cmpid", "_ga",
    "_gl", "yclid", "zanpid", "twclid", "wbraid", "gbraid",
    # Yext local-SEO / Google Business Profile attribution params that vendors
    # append to a business's real website URL. They are pure tracking and must
    # never leak into the stored `website` column.
    "sc_cid", "_vsrefdom", "y_source",
}

# Common Google redirect wrappers that resolve to a real destination via `url=`.
_GOOGLE_WRAPPERS = {
    "google.com", "www.google.com", "google.co.uk", "google.ca",
    "maps.google.com", "l.facebook.com", "lm.facebook.com",
}

# Params that look like long echo/redirect payloads and are safe to drop.
_REDUNDANT_PARAM_RE = re.compile(r"^(redirect|url|target|goto|next|return|dest|continue)=.+$", re.I)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _is_bare_ipv6_host(netloc: str) -> bool:
    """True when a netloc is a bare IPv6 literal (multiple colons, no brackets
    and no explicit port). Bracketed hosts, userinfo, and host:port pairs are
    excluded."""
    if not netloc or netloc.startswith("[") or "@" in netloc:
        return False
    # A bare IPv6 literal is the entire netloc with no ":port" suffix.
    if netloc.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(netloc)
        return True
    except ValueError:
        return False


def normalize_text(value) -> str:
    """Collapse whitespace and strip control characters; returns 'N/A' for empty."""
    if value is None:
        return "N/A"
    s = str(value)
    s = "".join(ch for ch in s if ch.isprintable() or ch in "\t ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or "N/A"


def normalize_url(raw: str) -> str:
    """Return a canonical identity URL or 'N/A'.

    Removes scheme casing, default ports, fragments, tracking params, trailing
    slashes, and obvious Google/Facebook redirect wrappers. Preserves the
    meaningful path/query where it actually identifies content.
    """
    if not raw:
        return "N/A"
    if raw is None or str(raw).strip().upper() == "N/A":
        return "N/A"
    raw = raw.strip()
    # Parse first to detect an existing scheme (e.g. mailto:, tel:, javascript:).
    pre = urlsplit(raw)
    if pre.scheme and pre.scheme.lower() not in ("http", "https"):
        return "N/A"
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "N/A"

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return "N/A"

    # A bare IPv6 literal (multiple colons, no brackets) is technically invalid
    # in a URL: `urlsplit(…).hostname` truncates it to the first hextet and
    # `.port` raises ValueError. Re-wrap the whole authority in brackets so the
    # host parses as a single IPv6 address (no port is present in this form).
    netloc = parts.netloc
    if _is_bare_ipv6_host(netloc):
        raw = urlunsplit((scheme, f"[{netloc}]", parts.path, parts.query, parts.fragment))
        parts = urlsplit(raw)

    host = (parts.hostname or "").lower()
    if not host:
        return "N/A"
    # Strip a leading "www." for canonical identity.
    if host.startswith("www."):
        host = host[4:]

    # Unwrap Google/Facebook redirect wrappers when they expose a `url=` param.
    if host in _GOOGLE_WRAPPERS:
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key in ("url", "u", "q", "target"):
            candidate = q.get(key) or q.get(key.lower())
            if candidate and candidate.lower().startswith(("http://", "https://")):
                return normalize_url(candidate)

    # Drop the default port for the scheme. Guard the port access: empty ports
    # (e.g. "http://host:") raise ValueError, which must not crash the pipeline
    # or quality gate. In that case treat the port as absent (scheme default).
    port = None
    try:
        port = parts.port
    except ValueError:
        port = None  # malformed/absent port → keep scheme default
    # Rebuild the netloc. IPv6 hosts must be re-bracketed (urlunsplit does not
    # bracket them), and only a non-default port is appended.
    is_ipv6 = ":" in host
    netloc = f"[{host}]" if is_ipv6 else host
    if port is not None:
        if not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{netloc}:{port}"

    # Strip tracking / redundant params.
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_PARAMS:
            continue
        if kl in ("redirect", "url", "target", "goto", "next", "return", "dest", "continue") and len(v or "") > 200:
            continue
        kept.append((k, v))
    query = "&".join(f"{k}={v}" for k, v in kept)

    # Normalize path: collapse duplicate slashes, drop trailing slashes, and
    # treat a bare root ("/") as empty so the canonical form has no trailing slash.
    path = parts.path or ""
    path = re.sub(r"/{2,}", "/", path)
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, query, ""))


def extract_domain(url: str) -> str:
    """Return a lowercase registrar-level domain (e.g. 'example.co.uk') or ''."""
    norm = normalize_url(url)
    if norm == "N/A":
        return ""
    host = urlsplit(norm).hostname or ""
    return canonical_domain(host)


def canonical_domain(host: str) -> str:
    """Reduce a hostname to its registrable domain using a conservative heuristic.

    Falls back to the last two labels when the public-suffix list is unavailable.
    Handles common two-part country TLDs (co.uk, com.au, co.nz, ...) and common
    multi-label suffixes.
    """
    host = (host or "").lower().strip().strip(".")
    if not host:
        return ""
    # An IP-address host is its own identity — do not label-split it. (Splitting
    # '1.1.1.1' into its last two labels produced the garbage key '1.1', which
    # corrupted dedup domain+city keys and the MX email-verification domain.)
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    _2ND_LEVEL = {  # second-level domain under a country code TLD
        "co", "com", "org", "net", "gov", "edu", "ac", "me", "ltd", "plc",
    }
    _KNOWN_PRIVATE_SUFFIX = {
        "github.io", "pages.dev", "web.app", "firebaseapp.com",
    }
    if len(labels) >= 3 and labels[-2] in _2ND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    if len(labels) >= 3:
        tail = ".".join(labels[-2:])
        if tail in _KNOWN_PRIVATE_SUFFIX:
            return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def normalize_phone(raw: str, default_country: str = "US") -> str:
    """Normalize a phone number to E.164-like digits, country-aware.

    Accepts local US formats when `default_country` is US-like, returning
    digits with a leading country code where determinable. Never fabricates.
    Returns 'N/A' if the input has no digit.
    """
    if raw is None:
        return "N/A"
    s = unicodedata.normalize("NFKD", str(raw))
    digits = re.sub(r"\D", "", s)
    if not digits:
        return "N/A"

    # Determine the country code WITHOUT re-adding it: if the number already
    # carries a leading international prefix, return the digits unchanged;
    # otherwise prefix the default country's code. This prevents the country
    # code from being doubled for already-prefixed numbers (+1…, +92…, +971…,
    # +44…) — which previously caused dedup collisions.
    cc = _guess_country_code(digits, default_country)
    if not cc:
        return digits
    if digits.startswith(cc):
        return digits
    return cc + digits


_COUNTRY_CODES = {
    "US": "1", "CA": "1", "GB": "44", "AU": "61", "NZ": "64",
    "DE": "49", "FR": "33", "IT": "39", "ES": "34", "NL": "31",
    "PK": "92", "IN": "91", "AE": "971", "SG": "65", "IE": "353",
}


def _guess_country_code(digits: str, default_country: str) -> str:
    """Return the country-code digits to reconcile with `digits`.

    Semantics (parse the country code *once*, never double it):
      * If `digits` already carries a recognized international prefix, return
        that prefix (so the caller leaves the number unchanged).
      * Otherwise return the default country's code so the caller can prepend
        it (a local, unprefixed number).
      * Return '' when no code can be determined.

    North-American Numbering Plan (US/CA) is handled by rule, not by the
    ambiguous bare "1" prefix: an 11-digit number starting with "1" is already
    international (1 + NANP), while a 10-digit number is a local number that
    should gain the "1".
    """
    # North-American Numbering Plan resolution MUST come before the foreign
    # prefix match. A 10-digit number under a US/CA default is unambiguously a
    # local NANP number (NANP local numbers are exactly 10 digits) and gains the
    # leading "1". Running the foreign-prefix loop first let a 10-digit Houston
    # number like "3462023432" match Spain's "34" and be left uncoded.
    if len(digits) == 11 and digits.startswith("1"):
        return "1"  # already international (1 + NANP)
    if (default_country or "").upper() in ("US", "CA") and len(digits) == 10:
        return "1"  # local NANP number -> gain the leading country code
    # Longest-match against explicit international prefixes (skip the bare "1",
    # which is too ambiguous to be a reliable prefix by itself).
    for code in sorted(set(_COUNTRY_CODES.values()), key=len, reverse=True):
        if code == "1":
            continue
        if digits.startswith(code):
            return code
    # Unprefixed local number: use the default country's code.
    return _COUNTRY_CODES.get((default_country or "").upper(), "")


def normalize_email(raw: str) -> str:
    """Lowercase and strip whitespace + stray quote characters from an email.

    Extraction can capture a leading/trailing HTML attribute quote (e.g.
    `'infonedallas@myidealdental.com`); strip those so the stored email is clean.
    A single quote/snippet must not wrap a legitimate address in the CSV.
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # Strip one or more leading/trailing quote characters (' " ) — these are
    # HTML/attribute artifacts, never a valid part of the address.
    s = s.lstrip("'\"")
    s = s.rstrip("'\"")
    return s


# ---------------------------------------------------------------------------
# Email validation & dummy/fake filtering
# ---------------------------------------------------------------------------

# A conservative but RFC-reasonable pattern. Rejects spaces, most junk.
_VALID_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

# Domains that are never a business's real inbox (example/test/placeholder TLDs
# and obvious template stand-ins). Deliberately does NOT include real parked
# domains like business.com/company.com, which would wrongly reject real mail.
_DUMMY_DOMAINS = {
    "example.com", "example.org", "example.net", "example.co",
    "test.com", "test.org", "test.net",
    "yourdomain.com", "yoursite.com", "yourwebsite.com", "mywebsite.com",
    "mysite.com", "domain.com", "email.com", "mail.com", "domain.org",
    "website.com", "placeholder.com", "sample.com", "abc.com", "xyz.com",
    "localhost", "localhost.localdomain",
    "foo.com", "foobar.com", "bar.com", "sitename.com",
    "example-email.com", "fake.com",
}

# Usernames that are placeholders regardless of domain.
# NOTE: legitimate role addresses (info@, admin@, webmaster@, sales@, …) are the
# most common real business contact, so they are deliberately NOT here — only
# unambiguous placeholders and no-reply/mailer-daemon addresses are rejected.
_DUMMY_LOCAL = {
    "test", "testing", "example", "sample", "yourname", "name",
    "user", "username", "email", "emailaddress", "you", "your", "someone",
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse",
}

# Substrings that indicate an asset/marketing/email-protection artifact, not a contact.
_JUNK_PATTERNS = [
    re.compile(r"[0-9a-f]{20,}", re.I),                 # hash fragments
    re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|json|woff2?|eot|ttf)$", re.I),
    re.compile(r"@2x\.", re.I),                          # retina asset shorthand
    re.compile(r"^[^@]+@sentry", re.I),
    re.compile(r"^[^@]+@(amp|sqs|s3|cloudfront|cdn)\."),
]

# Personal (free) email providers. These are legitimate contact addresses even
# though their domain differs from the business website, so they are EXEMPT from
# the domain-relationship check (a dentist can list a Gmail address).
PUBLIC_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "zoho.com", "gmx.com",
}

# Disposable / throwaway inboxes — never a real business contact.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "10minutemail.net", "tempmail.com",
    "guerrillamail.com", "yopmail.com", "trashmail.com", "throwawaymail.com",
    "dispostable.com", "sharklasers.com", "maildrop.cc", "temp-mail.org",
    "getairmail.com", "mailnesia.com", "tempinbox.com", "spamgourmet.com",
    "mailnull.com", "mailcatch.com",
}

# Words that strongly suggest a template/placeholder address — only meaningful
# when the email domain doesn't match the business website domain.
_SUSPICIOUS_WORDS = {"theme", "template", "layout", "sample", "fake", "test",
                     "demo", "example", "placeholder", "yourname", "site"}


def is_valid_email(email: str, max_length: int = 120) -> bool:
    """Syntax-validate an email (no DNS)."""
    if not email or len(email) > max_length:
        return False
    return bool(_VALID_EMAIL_RE.match(normalize_email(email)))


def email_rejection_reason(email: str, max_length: int = 120,
                           website_url: str | None = None) -> str | None:
    """Return a human reason an email should be rejected, else None.

    When `website_url` is supplied, an extra domain-relationship check runs:
    an email whose domain differs from the website — and is not a known
    personal provider — is rejected only if it also carries a suspicious word
    (a conservative, legacy-proven heuristic). Personal-provider addresses are
    always allowed regardless of domain mismatch.
    """
    e = normalize_email(email)
    if not e:
        return "empty"
    if len(e) > max_length:
        return "too_long"
    if not _VALID_EMAIL_RE.match(e):
        return "invalid_syntax"
    local, _, domain = e.rpartition("@")
    if domain in _DUMMY_DOMAINS:
        return "dummy_domain"
    if domain in DISPOSABLE_DOMAINS:
        return "disposable_domain"
    if local.lower() in _DUMMY_LOCAL:
        return "dummy_local"
    for pat in _JUNK_PATTERNS:
        if pat.search(e):
            return "suspicious_pattern"

    # Domain relationship check (only when we know the website domain).
    if website_url:
        web_domain = _website_domain(website_url)
        if web_domain and domain != web_domain and domain not in PUBLIC_EMAIL_PROVIDERS:
            if any(w in domain for w in _SUSPICIOUS_WORDS) or \
                    any(w in local.lower() for w in _SUSPICIOUS_WORDS):
                return "unrelated_domain_with_suspicious_word"
    return None


def _website_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_usable_email(email: str, max_length: int = 120,
                    website_url: str | None = None) -> bool:
    """True only when the email passes the full static cleaning pipeline."""
    return email_rejection_reason(email, max_length, website_url) is None
