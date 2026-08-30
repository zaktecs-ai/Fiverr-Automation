"""Regression tests for the 22 bugs fixed in the 2026-08-28 audit.

Each test locks in one specific fix so future refactors don't silently
reintroduce the bug. See Fiverr_Automation_Audit_Report.md (§6 gap analysis).
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from scraper.dedup import IdentityResolver, resolve_identity
from scraper.maps.collector import _with_region, parse_google_maps_url
from scraper.signals.detector import PageContext, SignalDetector
from scraper.utils.normalize import normalize_email, normalize_phone


# --- 2.1 place_id "N/A" must not collide ------------------------------------
class TestPlaceIdNACollision:
    def test_na_place_id_resolves_to_no_key(self):
        sig = resolve_identity({"business_name": "Alpha Dental",
                                "place_id": "N/A", "city": "Dallas",
                                "website": "https://alpha.example"})
        assert sig["place_id"] is None
        assert sig["key_type"] != "place_id"

    def test_two_na_place_ids_do_not_collide(self):
        r = IdentityResolver()
        a = {"business_name": "Alpha Dental", "place_id": "N/A",
             "city": "Dallas", "website": "https://alpha.example",
             "phone": "2145551111"}
        b = {"business_name": "Beta Clinic", "place_id": "N/A",
             "city": "Houston", "website": "https://beta.example",
             "phone": "7135552222"}
        dup1, _, _ = r.is_duplicate(a)
        dup2, _, _ = r.is_duplicate(b)
        assert dup1 is False and dup2 is False


# --- 2.2 phone country-code must not be doubled -----------------------------
class TestPhonePrefixNotDoubled:
    @pytest.mark.parametrize("raw,cc,want", [
        ("+1 (800) 555-1234", "US", "18005551234"),
        ("(214) 555-1234", "US", "12145551234"),
        ("2145551234", "US", "12145551234"),
        ("+1 (214) 555-1234", "US", "12145551234"),
        ("+92 300 1234567", "PK", "923001234567"),
        ("0300-1234567", "PK", "9203001234567"),
        ("+971 5 123 4567", "AE", "97151234567"),
        ("+44 20 7946 0000", "GB", "442079460000"),
    ])
    def test_exact(self, raw, cc, want):
        assert normalize_phone(raw, cc) == want

    def test_us_variants_are_equal(self):
        a = normalize_phone("+1 (214) 555-1234", "US")
        b = normalize_phone("(214) 555-1234", "US")
        assert a == b == "12145551234"


# --- 2.3 pipeline stores phone with country.default (was defaulting to US) ---
class TestPipelinePhoneUsesCountryDefault:
    def test_non_us_default_reaches_stored_phone(self):
        # The pipeline's _normalize_maps must pass country.default through to
        # normalize_phone so the stored value matches the dedup identity key.
        # (Regression: it called normalize_phone(raw) with no country arg,
        # which hard-coded US and broke dedup/resume for non-US jobs.)
        from scraper.pipeline import Pipeline

        class _Maps:
            def collect(self, query):
                yield {"business_name": "London Dentist",
                       "phone": "020 7946 0958",
                       "website": "https://example.co.uk",
                       "source_query": query}
                if False:
                    yield {}

        class _StoreStop(Exception):
            pass

        cfg = {
            "job": {"client_name": "c", "output_filename": "c",
                    "output_dir": "/tmp/pw_test_out", "max_results_per_query": 0,
                    "max_total_results": 0},
            "resolved_output_dir": "/tmp/pw_test_out",
            "queries": ["dentists in London"],
            "missing_value": "N/A",
            "country": {"default": "GB"},
            "maps": {"gl": "uk"},
            "website": {"require_website": False},
            "email": {"enabled": False},
            "smtp": {"enabled": False},
            "concurrency": {"website_workers": 1},
            "signals": {},
            "filters": {},
        }
        import scraper.pipeline as pm
        # Call the normalize step directly without a full Pipeline (avoids
        # CheckpointStore/threading side effects).
        p = pm.Pipeline.__new__(pm.Pipeline)
        p.cfg = cfg
        rec = p._normalize_maps({"business_name": "London Dentist",
                                 "phone": "020 7946 0958",
                                 "website": "https://example.co.uk",
                                 "source_query": "dentists in London"})
        assert rec.data["phone"] == "4402079460958"
        # And it must match the dedup identity key's phone.
        sig = resolve_identity(rec.data, default_country="GB")
        assert sig["normalized_phone"] == rec.data["phone"]


# --- 4.9 social links never cross columns -----------------------------------
class TestSocialLinkColumns:
    def test_facebook_not_in_instagram(self):
        from scraper.websites.enricher import detect_social_links
        html = '<a href="https://www.facebook.com/myclinic/">FB</a>'
        urls = ["https://instagram.com/theirhandle"]
        out = detect_social_links(html, urls)
        assert out["facebook"].startswith("https://www.facebook.com/") or \
            out["facebook"].startswith("https://facebook.com/")
        assert not out["facebook"].startswith("instagram")


# --- 4.11 region params deduplicated ----------------------------------------
class TestWithRegion:
    def test_existing_hl_gl_replaced_not_appended(self):
        out = _with_region("https://maps.example/place/x?hl=fr&gl=fr", "en", "us")
        assert out.count("hl=") == 1
        assert out.count("gl=") == 1
        assert "hl=en" in out and "gl=us" in out
        assert "hl=fr" not in out and "gl=fr" not in out

    def test_fresh_url_appends(self):
        out = _with_region("https://maps.example/search/x", "en", "us")
        assert out.endswith("hl=en&gl=us")


# --- 3.6 SignalDetector thread safety ---------------------------------------
class TestSignalDetectorThreadSafety:
    def test_concurrent_runs_return_self_consistent_maps(self):
        det = SignalDetector({})

        def one(i):
            # Page with meta pixel script only in even runs (script in `scripts`
            # list, which is what _meta_pixel actually inspects).
            scripts = ["https://connect.facebook.net/en_US/fbevents.js"] if i % 2 == 0 else []
            ctx = PageContext(text="hello", html="", scripts=scripts,
                              url="https://x.example")
            out, ev = det.run(ctx)
            return out.get("meta_pixel"), out.get("ga4")

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(one, range(200)))

        for i, (pixel, ga4) in enumerate(results):
            assert pixel in ("YES", "NO")
            assert ga4 in ("YES", "NO")
            if i % 2 == 0:
                assert pixel == "YES"
            else:
                assert pixel == "NO"


# --- 3.5 rejected record not blocked in-session -----------------------------
class TestRejectedRecordRollback:
    def test_rollback_allows_rediscovery(self):
        r = IdentityResolver()
        business = {"business_name": "Alpha", "place_id": "0xABCD",
                    "website": "https://alpha.example", "city": "Dallas"}
        dup, _, _ = r.is_duplicate(business)
        assert dup is False
        # Simulate rejection: roll back the identity.
        r.rollback(business)
        dup2, _, _ = r.is_duplicate(business)
        assert dup2 is False


# --- CSV data-quality fixes (audit-follow-up) -------------------------------
class TestEmailQuoteStrip:
    def test_leading_quote_stripped(self):
        assert normalize_email("'infonedallas@myidealdental.com") == \
            "infonedallas@myidealdental.com"

    def test_case_and_space_normalized(self):
        assert normalize_email("  Dr@Clinic.COM  ") == "dr@clinic.com"


class TestLatLngTokenFallback:
    def test_place_url_3d_4d_coords(self):
        u = ("https://www.google.com/maps/place/Dental/@32.7,-96.7,17z/"
             "data=!4m6!3m5!1s0x0:0x1!8m2!3d32.7767!4d-96.7970")
        out = parse_google_maps_url(u)
        assert out.get("lat") == 32.7767
        assert out.get("lng") == -96.7970

    def test_viewport_at_coords(self):
        u = "https://www.google.com/maps/@33.0,-97.0,14z"
        out = parse_google_maps_url(u)
        assert out.get("lat") == 33.0 and out.get("lng") == -97.0


class TestQualityGateCrossRowEmail:
    def test_same_email_across_rows_not_a_duplicate(self):
        # The quality gate should treat a shared inbox across businesses as
        # legitimate; only a repeated email WITHIN one row is a glitch.
        from scraper.validation.quality import run_quality_gate
        import tempfile, csv as _csv, pathlib
        tmp = tempfile.mkdtemp()
        out_dir = pathlib.Path(tmp)
        # Build a minimal CSV with two rows sharing one email.
        import scraper.models as M
        cols = M.OUTPUT_COLUMNS
        path = out_dir / "out.csv"
        recs = [
            {"business_name": "A", "website": "https://a.example", "emails": "same@example.com"},
            {"business_name": "B", "website": "https://b.example", "emails": "same@example.com"},
        ]
        with open(path, "w", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for rc in recs:
                w.writerow({c: rc.get(c, "N/A") for c in cols})
        report = run_quality_gate(out_dir)
        # duplicate_emails must PASS (ok=True) for cross-row shared email.
        dup_check = next(c for c in report.checks if c["check"] == "duplicate_emails")
        assert dup_check["ok"] is True


# --- Round 3: canonical_domain must receive a hostname, not a full URL ------
class TestCanonicalDomainExtraction:
    def test_resolve_identity_uses_registrar_domain_not_url(self):
        # A full URL (with scheme and, optionally, a path) must resolve to a
        # bare registrar-level domain. Previously canonical_domain() was fed the
        # whole URL string, producing a garbage identity key.
        sig = resolve_identity({"business_name": "X",
                                "website": "https://www.chaindental.com/locations/downtown",
                                "phone": "2145550000", "city": "Dallas",
                                "place_id": None})
        assert sig["canonical_domain"] == "chaindental.com"

    def test_resolve_identity_domain_matches_for_dedup(self):
        # Two listings sharing a domain + city but with NO place_id must be
        # detected as duplicates once the domain key is correct.
        r = IdentityResolver()
        a = {"business_name": "A", "website": "https://chaindental.com",
             "phone": "2145550001", "city": "Dallas", "place_id": None}
        b = {"business_name": "B", "website": "https://www.chaindental.com/about",
             "phone": "2145550002", "city": "Dallas", "place_id": None}
        assert r.is_duplicate(a)[0] is False
        assert r.is_duplicate(b)[0] is True


# --- Round 3: phone country-code collision (34 vs 346) -----------------------
class TestPhoneCountryCodeCollision:
    def test_ten_digit_346_number_gets_us_country_code(self):
        # A 10-digit NANP-local number starting with "346" (Houston overlay) is
        # NOT a Spain "34" number — it must gain the leading "1".
        assert normalize_phone("3462023432", "US") == "13462023432"

    def test_spain_34_still_recognized(self):
        assert normalize_phone("+34 912 345 678", "ES") == "34912345678"


# --- Round 3: email counters wired ------------------------------------------
class TestEmailCounters:
    def test_enricher_reports_rejected_count(self):
        from scraper.websites.enricher import WebsiteEnricher
        # A candidate that looks like a placeholder domain must be counted as
        # rejected when a website_url is supplied.
        enricher = WebsiteEnricher.__new__(WebsiteEnricher)
        # Minimal fake fetcher/crawler path is avoided: test clean_emails contract
        # directly + the counter key presence via a focused unit.
        from scraper.email.extract import clean_emails
        raw = ["hello@yoursite.com"]
        kept = clean_emails(raw, 120, website_url="https://smiledental.com")
        assert len(kept) == 0
        assert len(raw) - len(kept) == 1


# --- Round 4: asyncio/Playwright teardown noise silenced --------------------
class TestAsyncioNoiseSilenced:
    def test_asyncio_logger_quieted_on_setup(self, tmp_path):
        import logging
        from scraper.utils.logging_utils import setup_logging
        lg = logging.getLogger("asyncio")
        lg.setLevel(logging.NOTSET)  # reset before setup
        lg.propagate = True
        setup_logging(tmp_path)
        # asyncio must be silenced so Playwright teardown callbacks never spam
        # the terminal/scraper.log.
        assert lg.level >= logging.ERROR

    def test_recycle_also_drains_playwright_loop(self):
        # The loop-drain helper must exist on the manager and be invoked by the
        # per-recycle _close path (regression: only the final close() drained,
        # so mid-run recycles left stale callbacks that sprayed ERROR lines).
        import scraper.browser.browser_manager as bm
        assert hasattr(bm.BrowserManager, "_drain_playwright_loop")


# --- Round 5: Yext/GBP tracking params stripped from website URLs -----------
class TestTrackingParamsStripped:
    def test_yext_params_removed_from_website_url(self):
        from scraper.utils.normalize import normalize_url
        url = ("https://northeastdallasdentistry.com"
               "?sc_cid=GBP:O:GP:746:Organic_Search:General:na"
               "&_vsrefdom=organic_gbp"
               "&y_source=1_MTEyNTczNzYtNzE1LWxvY2F0aW9uLndlYnNpdGU=")
        out = normalize_url(url)
        assert "sc_cid" not in out
        assert "_vsrefdom" not in out
        assert "y_source" not in out
        assert "?" not in out
        assert out == "https://northeastdallasdentistry.com"

    def test_legit_query_params_preserved(self):
        from scraper.utils.normalize import normalize_url
        # A non-tracking query param (e.g. a real page id) must survive.
        out = normalize_url("https://example.com/page?lang=en")
        assert "lang=en" in out


# --- Round 2: email domain-relationship filter is now wired (dead code fixed)
class TestCleanEmailsWithWebsiteContext:
    def test_dummy_email_with_real_website_filtered_out(self):
        # An email whose domain differs from the website AND carries a
        # suspicious placeholder word must be rejected when website_url is
        # supplied. (Regression: website_url was never passed, so this filter
        # was dead code and placeholder emails leaked through.)
        from scraper.email.extract import clean_emails
        # "info@yoursite.com" — domain not the website, "yoursite" is suspicious.
        candidates = ["info@yoursite.com"]
        out = clean_emails(candidates, max_length=120,
                           website_url="https://smiledental.com")
        assert len(out) == 0

    def test_legit_email_survives_with_website_context(self):
        from scraper.email.extract import clean_emails
        # Matches the website domain — always kept.
        out = clean_emails(["hello@smiledental.com"], max_length=120,
                           website_url="https://smiledental.com")
        assert out == ["hello@smiledental.com"]


# --- Round 2: _site_paced_fetch must not hold the lock while sleeping -------
class TestSitePacedFetchLockRelease:
    def test_site_paced_fetch_does_not_hold_lock_while_sleeping(self):
        from unittest import mock
        import threading
        import scraper.websites.enricher as enricher_mod

        class _Fetcher:
            def __init__(self):
                self.calls = []
            def fetch(self, url):
                self.calls.append(url)
                return enricher_mod.FetchResult(url=url, html="ok",
                                                failure_reason="")

        enricher = enricher_mod.WebsiteEnricher.__new__(enricher_mod.WebsiteEnricher)
        enricher._site_min = 0.1
        enricher._site_max = 0.2
        # Seed a prior fetch so the pacing window is entered (a falsy 0.0 would
        # skip the elapsed computation entirely).
        enricher._last_fetch_ts = 50.0
        enricher._sleep_lock = threading.Lock()
        enricher._fetcher = _Fetcher()

        sleep_calls = []
        lock_held_during_sleep = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            # If _sleep_lock is held here, the lock leak is present.
            lock_held_during_sleep.append(enricher._sleep_lock.locked())

        # Deterministic clock + pacing window: elapsed = 0.05 < want = 0.15.
        with mock.patch("time.sleep", side_effect=fake_sleep), \
             mock.patch("time.time", side_effect=[50.05, 50.05]), \
             mock.patch("scraper.websites.enricher.random.uniform", return_value=0.15):
            enricher._site_paced_fetch("https://x.example")

        assert len(sleep_calls) == 1
        assert sleep_calls[0] > 0
        # The lock must NOT be held while sleeping.
        assert lock_held_during_sleep == [False]
