#!/bin/bash
# Double-click me to start the bot and open its dashboard.
# (macOS may ask once: right-click -> Open the first time.)
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First-time setup: creating Python environment..."
  python3 -m venv .venv || { echo "Python 3.11+ is required."; read -r; exit 1; }
  ./.venv/bin/pip install -e . || { echo "Install failed."; read -r; exit 1; }
fi
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env

source .venv/bin/activate
set -a; [ -f .env ] && source .env; set +a

( sleep 3; open "http://127.0.0.1:8790" ) &

while true; do
  phxbot run
  code=$?
  if [ $code -eq 42 ]; then
    echo ""
    echo "--- restarting (settings change or update from the dashboard) ---"
    echo ""
    continue
  fi
  echo ""
  echo "Bot stopped (exit $code). Press Enter to close this window."
  read -r
  exit $code
done
