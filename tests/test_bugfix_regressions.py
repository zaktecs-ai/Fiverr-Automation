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
