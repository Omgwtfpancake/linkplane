#!/data/data/com.termux/files/usr/bin/bash

CACHE="$HOME/.battery-cache.json"
TMP="$HOME/.battery-cache.tmp"

if termux-battery-status > "$TMP" 2>/dev/null; then
    mv "$TMP" "$CACHE"
else
    rm -f "$TMP"
fi
