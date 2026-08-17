#!/usr/bin/env bash
# One-time setup: virtualenv + data-layer dependencies. No API keys required.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "Creating virtualenv..."
python3 -m venv .venv
echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

mkdir -p results memory data_cache
[ -f .env ] || cp .env.example .env

echo
echo "Verifying the data layer against live sources..."
./bin/ta snapshot SPY | head -8
echo
echo "Ready. Open this directory in Claude Code and run:  /analyze NVDA"
