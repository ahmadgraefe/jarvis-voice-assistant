"""
Jarvis V2 — Claude Code Context
Reads recent local Claude Code session transcripts so Jarvis can answer
questions about what you've been discussing/working on — ueber MEHRERE
gleichzeitig aktive Projekte hinweg (Roadmap Punkt 18, Multi-Repo-
Bewusstsein), nicht nur das zuletzt beruehrte.
"""

import glob
import json
import os
import re
import time

# Server (2026-08-10): kein Live-Proxy zum Mac mehr fuer diese Funktion.
# Stattdessen liest der Server eine per periodischem rsync vom Mac
# gespiegelte Kopie (siehe scripts/sync-claude-projects.sh + LaunchAgent
# com.jarvis.claudesync, alle 30 Min) — Ahmads Sitzungen entstehen zwar
# zwangslaeufig nur auf dem Mac (dort programmiert er wirklich), aber der
# Server muss dafuer nicht mehr live erreichbar sein und faellt bei
# "Mac aus" nicht mehr komplett aus, sondern liefert die letzte bekannte
# Kopie mit einem Alters-Hinweis.
IS_SERVER = os.environ.get("JARVIS_ROLE") == "server"
PROJECTS_DIR = (
    os.path.join(os.path.dirname(__file__), "claude_projects_sync")
    if IS_SERVER
    else os.path.expanduser("~/.claude/projects")
)
SYNC_STAMP_FILE = os.path.join(PROJECTS_DIR, "_last_sync_epoch.txt")
APP_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")
MAX_PROJECTS = 3
MAX_TURNS_PER_PROJECT = 15
MAX_CHARS = 12000

# Never forward secrets that happen to be sitting in chat history back to the API.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk_[A-Za-z0-9]{20,}"),
]


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _extract_text(message: dict) -> str:
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p.strip())


def _recent_project_transcripts(max_projects: int = MAX_PROJECTS) -> list:
    """Neueste Transkript-Datei PRO Projekt-Ordner, sortiert nach zuletzt
    aktivem Projekt zuerst — Multi-Repo-Bewusstsein statt nur des einen
    ueber ALLE Projekte hinweg zuletzt beruehrten Transkripts."""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    project_latest = []
    for entry in os.scandir(PROJECTS_DIR):
        if not entry.is_dir():
            continue
        files = glob.glob(os.path.join(entry.path, "*.jsonl"))
        if not files:
            continue
        latest = max(files, key=os.path.getmtime)
        project_latest.append((os.path.getmtime(latest), entry.name, latest))
    project_latest.sort(key=lambda x: x[0], reverse=True)
    return project_latest[:max_projects]


def _project_label(slug: str, transcript_path: str) -> str:
    """Echten Projekt-Namen aus dem 'cwd'-Feld der Transkript-Eintraege lesen
    statt den Ordner-Slug zurueckzuuebersetzen — Bindestriche im echten Pfad
    (z.B. 'jarvis-voice-assistant') sind sonst nicht von den Bindestrichen
    unterscheidbar, die Claude Code selbst als Pfad-Trennzeichen einsetzt."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if cwd:
                    return os.path.basename(cwd.rstrip("/")) or cwd
    except OSError:
        pass
    return slug.lstrip("-")


def _read_app_status() -> str:
    """Notes Ahmad pastes in from Claude Desktop App chats (no local file access
    there, so no automated way to read those conversations directly)."""
    try:
        with open(APP_STATUS_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    return text[:MAX_CHARS]


def _sync_staleness_note() -> str:
    """Nur auf dem Server relevant — sagt ehrlich wie alt die gespiegelte
    Kopie ist, statt stillschweigend veraltete Daten als aktuell auszugeben."""
    if not IS_SERVER:
        return ""
    try:
        with open(SYNC_STAMP_FILE, encoding="utf-8") as f:
            synced_at = float(f.read().strip())
    except (OSError, ValueError):
        return "Hinweis: Noch keine Synchronisierung der Claude-Code-Sitzungen vom Mac erfolgt.\n\n"

    age_minutes = (time.time() - synced_at) / 60
    if age_minutes < 5:
        return ""
    if age_minutes < 90:
        return f"Hinweis: Diese Daten sind vom letzten Sync vor ca. {int(age_minutes)} Minuten, nicht live.\n\n"
    return f"Hinweis: Diese Daten sind vom letzten Sync vor ca. {age_minutes / 60:.1f} Stunden (Mac evtl. laenger aus), nicht live.\n\n"


def get_recent_context(question: str) -> str:
    """Return recent Claude Code conversation turns aus den zuletzt aktiven
    Projekten (Multi-Repo-Bewusstsein, Roadmap Punkt 18) PLUS any notes
    pasted in from Claude Desktop App chats, formatted for the Jarvis LLM."""
    blocks = []

    app_status = _read_app_status()
    if app_status:
        blocks.append(f"Notizen aus der Claude Desktop App:\n{app_status}")

    projects = _recent_project_transcripts()
    per_project_char_budget = MAX_CHARS // max(1, len(projects)) if projects else MAX_CHARS

    for _mtime, slug, path in projects:
        turns = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") not in ("user", "assistant"):
                        continue
                    text = _extract_text(entry.get("message", {}))
                    if text:
                        role = "Ahmad" if entry["type"] == "user" else "Claude Code"
                        turns.append(f"{role}: {text}")
        except OSError as e:
            blocks.append(f"ERROR: Konnte Verlauf fuer Projekt '{slug}' nicht lesen: {e}")
            continue

        if not turns:
            continue
        label = _project_label(slug, path)
        excerpt = "\n\n".join(turns[-MAX_TURNS_PER_PROJECT:])
        excerpt = _redact(excerpt)
        if len(excerpt) > per_project_char_budget:
            excerpt = excerpt[-per_project_char_budget:]
        blocks.append(f"Projekt '{label}':\n{excerpt}")

    if not blocks:
        return _sync_staleness_note() + "Kein Claude Code Gespraechsverlauf und keine App-Notizen gefunden."

    return _sync_staleness_note() + f"Frage von Ahmad: {question}\n\n" + "\n\n---\n\n".join(blocks)
