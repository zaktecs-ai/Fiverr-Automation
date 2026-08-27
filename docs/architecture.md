# Architecture & Design Decisions

This document records the important engineering decisions behind the scraper
engine, so future maintainers understand *why* things are the way they are.

---

## 1. The pipeline

The engine is a **sequential pipeline of independent modules**:

```
CONFIG → JOB MANAGER → QUERY MANAGER → GOOGLE MAPS COLLECTOR → DATA
NORMALIZER → EARLY DEDUPLICATION → MAPS FILTER → WEBSITE FETCHER (HTTP-first)
→ PLAYWRIGHT FALLBACK → SMART CRAWLER → EMAIL EXTRACT/CLEAN → TECH DETECT →
SIGNAL DETECT → OPTIONAL MX → OPTIONAL SMTP → VALIDATION → ATOMIC CSV COMMIT
→ CHECKPOINT UPDATE → [final] QUALITY GATE → XLSX + SUMMARY + LOGS
```

Queries are processed **sequentially** (per spec). Within a query, website
enrichment uses a **bounded** thread pool (default 4 workers). This keeps
per-query ordering deterministic for checkpointing while still using the VPS
CPU for independent website fetches.

---

## 2. Resumable state: SQLite, not a JSON blob

**Decision:** the checkpoint is a **SQLite database** (`checkpoint.db`) with
WAL journaling, not a single giant JSON file.

**Rationale:**
- Transactional integrity — a mid-write crash can't corrupt prior state.
- Fine-grained queries (per-query status, per-record stage, per-counter).
- A JSON *mirror* (`checkpoint.json`) is still written for human inspection,
  plus a `.backup` copy and a `.sqlite.bak` after each job.

**State tracked per record:** identity key, place ID, canonical domain,
normalized phone, source query, processing **stage** (`discovered` →
`accepted` → `committed` / `filtered` / `rejected`), and committed CSV row
offset. On restart, committed records are skipped, and the `IdentityResolver`
is re-seeded from the store so dedup continues correctly across restarts.

**CSV as source of truth:** rows are appended, flushed, and `fsync`'d *before*
the checkpoint advances. Recovery trims a malformed trailing partial row.
Because CSV is append-only and checkpoint is transactional, the two can never
desync by more than one in-flight record, which is reconciled on restart.

---

## 3. Deduplication / identity resolution

A single "same website" is **not** treated as "same business", because
multi-location chains and branches share a domain. Resolution used a
**priority ladder** of composite identity signals:

