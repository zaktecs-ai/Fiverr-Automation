# GUIDE — Plain-English Configuration Guide

This guide is for **non-programmers**. It explains every part of
`config.yaml` in simple words, with examples. You do not need to know Python.

> **The golden rule:** you only edit ONE file — `config.yaml`. The engine
> checks it before running and, if anything is wrong, tells you in plain
> English exactly what to fix.

---

## 1. The basics

`config.yaml` is a text file made of **sections**, each with a name and some
settings. A setting looks like:

```yaml
name: value
```

Comments (lines starting with `#`) are notes for you — the engine ignores them.

---

## 2. `job` — naming your run and setting limits

```yaml
job:
  client_name: luxury_pool_campaign
  output_filename: luxury_pool_campaign
  max_results_per_query: 0
  max_total_results: 0
```

| Setting | What it means |
|---------|---------------|
| `client_name` | The name of this campaign. It becomes the folder name under `output/`. |
| `output_filename` | The base name of your CSV/XLSX files. Usually the same as `client_name`. |
| `max_results_per_query` | How many results to collect **per search query**. `0` = no limit. |
| `max_total_results` | How many results to collect **for the whole job**. `0` = no limit. |

**Example:** 3 queries × 500 per query = up to 1,500 total. If you also set
`max_total_results: 1000`, the job stops at 1,000 total.

---

## 3. `queries` — what to search for

```yaml
queries:
  - "dentists in Dallas, TX"
  - "dentists in Houston, TX"
```

Type search terms **exactly as you would in Google Maps**. Each `- "..."` is
one search. They run one after another, and results combine into one output.

---

## 4. `maps` — how Google Maps is collected

```yaml
maps:
  include_permanently_closed: false
  headless: true
  hl: en     # interface language (en = English)
  gl: us     # region (us = United States)
  browser_restart_after_queries: 5
```

| Setting | What it means | Recommended |
|---------|---------------|-------------|
| `include_permanently_closed` | `false` = skip closed businesses. `true` = keep them. | `false` |
| `headless` | `true` = browser runs invisibly (correct for a VPS). | `true` |
| `hl` / `gl` | Force Google Maps language (`hl: en`) and region (`gl: us`). This keeps results + labels in English regardless of where your VPS is (Germany, UK, etc.). | `en` / `us` |
| `browser_restart_after_queries` | Restart the browser after this many queries to keep memory healthy. `0` = never. | `5` |

---

## 5. `website` — visiting and learning from websites

```yaml
website:
  require_website: true
  enable_playwright_fallback: true
  enable_sitemap: true
  max_pages_per_site: 8
  overall_site_timeout_seconds: 120
```

| Setting | What it means |
|---------|---------------|
| `require_website` | `true` = only keep businesses that have a website (skips the rest, saves lots of time). |
| `enable_playwright_fallback` | `true` = if a normal request can't read a site, use a real browser. Keep `true`. |
| `enable_sitemap` | `true` = use the site's sitemap to find the most useful pages. |
| `max_pages_per_site` | Most pages to visit per website. Higher = more data but slower. |
| `overall_site_timeout_seconds` | Hard time limit (seconds) for any one website, so one slow site can't stall the whole job. |

---

## 6. `email` — collecting and (optionally) verifying emails

```yaml
email:
  enabled: true
  max_email_length: 120
  enable_mx_check: false
```

| Setting | What it means |
|---------|---------------|
| `enabled` | `true` = collect emails from websites. |
| `max_email_length` | Longest email to trust (characters). Longer ones are usually fake/junk. |
| `enable_mx_check` | `true` = also check the domain's mail server exists (DNS). Adds a small delay. |

---

## 7. `smtp` — verifying emails actually work (optional)

```yaml
smtp:
  enabled: false
  workers: 3
```

| Setting | What it means |
|---------|---------------|
| `enabled` | `false` = skip SMTP checks (emails show "Not Checked"). `true` = verify each email really accepts mail. **Slower** and can be rate-limited. |
| `workers` | How many checks run at once. Keep it low (`3`). |

