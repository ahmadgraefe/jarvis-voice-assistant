#!/bin/bash
# Jarvis — Launch Session (macOS)
# Ported from launch-session.ps1 (Windows) for double-clap trigger + manual use.
#
# Seit der Hetzner-Server-Migration (2026-08-09): server.py laeuft nicht mehr
# lokal auf dem Mac, sondern 24/7 auf dem Hetzner-Server, nur ueber Tailscale
# erreichbar (siehe UEBERGABE.md). Dieses Skript startet darum KEINEN lokalen
# Server mehr, sondern oeffnet Chrome direkt gegen die Tailscale-Adresse.
# Voraussetzung: Tailscale laeuft auf diesem Mac (Menüleisten-App).
#
# Music was deliberately removed (2026-08-05): the "activate" needed to start
# Apple Music catalog playback kept fighting WhatsApp's own focus-stealing for
# frontmost status, stealing focus from Chrome regardless of how carefully the
# window itself was hidden/minimized — not worth it for a 12-second sting.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$DIR/config.json"

# Echtes HTTPS ueber Tailscale (tailscale serve, siehe UEBERGABE.md) statt
# der nackten http://IP:8340 — Chrome/Safari behandeln eine rohe IP+http
# als unsicheren Origin und blockieren dann Mikrofon/Kamera/Benachrich-
# tigungen automatisch, WSS ueber den echten Tailscale-Hostnamen umgeht das.
JARVIS_HOST="jarvis-brain.tailef7101.ts.net"

get_json() {
  python3 -c "import json,sys;print(json.load(open('$CONFIG')).get('$1', ''))"
}

WORKSPACE_PATH=$(get_json workspace_path)

open_config_apps() {
  python3 -c "import json;print(chr(10).join(json.load(open('$CONFIG')).get('apps', [])))" | while IFS= read -r app; do
    [ -z "$app" ] && continue
    if [[ "$app" == *"://"* ]]; then
      open "$app"
    else
      open -a "$app" 2>/dev/null || echo "[jarvis] Konnte App nicht oeffnen: $app"
    fi
  done
}

# Nothing here depends on anything else, so run it all at once.
open_config_apps &

# Kurzer Check ob der Hetzner-Server (ueber Tailscale) gerade erreichbar ist —
# reiner Hinweis, kein Warten/Blockieren mehr noetig, der Server laeuft
# ohnehin 24/7 unabhaengig von diesem Skript.
if ! curl -s -o /dev/null -m 3 "https://$JARVIS_HOST/"; then
  echo "[jarvis] WARNUNG: Hetzner-Server ($JARVIS_HOST) antwortet gerade nicht. Tailscale-Verbindung pruefen."
fi

EXTRA_TABS=()
while IFS= read -r line; do
  [ -n "$line" ] && EXTRA_TABS+=("$line")
done < <(python3 -c "import json;[print(u) for u in json.load(open('$CONFIG')).get('extra_tabs', [])]")

EXTRA_TABS_APPLESCRIPT="{"
for i in "${!EXTRA_TABS[@]}"; do
  [ $i -gt 0 ] && EXTRA_TABS_APPLESCRIPT+=", "
  EXTRA_TABS_APPLESCRIPT+="\"${EXTRA_TABS[$i]}\""
done
EXTRA_TABS_APPLESCRIPT+="}"

# Reuse an existing Jarvis window if one from an earlier double-clap is still
# open (repeated claps in a day shouldn't pile up new windows), otherwise open
# a dedicated NEW Chrome window (same Chrome app/profile as your regular
# browsing — just not mixed into your existing work window) with Jarvis as the
# focused tab 1, plus extra_tabs from config.
# Note: because this reuses your already-running Chrome, the --autoplay-policy
# flag can't be applied (Chrome only honors launch flags on a fresh process) —
# the first response after each launch needs one click before Jarvis can play audio.
osascript -e "
tell application \"Google Chrome\"
    activate
    set foundWindow to missing value
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains \"$JARVIS_HOST\" then
                set foundWindow to w
                exit repeat
            end if
        end repeat
        if foundWindow is not missing value then exit repeat
    end repeat
    if foundWindow is missing value then
        set jarvisWindow to make new window
        tell jarvisWindow
            set URL of active tab to \"https://$JARVIS_HOST\"
            repeat with tabURL in $EXTRA_TABS_APPLESCRIPT
                make new tab at end of tabs with properties {URL:tabURL}
            end repeat
            set active tab index of jarvisWindow to 1
        end tell
    else
        set index of foundWindow to 1
    end if
end tell"