1. **place_id** (Google's own identity) — strongest.
2. **canonical domain + city** — same site *and* same market ⇒ same branch.
3. **normalized phone** — a phone rarely belongs to two distinct businesses.
4. **normalized name + city** — fallback.

**Policy:** FIRST VALID RECORD WINS. The first record seen is kept; later
collisions are dropped as duplicates (with a reason). Dedup runs as early as
possible — right after Maps collection and normalization — so duplicate
records never trigger expensive website enrichment.

---

## 4. Status model: never conflate "blocked" with "dead"

`website_status` ∈ {`LIVE`, `DEAD`}. `website_failure_reason` ∈ a rich set:

- `HTTP_BLOCKED`, `CAPTCHA_DETECTED`, `JS_REQUIRED`, `TIMEOUT` →
  **LIVE (but not fetched)** — temporary/scraper-side.
- `DNS_FAILURE`, `CONNECTION_REFUSED`, `NOT_FOUND`, `TLS_ERROR` →
  **DEAD** — strong evidence the site is gone.

This mapping is centralized in `models.resolve_website_status`, and the
quality gate flags any contradiction (e.g. `DEAD` + `HTTP_BLOCKED`).

---

## 5. HTTP-first, Playwright second

The default path is a lightweight `httpx` GET. Only when HTTP is insufficient
(JS-required, blocked, or incomplete) does the engine escalate to Playwright.
This keeps the majority of the run cheap and only spins up a browser where it
actually helps.

---

## 6. Browser lifecycle & memory

- One shared Playwright browser (via `BrowserManager`) reused across many
  pages — not a fresh process per task.
- Recycled after a configurable number of queries (`browser_restart_after_queries`),
  on memory pressure, or explicitly.
- Every network operation has independent connect/read/navigation timeouts;
  no task can hang indefinitely. Futures are shutdown with `wait=False` to
  avoid executor deadlocks.
- Data is streamed — only bounded queues and current-page content are in
  memory; full page history is never retained.

---

## 7. Concurrency limits (12 GB RAM defaults)

| Pool | Default | Hard max |
|------|---------|----------|
| Google Maps workers | 2 | 4 |
| Website HTTP workers | 4 | 8 |
| Playwright workers | 2 | 4 |
| SMTP workers | 3 | 8 |

The config validator enforces these maxima with a clear error naming the bad
value and the recommendation, so an operator can't accidentally set an unsafe
number.

---

## 8. Proxies (future-proof, disabled now)

`ProxyManager` is a clean abstraction over HTTP(S), Playwright, pool, and
rotation. It is **off by default**. Adding proxies later requires only `.env`
entries and a config flag — zero core changes.

---

## 9. Tech detection

Primary: **`wappalyzer-python3`** — the actively-maintained fork of the
archived `python-Wappalyzer`, publishing 1,400+ Wappalyzer fingerprints and
detecting technologies directly from the HTML string + headers (no browser
needed, which keeps the HTTP-first path cheap). Evaluated against the
alternatives at implementation time: `s0md3v/wappalyzer` (runs the extension
inside Chromium via Playwright — conflicts with the no-per-page-browser
principle) and `py-wappalyzer` (1-star, HAR-centric), and `wappalyzer-python3`
was the clear fit.

Fallback: a built-in regex signature table covering the most common platforms
(WordPress, Shopify, React, GA, GTM, Cloudflare…). The engine works even if
`wappalyzer-python3` is absent, per the "never depend on one detector" rule.

---

## 10. Extensibility via YAML

Two subsystems are user-extensible without editing Python:
- **Custom signals** (`signals:` — keywords/regex/tags with ANY/ALL logic).
- **Filters** (`filters:` — include/exclude × all/any condition groups).

Documented in full in `GUIDE.md`.

---

## Legacy reference analysis

The supplied legacy scraper (`High-Ticket B2B & Luxury Services.py`) was
analysed and its **field-tested patterns** ported into this engine:

- **Maps selectors**: result cards `a.hfpxzc`, name `h1.DUwDvf`, category
  `button.DkEaL`, address `button[data-item-id="address"]`, phone
  `button[data-item-id^="phone:tel:"]`, website `a[data-item-id="authority"]`,
  claimed via `merchant_claim_business`, plus the feed `div[role="feed"]`.
  All re-verified live against Google Maps on 2026-08-28; business hours are
  read from the hours table (`table.eK4R0e` rows carrying a
  `Copy open hours` aria-label) and open/closed status from `span.ZDu9vd`,
  since the older `div[class*="hours"]` selector no longer matches.
- **Rating/reviews regex** (`([1-5]\.\d)`, `\(([\d,]+)\)`, `(\d+) reviews`)
  used as a semantic fallback alongside the grandparent-header trick.
- **Address decomposition** into city/state/zip via `\b\d{5}\b`,
  `\b([A-Z]{2})\b\s*\d{5}`, `,\s*([^,]+?),\s*[A-Z]{2}\s*\d{5}`.
- **Bot/cooldown detection** markers and configurable cooldown-then-skip.
- **Email filtering**: dummy/disposable domains, public-provider whitelist,
  suspicious words, and a domain-relationship check (with personal-provider
  exemption).

Its weaknesses were explicitly **not** ported: the false-"dead" classification
on any request exception (including timeouts), the verboten "Lead Score"
column, the fragile CSV-based dedup, query-only JSON checkpoints, bare
`except: pass`, hardcoded city/niche lists, and `verify=False` SSL bypass.

## Reliability guarantees (post-audit)

A third-party audit (Claude + ChatGPT bug reports) surfaced several
seam-level defects. All were fixed:

- **Zero-listings fail-closed**: a non-empty Maps search returning 0 links now
  raises `ZeroListingsError`; the pipeline marks the query `failed` (never
  `done`) so it is retried on the next run, instead of silently succeeding.
- **Checkpoint resume correctness**: dedup seen-sets are seeded from
  `committed` records only, so a crash mid-enrichment no longer makes an
  unfinished record look like a duplicate and vanish forever.
- **Crawler `len(int)` TypeError** fixed.
- **Filter ordering**: conditions split into a pre-enrichment pass (Maps
  fields) and a post-enrichment pass (ga4/gtm/emails/signals), so signal-based
  filters actually work.
- **`ga4`/`gtm` added to `OUTPUT_COLUMNS`** (they were computed then silently
  dropped at export).
- Randomized delays, HTTP/SMTP retry wiring, browser recycling (`mark_query`),
  Playwright missing-binary error handling, MX priority sorting, duplicate-email
  quality check, signal-evidence retention, and correct checkpoint backup
  rotation.

## Assumptions made

These were engineering choices where the spec left room for judgment:

- **SQLite** for checkpoint (over JSON) for transactional safety.
- **First-valid-record-wins** dedup, with a domain+city guard for chain nuance.
- CSV written **record-by-record** with `fsync` (accepting a minor throughput
  cost) to guarantee no lost or partial rows after a crash.
- Queries run sequentially; enrichment parallelized only *within* a query.
- Wappalyzer (`wappalyzer-python3`) is a declared dependency, but the engine
  degrades to a regex fallback if it's ever missing or fails.
- OCR (`pytesseract`) is optional and off by default; not a hard dependency.
