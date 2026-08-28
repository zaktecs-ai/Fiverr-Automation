# Running the scraper in the background (tmux) + reading progress

Plain-English guide. No programming knowledge needed.

---

## 1. Why tmux?

Your VPS only runs the scraper **while your terminal (SSH) window is open**.
If your internet drops, or you close the window, the scraper stops.

**tmux** is a small tool that keeps a session alive on the server even after
you close your window. You start the scraper inside tmux, detach, and it keeps
running in the background. Come back later and re-attach to see progress.

---

## 2. One-time setup (install tmux)

```bash
sudo apt-get update && sudo apt-get install -y tmux
```

---

## 3. Start the scraper inside tmux

```bash
tmux new -s scraper
```

This opens a fresh tmux session named `scraper`. Inside it:

```bash
cd Fiverr-Automation
source .venv/bin/activate
python main.py
```

You will now see clean progress like:

```
==========================================================
  B2B LEAD SCRAPER  —  luxury_pool_campaign
  Total searches : 3
  Already done   : 0
  To do now      : 3
==========================================================
[1/3] RUNNING  dentists in Dallas, TX
    + found #1: Smile Dental
    + found #2: Bright Smiles
    ...
[1/3] DONE     dentists in Dallas, TX  →  discovered 45, exported 38
[2/3] RUNNING  dentists in Houston, TX
    ...
```

Each line is plain English:
- `RUNNING` → which search is being done right now.
- `+ found #N: <name>` → a business was found, in order.
- `DONE ... discovered X, exported Y` → that search finished and how many saved.
- `[1/3]` → search number 1 of 3.

---

## 4. Detach (leave it running in the background)

While the scraper is running, press:

```
Ctrl + B, then D
```

(press `Ctrl`+`B` together, let go, then press `D`)

You are now back at your normal terminal. The scraper keeps running inside tmux.
You can even close your SSH window entirely.

---

## 5. Re-attach later (see progress again)

```bash
tmux attach -t scraper
```

You will see the same live progress again.

---

## 6. Stop the scraper (Ctrl+C)

Inside the tmux session, press `Ctrl + C`. The scraper saves its checkpoint
automatically, so you can restart it later and it resumes from where it stopped.

---

## 7. Useful tmux commands (cheat sheet)

| Command | What it does |
|---------|--------------|
| `tmux new -s scraper` | Start a session named "scraper" |
| `Ctrl+B` then `D` | Detach (leave running) |
| `tmux attach -t scraper` | Re-attach to see progress |
| `tmux ls` | List running sessions |
| `tmux kill-session -t scraper` | Fully stop a session |

---

## 8. Where the full detail lives

The clean lines above are just the summary. Every tiny detail (with timestamps)
is also written to:

```
output/<client_name>/scraper.log
```

Check that file if you ever need to diagnose something in depth.
