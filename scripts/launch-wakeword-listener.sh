#!/bin/bash
# Startet den "Hey Jarvis"-Wake-Word-Listener innerhalb von Terminal.app,
# damit er Terminals bereits erteilte Mikrofon-TCC-Berechtigung erbt
# (launchds eigene Prozess-Identitaet kann ueber Systemeinstellungen keinen
# Mikrofon-Zugriff bekommen -- exakt dasselbe Muster wie beim bestehenden
# Klatsch-Trigger, siehe launch-clap-listener.sh).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Doppelte Listener verhindern (z.B. nach einem manuellen `launchctl kickstart`).
pkill -f "scripts/wakeword-trigger.py" 2>/dev/null

osascript <<EOF
tell application "Terminal"
    set wakewordTab to do script "cd '$DIR' && source venv/bin/activate && python scripts/wakeword-trigger.py; exit"
    repeat while (busy of wakewordTab)
        delay 5
    end repeat
end tell
EOF
