#!/bin/sh
# Runs as root just long enough to make /config writable, then drops to PUID:PGID
# (Unraid's nobody:users by default). Docker creates bind-mounted appdata folders
# as root, so without this the app could not write its database or tokens.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$0" "$@"
fi

exec python -m family_assistant "$@"
