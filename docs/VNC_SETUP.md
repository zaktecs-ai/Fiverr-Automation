# Scraper Engine — Visible Screen Setup (TightVNC) & Manual CAPTCHA

Plain-English guide. No programming knowledge needed.

This walks you through giving the scraper a **visible screen** you can watch, and
how to **solve a CAPTCHA by hand** when Google asks "are you a human?".

---

## 1. What this is (and why)

Normally the scraper runs its browser **invisibly** (`headless: true`). That is
fine most of the time, but when Google detects a robot it shows a CAPTCHA that
the scraper cannot solve on its own — so it stops and marks that search as
"failed".

The fix is a **second, visible screen** on your VPS:

- The scraper's browser opens **on that screen** (you can see it).
- You watch it through **TightVNC**.
- When a CAPTCHA appears, **you click it yourself**. The scraper waits for you.

It is **completely separate** from any VNC screen already on the VPS, so it
never interferes with what is already set up.

---

## 2. Your existing VNC screen (for reference — we do NOT touch it)

| Setting | Value |
|---------|-------|
| Display | `:1` |
| VNC port | `43871` |
| X11 port | `6001` |

The new Scraper Engine screen uses different values so nothing clashes:

| Setting | Value |
|---------|-------|
| Display | `:2` (separate from `:1`) |
| VNC port | `43873` (a **non-common** port, not 5901/5902) |
| X11 port | `6002` (automatic) |

---

## 3. Setup (one time, ~5 minutes)

**Step A — install TightVNC (if you don't have it):**

```bash
sudo apt-get update
sudo apt-get install -y tightvncserver
```

**Step B — set a password for the new screen:**

```bash
vncserver :2 -geometry 1366x900 -depth 24 -rfbport 43873 -localhost no
```

The first time, it will ask you to set a password. This is the password you
will type in your TightVNC viewer. (You can ignore the "view-only password"
question — type **n**.)

It will also ask for a "desktop number". That is just the screen — pick `2`.

**Step C — stop it (we'll use the helper script from now on):**

```bash
vncserver -kill :2
```

From now on, use the included helper instead of typing all that yourself:

```bash
./vnc-screen.sh            # start the Scraper Engine screen
./vnc-screen.sh status     # check it is running
./vnc-screen.sh stop       # stop it
```

---

## 4. Make the scraper use the visible screen

Open `config.yaml` and change one line:

```yaml
maps:
  headless: false      # <-- change from true to false
```

Leave the `vnc:` section as-is (it already points at display `:2` and port
`43873`).

Now run the scraper normally:

```bash
python main.py
```

The browser will open **on display `:2`** instead of running invisibly.

---

## 5. Connect with TightVNC (watch the screen)

1. Install **TightVNC Viewer** on your own computer (Linux/Mac/Windows).
2. Find your VPS's public IP (or let the script tell you):
   ```bash
   curl -4 ifconfig.me
   ```
3. In TightVNC Viewer, type:
   ```
   YOUR-VPS-IP:43873
   ```
4. Enter the **password you set in Step B**.

You should now see the scraper's browser working live.

> **Note:** `43873` is a **non-common** port we picked on purpose. If you need
> to open it in your VPS firewall, allow that specific port only.

---

## 6. Solving a CAPTCHA by hand

When Google shows a "Prove you're human" / CAPTCHA page:

1. **Look at your TightVNC window** — the challenge is visible there.
2. **Click the checkbox / solve the puzzle yourself** in the window.
3. The scraper detects the page is no longer blocked and **continues on its
   own**. You don't need to do anything in the terminal.

**Tip:** If a CAPTCHA is solved, that search query will be re-run, because the
scraper records a blocked search as "failed" (not "done"), so it retries it
later instead of silently skipping it.

---

## 7. Stop the screen when you are done

```bash
./vnc-screen.sh stop
```

---

## 8. Troubleshooting

| Problem | Fix |
|---------|-----|
| "vncserver: command not found" | Run Step A (install tightvncserver). |
| Can't connect from your PC | Check firewall allows port `43873`; confirm `./vnc-screen.sh status` says RUNNING. |
| Wrong password | Re-run Step B and set the password again. |
| Screen already exists | Run `./vnc-screen.sh stop`, then start again. |
