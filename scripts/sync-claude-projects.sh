#!/bin/bash
# Spiegelt Ahmads lokale Claude-Code-Sitzungsverlaeufe (~/.claude/projects)
# regelmaessig zum Hetzner-Server, damit claude_code_context.py dort auf eine
# eigene, lokale Kopie zugreifen kann statt live per Mac-Actuator-Proxy
# nachzufragen (2026-08-10, Ahmads ausdruecklicher Wunsch: Server soll bei
# "Mac aus" nicht mehr komplett ausfallen, sondern die letzte bekannte Kopie
# mit Alters-Hinweis liefern). Ein-Schuss-Skript, kein Dauerprozess — laeuft
# per launchd StartInterval, bewusst OHNE KeepAlive (siehe com.jarvis.brain-
# Vorfall in UEBERGABE.md: KeepAlive + interne Prozess-Verwaltung hat sich
# dort selbst hochgeschaukelt).

SRC_DIR="$HOME/.claude/projects"
DEST_HOST="root@jarvis-brain.tailef7101.ts.net"
DEST_DIR="/opt/jarvis/claude_projects_sync"

if [ ! -d "$SRC_DIR" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Kein ~/.claude/projects gefunden, ueberspringe."
    exit 0
fi

# Zeitstempel VOR dem rsync schreiben (wird mit uebertragen) — der Server
# liest daraus, wie alt seine Kopie ist, statt sie stillschweigend als aktuell
# auszugeben.
date +%s > "$SRC_DIR/_last_sync_epoch.txt"

rsync -az --delete \
    -e "ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
    "$SRC_DIR/" "$DEST_HOST:$DEST_DIR/"

if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Sync erfolgreich."
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') Sync fehlgeschlagen (Server nicht erreichbar?)."
fi
