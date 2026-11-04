#!/bin/sh
# Start a virtual display for the HEB login browser, then run the app (or a CLI command).
set -e
if [ -n "$DISPLAY" ] && command -v Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
fi
exec python -m family_assistant "$@"
