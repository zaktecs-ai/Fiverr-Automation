# B2B Lead Scraper Engine

A production-grade, modular, **resumable** lead-generation scraper built in
Python. It collects Google Maps business data, enriches each business's
website (emails, social links, tech stack, signals), optionally verifies
email deliverability, and exports clean CSV + XLSX — all while surviving
crashes, reboots, and long multi-week runs without restarting from zero.

> Designed for long-running operation on a **12 GB RAM / 200 GB disk Linux
> VPS** (e.g. Oracle Cloud). Historical runs have reached ~200,000 records
> over ~2 weeks.

---

## What it does

1. **Google Maps collection** — streams listings per query, opening each
   business to capture name, category, phone, website, hours, address/geo,
   rating/reviews, claimed status, description, plus code, place ID, and more.
2. **Normalization + deduplication** — canonical URLs, country-aware phones,
   and a multi-signal identity resolver that removes duplicates *without*
   wrongly collapsing multi-location chains.
3. **Filtering** — keep/reject businesses early (AND/OR/NOT conditions) so the
   engine never wastes VPS resources enriching records you don't want.
4. **Website enrichment** — HTTP-first fetching with a smart priority crawler
   + sitemap, escalating to Playwright only when JS is required or HTTP is
   blocked.
5. **Email + signals + tech stack** — extracts/cleans emails, detects social
   profiles, technologies, and configurable business signals.
6. **Optional MX / SMTP verification** — both off by default, both safe.
7. **Atomic export + checkpoint** — every record is committed to CSV and a
   SQLite checkpoint transactionally; a crash resumes at the exact right place.
8. **Quality gate + summary** — automated data-quality report and
   `run_summary.json`.

---

## VPS requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM      | 4 GB    | 8–12 GB     |
| Disk     | 20 GB   | 100–200 GB  |
| OS       | Linux (Ubuntu/Debian preferred) | |
| Python   | 3.9+    | 3.10–3.12   |

---

## Installation

```bash
git clone <your-repo-url> && cd Fiverr-Automation
./setup.sh
```

`setup.sh` installs system packages, creates a `.venv`, installs Python
dependencies, and downloads the Chromium browser for Playwright.

---

## Configuration

Edit **`config.yaml`** (one human-readable file). Secrets go in `.env`
(copy `.env.example`). See **`GUIDE.md`** for a plain-English walkthrough of
every setting.

Set your job name and queries:

```yaml
job:
  client_name: my_campaign
  output_filename: my_campaign

queries:
  - "dentists in Dallas, TX"
  - "dentists in Houston, TX"
```

---

## Running a job

```bash
source .venv/bin/activate
python main.py
```

The engine:
1. **validates** your config (and aborts with a clear message if anything is
   wrong),
2. **detects any existing checkpoint** and resumes if present,
3. streams **plain-English progress** to the terminal (which search is running,
   each business found, and how many were exported) plus full detail in
   `scraper.log`.

To run it in the **background** (survive SSH disconnects), use `tmux` — see
**`docs/RUN_WITH_TMUX.md`** for a plain-English guide.

---

## Resuming a job

Just run `python main.py` again. The engine reads the SQLite checkpoint in the
job's `output/` folder and:
- skips already-completed queries,
- never re-discovers or duplicate-commits existing records,
- resumes unfinished work (including the exact record that was mid-process).

It is safe against `tmux` drops, VPS reboots, browser crashes, network
outages, and `Ctrl-C`.

---

## Visible screen + manual CAPTCHA (TightVNC)

By default the browser runs invisibly (`maps.headless: true`). To **watch it
live** and **solve CAPTCHAs by hand**, switch on a separate virtual screen:

1. `./vnc-screen.sh` — starts a dedicated screen (display `:2`, non-common port
   `43873`) that never touches your existing `:1` screen.
2. Set `maps.headless: false` in `config.yaml`.
3. `python main.py` — the browser appears on that screen.
4. Connect TightVNC to `YOUR-VPS-IP:43873` and click the CAPTCHA when it shows.

See **`docs/VNC_SETUP.md`** for the full plain-English walkthrough.

---

## Output files

Everything lands in `output/<client_name>/`:

| File | Purpose |
|------|---------|
| `<client_name>.csv` | **Primary source of truth** — append-safe records |
| `<client_name>.xlsx` | Convenience spreadsheet (built at the end) |
| `filtered_records.csv` | Records rejected by filters (with reason) |
| `failed_records.csv` | Records that failed validation |
| `run_summary.json` | Job statistics |
| `quality_report.json` | Final data-quality gate results |
| `scraper.log` | Persistent diagnostics |
| `checkpoint.db` / `.json` / backup | Resumable state |

---

## Testing

```bash
source .venv/bin/activate
pip install -r requirements.txt   # if not already done
python -m pytest tests/ -q
```

Core logic (normalization, dedup, filters, signals, checkpoint recovery,
config validation, CSV integrity, status classification) is tested with
mocks — no live Google Maps needed.

---

## Project layout

```
.
├── main.py                 # entrypoint
├── config.yaml             # the ONE config file you edit
├── .env.example            # secrets template (copy to .env)
├── requirements.txt
├── setup.sh
├── vnc-screen.sh           # visible Scraper Engine screen (TightVNC)
├── README.md
├── GUIDE.md                # plain-English config guide
├── docs/architecture.md    # design decisions
├── docs/VNC_SETUP.md       # visible-screen + CAPTCHA walkthrough
├── docs/RUN_WITH_TMUX.md   # background running + reading progress
├── scraper/
│   ├── config.py           # config load + validation
│   ├── models.py           # schema + status model
│   ├── pipeline.py         # orchestration
│   ├── maps/               # Google Maps collector
│   ├── websites/           # fetcher, crawler, enricher, tech detect
│   ├── email/              # extraction + MX/SMTP
│   ├── signals/            # signal detection engine
│   ├── filters/            # filter engine
│   ├── dedup/              # identity resolution
│   ├── checkpoint/         # SQLite resumable state
│   ├── validation/         # record + quality gate
│   ├── export/             # CSV/XLSX/summary writers
│   ├── browser/            # browser lifecycle + proxy
│   └── utils/              # normalize, logging, retry, dns cache
├── tests/                  # pytest suite (mocked)
└── output/                 # runtime outputs (git-ignored)
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Playwright is not installed` | Run `./setup.sh` (or `python -m playwright install chromium`) |
| Config error message | Reads the message — it names the exact key, bad value, and safe range |
| Job seems stuck | Check `scraper.log`; every stage has a timeout, nothing hangs forever |
| Duplicate records after restart | Should not happen — ensure you're using the same `output/` dir |
| Memory creeping up | Lower `concurrency.*`, keep `browser_restart_after_queries` small |
| `found 0 listing place URLs` on an EU VPS | The GDPR consent wall hid the feed — the collector now auto-dismisses it; `git pull` and retry |

---

## Limitations & assumptions

- Google Maps and external sites change their markup; the collector uses
  layered (primary → alternate → fallback) selectors and returns `N/A`
  rather than guessing.
- CAPTCHA / anti-bot challenges are **classified and preserved**, never
  bypassed, and never mistaken for a dead website.
- Wappalyzer tech detection uses `wappalyzer-python3` (maintained fork, 1,400+
  fingerprints), with a built-in regex fallback if it's ever unavailable.
- No paid APIs or third-party verification services required.
