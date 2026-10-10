#!/bin/bash
# Double-click this file (macOS) to start Tradio and open it in your browser.
# Same as running: python3 tradio/serve.py
cd "$(dirname "$0")" || exit 1
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
exec python3 tradio/serve.py "$@"
