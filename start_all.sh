#!/usr/bin/env bash
# start_all.sh - Launch tradeBotTiuku UI and Paper Trader Daemon on Linux

set -e

# Detect python executable
if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python &>/dev/null; then
    PYTHON_BIN="python"
else
    echo "❌ Virhe: Pythonia ei löytynyt polusta."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec "$PYTHON_BIN" start_all.py "$@"
