#!/usr/bin/env bash
# ============================================================================
#  Scraper Engine — visible virtual screen launcher (TightVNC).
#
#  What this does (plain English):
#    This starts a SECOND, separate screen on your VPS that the scraper's
#    browser will run inside. You then watch that screen through TightVNC and,
#    if Google shows a "prove you're human" CAPTCHA, you click it yourself while
#    the scraper waits.
#
#  It does NOT touch your existing VNC screen. It uses its OWN display (":2")
#  and its OWN port, so nothing clashes.
#
#  Usage:
#      ./vnc-screen.sh            # start the screen (display :2, port below)
#      ./vnc-screen.sh stop       # stop it
#      ./vnc-screen.sh status     # is it running?
#
#  Then set `maps.headless: false` in config.yaml and run `python main.py`.
#  Your browser will appear on this screen instead of running invisibly.
# ============================================================================
set -euo pipefail

# ---- Config (edit these two numbers if you must; keep them UNIQUE) ---------
DISPLAY_NUM="2"          # X display ":2" (existing screen is ":1")
VNC_PORT="43873"         # a non-common port, NOT 5901/5902/etc.
RESOLUTION="1366x900"

# Derived values (do not edit).
X11_PORT=$((6000 + DISPLAY_NUM))
DISPLAY_STR=":${DISPLAY_NUM}"
LOCK_DIR="/tmp/.X${DISPLAY_NUM}-lock"

log() { echo "[vnc-screen] $*"; }

case "${1:-start}" in
  stop)
    vncserver -kill "${DISPLAY_STR}" >/dev/null 2>&1 || true
    pkill -f "Xtightvnc ${DISPLAY_STR}" >/dev/null 2>&1 || true
    log "screen ${DISPLAY_STR} stopped."
    ;;
  status)
    if [ -f "${LOCK_DIR}" ]; then
      log "screen ${DISPLAY_STR} is RUNNING (VNC port ${VNC_PORT}, X11 ${X11_PORT})."
    else
      log "screen ${DISPLAY_STR} is NOT running."
    fi
    ;;
  *)
    # Ensure tightvncserver is installed.
    if ! command -v vncserver >/dev/null 2>&1; then
      echo "tightvncserver is not installed."
      echo "Install it with:  sudo apt-get update && sudo apt-get install -y tightvncserver"
      exit 1
    fi

    if [ -f "${LOCK_DIR}" ]; then
      log "screen ${DISPLAY_STR} is already running."
    else
      # Start TightVNC on the dedicated display + non-common port.
      # (No -localhost flag: we WANT it to listen on all interfaces so a remote
      #  TightVNC viewer can connect. `-localhost no` is invalid syntax.)
      vncserver "${DISPLAY_STR}" \
        -geometry "${RESOLUTION}" \
        -depth 24 \
        -rfbport "${VNC_PORT}" \
        >/dev/null 2>&1
      log "screen ${DISPLAY_STR} started."
    fi

    # Show how to connect.
    PUBLIC_IP="$(curl -4 -s ifconfig.me 2>/dev/null || echo "<your-VPS-IP>")"
    echo ""
    echo "Ready! Connect your TightVNC viewer to:"
    echo ""
    echo "    ${PUBLIC_IP}:${VNC_PORT}"
    echo ""
    echo "  * use the VNC password you set for display :2"
    echo "  * this is a SEPARATE screen from any existing ':1' screen"
    echo ""
    ;;
esac
