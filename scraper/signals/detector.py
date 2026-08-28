"""Configurable signal detection engine.

A signal detector runs multiple evidence strategies over a page's collected
context (normalized text, URLs, script/src patterns, structured data, tech
signals) and returns detected + evidence. Built-in detectors cover the common
lead-gen signals; operators can add custom signals in YAML without editing code.

Custom signal schema (per config.yaml `signals:`):
    established_business:
      enabled: true
      keywords: ["family owned", "established in"]
      regex: ["since\\s+(19|20)\\d{2}"]
      match_logic: ANY     # ANY (default) or ALL
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PageContext:
    """Everything a detector may inspect for a single page."""
    text: str = ""                      # normalized visible + meta text
    html: str = ""                      # raw HTML (for script/tag patterns)
    url: str = ""                       # current page URL
    urls: list[str] = field(default_factory=list)   # all discovered URLs on page
    scripts: list[str] = field(default_factory=list)  # script src attributes
    meta: dict = field(default_factory=dict)         # meta tags
    technologies: set[str] = field(default_factory=set)  # tech detector output
    structured: str = ""                # JSON-LD / microdata text


# Built-in signal definitions. Each entry maps outcome fields to detection fns.
# Detection functions return (detected: bool, evidence: str|None).

def _kw(pattern: str):
    return re.compile(re.escape(pattern), re.I)


def _any_text(text: str, patterns) -> tuple[bool, str | None]:
    for p in patterns:
        if p.search(text):
            return True, p.pattern
    return False, None


def _any_src(scripts, pattern) -> tuple[bool, str | None]:
    p = re.compile(pattern, re.I)
    for s in scripts:
        if p.search(s):
            return True, s
    return False, None


def _meta_pixel(ctx: PageContext):
    hit, ev = _any_src(ctx.scripts, r"connect\.facebook\.net|facebook\.com/tr|fbq\(")
    if hit:
        return True, ev or "facebook pixel script detected"
    if re.search(r"fbq\(", ctx.html, re.I):
        return True, "facebook pixel script detected"
    return False, None


def _ga4(ctx: PageContext):
    if re.search(r"gtag\(|googletagmanager\.com/gtag|G-[A-Z0-9]{6,}", ctx.html, re.I):
        return True, "GA4/gtag measurement script detected"
    hit, ev = _any_src(ctx.scripts, r"googletagmanager\.com/gtag|google-analytics\.com")
    if hit:
        return True, ev
    return False, None


def _gtm(ctx: PageContext):
    hit, ev = _any_src(ctx.scripts, r"googletagmanager\.com/gtm\.js")
    if hit:
        return True, ev or "Google Tag Manager script detected"
    if re.search(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]+", ctx.html, re.I):
        return True, "Google Tag Manager detected"
    return False, None


def _booking(ctx: PageContext):
    booking_domains = re.compile(
        r"calendly\.com|acuityscheduling\.com|booksy\.com|mindbodyonline\.com|"
        r"vagaro\.com|fresha\.com|setmore\.com|appointy\.com|square\.up/site|"
        r"youcanbook\.me|simplybook\.me|schedulicity\.com", re.I)
    hit, ev = _any_text(ctx.text + "\n" + "\n".join(ctx.urls) + "\n" + ctx.html, [booking_domains])
    return (hit, ev)


def _chat_widget(ctx: PageContext):
    if re.search(r"tawk\.to|intercom|drift\.com|livechatinc\.com|zopim|crisp\.chat|"
                 r"hubspot\.com/forms|chat-widget|freshchat|zendesk", ctx.html, re.I):
        return True, "chat widget detected"
    hit, ev = _any_src(ctx.scripts, r"tawk\.to|intercom|drift|livechat|zopim|crisp\.chat|hubspot")
    return (hit, ev)


def _analytics(ctx: PageContext):
    if re.search(r"google-analytics|analytics\.js|segment\.com|mixpanel|hotjar|matomo|"
                 r"clarity\.ms", ctx.html, re.I):
        return True, "analytics script detected"
    return False, None


def _pricing(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(price|pricing|rates|fee|quote|estimate|cost)\b", re.I),
    ])
    return (hit, ev or "pricing keywords")


def _financing(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(financing|payment plans?|0% apr|installments?|"
                   r"buy now pay later|affirm|klarna|afterpay)\b", re.I),
    ])
    return (hit, ev)


def _licensed_insured(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(licensed(?:\s*&\s*insured)?|bonded|insured|certified)\b", re.I),
    ])
    return (hit, ev)


def _established(ctx: PageContext):
    m = re.search(r"\b(since|established|est\.?)\s+(19|20)\d{2}\b", ctx.text, re.I)
    if m:
        return True, m.group(0)
    m2 = re.search(r"\b(?:in business|serving\w*)\s+(?:for\s+)?(\d+)\s*(?:years|yrs)\b",
                   ctx.text, re.I)
    if m2:
        return True, m2.group(0)
    return False, None


def _portfolio(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(portfolio|gallery|our work|case stud|projects|before.after)\b", re.I),
    ])
    return (hit, ev)


def _mobile_service(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(mobile service|we come to you|on-site|at your home|"
                   r"house calls|we travel to)\b", re.I),
    ])
    return (hit, ev)


def _membership(ctx: PageContext):
    hit, ev = _any_text(ctx.text, [
        re.compile(r"\b(membership|subscription|monthly plan|join now|club)\b", re.I),
    ])
    return (hit, ev)


# Built-in detectors: name -> (output_fields, detector_fn)
DEFAULT_SIGNALS: dict[str, dict] = {
    "meta_pixel":    {"fields": ["meta_pixel"], "fn": _meta_pixel},
    "ga4":           {"fields": ["ga4"], "fn": _ga4},
    "gtm":           {"fields": ["gtm"], "fn": _gtm},
    "analytics":     {"fields": ["analytics"], "fn": _analytics},
    "booking_system": {"fields": ["booking_system"], "fn": _booking},
    "chat_widget":   {"fields": ["chat_widget"], "fn": _chat_widget},
    "pricing":       {"fields": ["signal_pricing"], "fn": _pricing},
    "financing":     {"fields": ["signal_financing"], "fn": _financing},
    "licensed_insured": {"fields": ["signal_licensed_insured"], "fn": _licensed_insured},
    "established":   {"fields": ["signal_established"], "fn": _established},
    "portfolio":     {"fields": ["signal_portfolio"], "fn": _portfolio},
    "mobile_service": {"fields": ["signal_mobile_service"], "fn": _mobile_service},
    "membership":    {"fields": ["signal_membership"], "fn": _membership},
}


class SignalDetector:
    def __init__(self, custom_signals: dict | None = None):
        # `_custom` is read-only after construction; run-time results use local
        # dicts so concurrent `run()` calls (thread-pool enrichment) never share
        # mutable state — which previously attributed signals to the wrong site.
        self._custom = custom_signals or {}

    def run(self, ctx: PageContext) -> tuple[dict[str, str], dict[str, str]]:
        """Run all enabled built-in + custom detectors over a PageContext.

        Returns (outcome_fields, evidence). Outcome values are 'YES'/'NO';
        evidence holds the detector's human-readable reason for YES. Both dicts
        are freshly allocated per call, making the detector thread-safe.
        """
        outcome: dict[str, str] = {}
        evidence: dict[str, str] = {}
        for name, spec in DEFAULT_SIGNALS.items():
            detected, ev = spec["fn"](ctx)
            for field in spec["fields"]:
                outcome[field] = "YES" if detected else "NO"
            if detected and ev:
                evidence[name] = ev

        for name, spec in self._custom.items():
            if not isinstance(spec, dict) or not spec.get("enabled", True):
                continue
            detected, ev = self._eval_custom(spec, ctx)
            outcome[f"signal_{name}"] = "YES" if detected else "NO"
            if detected and ev:
                evidence[name] = ev

        return outcome, evidence

    def _eval_custom(self, spec: dict, ctx: PageContext) -> tuple[bool, str | None]:
        keywords = spec.get("keywords", []) or []
        regexes = spec.get("regex", []) or []
        tags = spec.get("tags", []) or []
        haystack = (ctx.text + " " + ctx.html).lower()

        keyword_hits = [k for k in keywords if str(k).lower() in haystack]
        regex_hits = []
        for pattern in regexes:
            try:
                p = re.compile(pattern, re.I)
            except re.error:
                continue
            if p.search(ctx.text) or p.search(ctx.html):
                regex_hits.append(pattern)
        tag_hits = [t for t in tags if t in ctx.technologies]

        match_logic = spec.get("match_logic", "ANY").upper()
        if match_logic == "ALL":
            distinct_hits = bool(keyword_hits) + bool(regex_hits) + bool(tag_hits)
            # ALL means: every provided rule category that had rules must fire.
            provided = bool(keywords) + bool(regexes) + bool(tags)
            detected = (distinct_hits == provided) and provided > 0
        else:  # ANY (default)
            detected = bool(keyword_hits or regex_hits or tag_hits)

        evidence = ", ".join(keyword_hits + regex_hits + tag_hits) or None
        return detected, evidence
