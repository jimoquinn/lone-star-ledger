#!/usr/bin/env bash
# Supervised Hays pull through your own Chrome. Start Chrome once with:
#   google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/google-chrome-lsl"
# open the Hays election page, clear any Cloudflare check, then run this.
set -euo pipefail
cd "$(dirname "$0")"
~/lsl-venv/bin/python collector/hays_fetch.py "$@"
python3 collector/collect.py --only hays
