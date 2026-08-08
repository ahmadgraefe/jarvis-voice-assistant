"""
Jarvis V2 — Claude Code Context
Reads the most recent local Claude Code session transcript so Jarvis can
answer questions about what you've been discussing/working on.
"""

import glob
import json
import os
import re

TRANSCRIPTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
APP_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")
MAX_TURNS = 40
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


def _latest_transcript_path():
    files = glob.glob(TRANSCRIPTS_GLOB, recursive=True)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _read_app_status() -> str:
    """Notes Ahmad pastes in from Claude Desktop App chats (no local file access
    there, so no automated way to read those conversations directly)."""
    try:
        with open(APP_STATUS_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    return text[:MAX_CHARS]


def get_recent_context(question: str) -> str:
    """Return recent Claude Code conversation turns PLUS any notes pasted in from
    Claude Desktop App chats, formatted for the Jarvis LLM."""
    blocks = []

    app_status = _read_app_status()
    if app_status:
        blocks.append(f"Notizen aus der Claude Desktop App:\n{app_status}")

    path = _latest_transcript_path()
    if path:
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
            return f"ERROR: Konnte Claude Code Verlauf nicht lesen: {e}"

        excerpt = "\n\n".join(turns[-MAX_TURNS:])
        excerpt = _redact(excerpt)
        if len(excerpt) > MAX_CHARS:
            excerpt = excerpt[-MAX_CHARS:]
        blocks.append(f"Letzter Claude Code Gespraechsverlauf:\n{excerpt}")

    if not blocks:
        return "Kein Claude Code Gespraechsverlauf und keine App-Notizen gefunden."

    return f"Frage von Ahmad: {question}\n\n" + "\n\n---\n\n".join(blocks)
