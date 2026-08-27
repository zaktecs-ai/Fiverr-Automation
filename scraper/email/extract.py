"""Email extraction and static cleaning.

Extraction sources (priority order, per page):
  1. mailto: links
  2. visible HTML text (regex over a light email pattern)
  3. JSON-LD + microdata structured data
  4. inline <script> blocks (for obfuscated/JS-rendered addresses)
  5. rendered DOM text (supplied by the Playwright path when used)

Cleaning pipeline (all static; no network):
  normalize -> syntax validate -> dummy/fake domain -> suspicious pattern ->
  asset/path junk -> length cap. See normalize.py for the rejection rules.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..utils.normalize import email_rejection_reason, normalize_email, is_usable_email

# Broad capture regex: finds email-shaped tokens in arbitrary text.
_EMAIL_TOKEN_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?\.[A-Za-z]{2,63}",
)

# Common image extensions and asset paths that look like emails but aren't.
_PROTECTED_MARKERS = ["[at]", "(at)", "[dot]", "(dot)", " at ", " (at) ", "&#64;", "@&#8203;"]


def _decode_obfuscated(text: str) -> str:
    t = text
    t = t.replace("&#64;", "@").replace("&commat;", "@").replace("@&#8203;", "@")
    t = re.sub(r"\s*\[at\]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*\(at\)\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*\[dot\]\s*", ".", t, flags=re.I)
    t = re.sub(r"\s*\(dot\)\s*", ".", t, flags=re.I)
    t = re.sub(r"\s+at\s+", "@", t, flags=re.I)
    return t


def extract_emails_from_text(text: str) -> list[str]:
    """Extract unique usable emails from arbitrary text."""
    if not text:
        return []
    decoded = _decode_obfuscated(text)
    found: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_TOKEN_RE.finditer(decoded):
        candidate = normalize_email(m.group(0))
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


def extract_emails(html: str | None, rendered_text: str = "", url: str = "") -> list[str]:
    """Extract emails from an HTML page (and optional rendered DOM text).

    Returns an ordered list of unique raw candidate emails (pre-cleaning).
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(emails):
        for e in emails:
            ne = normalize_email(e)
            if ne and ne not in seen:
                seen.add(ne)
                candidates.append(ne)

    if html:
        soup = BeautifulSoup(html, "lxml")
        # 1. mailto links — highest confidence.
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href.lower().startswith("mailto:"):
                add([href[7:].split("?")[0]])
        # 2. JSON-LD structured data.
        for script in soup.find_all("script", type="application/ld+json"):
            add(extract_emails_from_text(script.get_text()))
        # 3. Inline scripts (obfuscated emails often live here).
        for script in soup.find_all("script"):
            if not script.get("src"):
                add(extract_emails_from_text(script.get_text()))
        # 4. Visible text of the whole document.
        add(extract_emails_from_text(soup.get_text(" ")))
        # 5. mailto in raw html as fallback (covers href attributes missed above).
        for m in re.finditer(r"mailto:([^\"'>\s]+)", html, re.I):
            add([m.group(1).split("?")[0]])

    # 6. Rendered DOM text (from Playwright) if provided.
    if rendered_text:
        add(extract_emails_from_text(rendered_text))

    return candidates


def clean_emails(candidates: list[str], max_length: int = 120) -> list[str]:
    """Apply the static cleaning pipeline; returns only usable emails, ordered."""
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        e = normalize_email(c)
        if not e or e in seen:
            continue
        seen.add(e)
        if is_usable_email(e, max_length):
            out.append(e)
    return out
