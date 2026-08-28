#!/usr/bin/env bash
# ============================================================================
#  B2B Lead Scraper Engine — one-command VPS setup.
#
#  Usage (on a fresh Linux VPS):
#      git clone <repo-url> && cd <repo-dir>
#      ./setup.sh
#      python main.py
#
#  What it does:
#    1. Installs system packages (python3, venv, build tools, chromium deps).
#    2. Creates a Python virtual environment.
#    3. Installs Python dependencies from requirements.txt.
#    4. Installs Playwright and its Chromium browser.
#    5. Creates the output/ directory.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 1. Detect OS ----------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG_MANAGER="yum"
else
    echo "Unsupported OS. Install Python 3.9+, pip, venv, and Chromium deps manually."
    exit 1
fi

echo "==> [1/5] Installing system dependencies..."
if [ "$PKG_MANAGER" = "apt" ]; then
    # `sudo` may not exist in minimal containers; tolerate its absence.
    SUDO=""
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
    $SUDO apt-get update -y
    $SUDO apt-get install -y python3 python3-venv python3-pip \
        build-essential libssl-dev libffi-dev \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libxcomposite1 libxdamage1 \
        libxrandr2 libgbm1 libasound2 libxkbcommon0 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 libx11-6 libx11-xcb1 \
        fonts-liberation libfreetype6
elif [ "$PKG_MANAGER" = "dnf" ]; then
    SUDO=""
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
    $SUDO dnf install -y python3 python3-pip gcc libffi-devel openssl-devel \
        nss nspr atk at-spi2-atk cups-libs libdrm libxcb libXcomposite \
        libXdamage libXrandr mesa-libgbm alsa-lib pango cairo
else
    echo "Detected yum; installing common deps (may need adjustment)..."
    SUDO=""
    if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
    $SUDO yum install -y python3 python3-pip gcc libffi-devel openssl-devel \
        nss nspr atk at-spi2-atk cups-libs libdrm libxcb libXcomposite \
        libXdamage libXrandr mesa-libgbm alsa-lib pango cairo
fi

# --- 2. Python venv ---------------------------------------------------------
echo "==> [2/5] Creating Python virtual environment..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# --- 3. Python deps ---------------------------------------------------------
echo "==> [3/5] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# --- 4. Playwright browser --------------------------------------------------
echo "==> [4/5] Installing Playwright and Chromium..."
python -m playwright install --with-deps chromium

# --- 5. Output dirs ---------------------------------------------------------
echo "==> [5/5] Preparing output directory..."
mkdir -p output
# `config.yaml` ships in the repo; only copy from a template if one exists.
[ -f config.yaml ] || { [ -f config.yaml.example ] && cp config.yaml.example config.yaml; }

echo ""
echo "=============================================================="
echo " Setup complete."
echo " Next steps:"
echo "   1. cp .env.example .env        # if you have any secrets"
echo "   2. edit config.yaml            # set client_name + queries"
echo "   3. source .venv/bin/activate"
echo "   4. python main.py"
echo "=============================================================="
