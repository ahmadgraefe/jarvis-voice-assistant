#!/bin/bash
# Starts the always-on background brain inside Terminal.app so it inherits
# Terminal's already-granted Automation/Accessibility TCC permissions (needed
# for the AppleScript window positioning and WhatsApp keystroke send) —
# launchd's own process identity can't be granted those via System Settings,
# same reasoning as launch-clap-listener.sh.
#
# Blocks until the Terminal tab's command actually finishes (polls "busy")
# instead of returning the instant `do script` fires it off — launchd's
# KeepAlive restarts a job when ITS direct child exits, and that child here
# is THIS script, not the python process living inside Terminal. Without the
# wait, launchd would see this script exit almost immediately and relaunch it
# in a tight loop, stacking a new Terminal window every few seconds.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prevent duplicate instances (e.g. after a manual `launchctl kickstart`).
pkill -f "background_brain.py" 2>/dev/null

osascript <<EOF
tell application "Terminal"
    set jarvisTab to do script "cd '$DIR' && source venv/bin/activate && python background_brain.py; exit"
    repeat while (busy of jarvisTab)
        delay 5
    end repeat
end tell
EOF
