#!/bin/bash
# Starts the double-clap listener inside Terminal.app so it inherits
# Terminal's already-granted Microphone TCC permission (launchd's own
# process identity cannot be granted mic access via System Settings).
#
# Blocks until the Terminal tab's command actually finishes (polls "busy")
# instead of returning the instant `do script` fires it off — see
# launch-background-brain.sh for why that matters once KeepAlive is on.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prevent duplicate listeners (e.g. after a manual `launchctl kickstart`) — two
# instances would both fire on the same clap and double-launch everything.
pkill -f "scripts/clap-trigger.py" 2>/dev/null

osascript <<EOF
tell application "Terminal"
    set clapTab to do script "cd '$DIR' && source venv/bin/activate && python scripts/clap-trigger.py; exit"
    repeat while (busy of clapTab)
        delay 5
    end repeat
end tell
EOF