---

## 8. `concurrency` — how much runs at once

This is about **not exhausting your VPS's 12 GB RAM**.

```yaml
concurrency:
  website_workers: 4
  playwright_workers: 2
```

| Setting | What it means | Recommended |
|---------|---------------|-------------|
| `website_workers` | Website requests at once. | `4` |
| `playwright_workers` | Browser-based website fetches at once (RAM-hungry). | `2` |

**Lower = safer.** If you have more RAM you can raise these, but the engine
will refuse clearly-dangerous values.

---

## 9. `delays` — being polite to websites

```yaml
delays:
  maps_min_seconds: 2
  maps_max_seconds: 5
  cooldown_seconds: 60
```

| Setting | What it means |
|---------|---------------|
| `maps_min_seconds` / `maps_max_seconds` | Random pause between Google Maps actions (the engine picks a random number in this range). |
| `cooldown_seconds` | Extra pause when the engine detects a block/challenge. |

> Random delays reduce the chance a site flags you, but they are **not** a
> guarantee against detection.

---

## 10. `signals` — detecting things on websites

The engine always looks for built-in signals (Meta Pixel, GA4, Google Tag
Manager, booking systems, chat widgets, pricing, financing, "licensed &
insured", "established in", portfolio, mobile service, membership). Each shows
up as a `YES`/`NO` column.

You can add your **own** signals in `config.yaml` without touching code:

```yaml
signals:
  family_owned:
    enabled: true
    keywords:
      - "family owned"
      - "family-run"
    regex:
      - "since\\s+(19|20)\\d{2}"
    match_logic: ANY
```

| Part | Meaning |
|------|---------|
| `enabled` | `true` = this signal is checked. |
| `keywords` | Words/phrases to look for (plain text). |
| `regex` | Advanced text patterns (leave empty if unsure). |
| `match_logic` | `ANY` = any keyword/regex match counts. `ALL` = every rule group must match. |

The result appears as a column named `signal_family_owned` = `YES`/`NO`.

---

## 11. `filters` — keeping only the leads you want

Filters run **early**, before the slow website work, so you don't waste time
on businesses you'll throw away.

```yaml
filters:
  include_all:
    - field: website
      op: "="
      value: "yes"
    - field: review_count
      op: ">="
      value: 15
    - field: rating
      op: ">="
      value: 4.0
```

| Group | Meaning |
|-------|---------|
| `include_all` | Every condition must be true (AND). |
| `include_any` | At least one condition must be true (OR). |
| `exclude_all` | Reject if every condition is true. |
| `exclude_any` | Reject if any condition is true. |

**Useful fields:** `website` (yes/no), `review_count`, `rating`,
`email_found` (yes/no), `meta_pixel` (yes/no), `ga4` (yes/no), `gtm` (yes/no).

**Operators:** `=` (equals), `!=` (not equals), `>`, `<`, `>=`, `<=`,
`contains`.

Rejected records are saved in `filtered_records.csv` with a reason.

---

## 12. Output naming

With `client_name: luxury_pool_campaign`, output is:

```
output/luxury_pool_campaign/
    luxury_pool_campaign.csv
    luxury_pool_campaign.xlsx
    filtered_records.csv
    failed_records.csv
    run_summary.json
    quality_report.json
    scraper.log
    checkpoint.db        (+ .json and backup)
```

---

## 13. Secrets (`.env`)

Anything sensitive (API keys, proxy credentials) goes in `.env`, **not** in
`config.yaml`. Copy `.env.example` to `.env` and fill in real values. `.env`
is never committed to Git.

```bash
cp .env.example .env
```

---

## 14. Checkpointing & resuming (the important part)

The engine saves its progress continuously. If it stops for **any** reason —
crash, reboot, `Ctrl-C`, power loss — just run `python main.py` again and it
**continues where it left off**. It will not restart from zero and will not
create duplicate rows.

You do not need to do anything special to get this; it's always on.
