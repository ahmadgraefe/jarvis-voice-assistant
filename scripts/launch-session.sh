#!/bin/bash
# Jarvis — Launch Session (macOS)
# Ported from launch-session.ps1 (Windows) for double-clap trigger + manual use.
#
# Server (hidden) + Chrome with Jarvis. Server/other-apps run in PARALLEL, and
# Chrome opens as soon as the server actually responds (polled, not a blind
# sleep) — the whole thing should feel close to instant after a double-clap.
#
# Music was deliberately removed (2026-08-05): the "activate" needed to start
# Apple Music catalog playback kept fighting WhatsApp's own focus-stealing for
# frontmost status, stealing focus from Chrome regardless of how carefully the
# window itself was hidden/minimized — not worth it for a 12-second sting.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$DIR/config.json"

get_json() {
  python3 -c "import json,sys;print(json.load(open('$CONFIG')).get('$1', ''))"
}

WORKSPACE_PATH=$(get_json workspace_path)

start_server() {
  LOG_FILE="$HOME/Library/Logs/jarvis-server.log"
  # server.py is meant to stay running continuously, not get relaunched on
  # every double-clap — without this check, a clap while it's already up
  # (confirmed to happen: two claps close together, or clapping again before
  # noticing Jarvis already opened) spawns a SECOND process that can't bind
  # port 8340, silently loses the race, and keeps running code from before
  # any fix made since the first one started. If it's already answering,
  # leave it alone — only Chrome needs to (re)open below.
  if curl -s -o /dev/null -m 1 "http://localhost:8340/"; then
    return
  fi
  cd "$WORKSPACE_PATH" && source venv/bin/activate && nohup python server.py > "$LOG_FILE" 2>&1
}

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
start_server &
open_config_apps &

# Poll for the server actually being up instead of guessing with a fixed sleep
# — opens Chrome the moment it's ready, and never opens it too early either.
for i in $(seq 1 50); do
  if curl -s -o /dev/null -m 1 "http://localhost:8340/"; then
    break
  fi
  sleep 0.1
done

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
            if URL of t contains \"localhost:8340\" then
                set foundWindow to w
                exit repeat
            end if
        end repeat
        if foundWindow is not missing value then exit repeat
    end repeat
    if foundWindow is missing value then
        set jarvisWindow to make new window
        tell jarvisWindow
            set URL of active tab to \"http://localhost:8340\"
            repeat with tabURL in $EXTRA_TABS_APPLESCRIPT
                make new tab at end of tabs with properties {URL:tabURL}
            end repeat
            set active tab index of jarvisWindow to 1
        end tell
    else
        set index of foundWindow to 1
    end if
end tell"
