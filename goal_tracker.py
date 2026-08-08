"""
Jarvis V2 — Goal Tracker
Generisches Ziel-/Projekt-Tracking (Tier 1, Punkt 8, 2026-08-08) — dasselbe
Grundmuster wie jerome_comm.py's Trial-Reel-Wave-State-Machine (Kandidat
loggen, periodisch pruefen, bei Stillstand nachhaken), aber domaenenunabhaengig
statt an Instagram-Content-Scaling gebunden. Bewusst ein EIGENES Modul statt
eines Umbaus von Trial Reel Wave — die ist live im Einsatz fuer echte
Jerome-Koordination, daran zu ruehren waere unnoetiges Risiko.

Reine lokale Datei-Operationen, synchron — gleiches Muster wie memory.py's
eigene Funktionen (remember, add_pending_question), kein run_in_executor
noetig, das ist nur fuer echte Netzwerk-Calls da.
"""

import json
import os
import time

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
GOALS_PATH = os.path.join(MEMORY_DIR, "goals.jsonl")
os.makedirs(MEMORY_DIR, exist_ok=True)

VALID_STATUSES = ("active", "done", "abandoned")


def _read_all_goals() -> list:
    if not os.path.exists(GOALS_PATH):
        return []
    entries = []
    with open(GOALS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_all_goals(entries: list):
    with open(GOALS_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def add_goal(description: str, check_in_days: int = 3) -> str:
    """Neues Ziel anlegen. check_in_days steuert, wie oft goal_progress_pass
    (background_brain.py) proaktiv nachfragt, wenn nichts Neues gemeldet wurde."""
    entries = _read_all_goals()
    entry = {
        "id": int(time.time() * 1000),
        "description": description,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "check_in_days": max(1, check_in_days),
        "last_checked": time.time(),
        "status": "active",
        "notes": [],
    }
    entries.append(entry)
    _write_all_goals(entries)
    return "Ziel gemerkt."


def find_goal(title_query: str) -> dict:
    """Lockerer Teilstring-Match gegen aktive Ziele (case-insensitive) —
    gleiches Prinzip wie calendar_tools._find_matching_events_sync. Gibt
    None zurueck bei keinem oder mehrdeutigem Treffer, NIE geraten welches
    Ziel gemeint war."""
    query = title_query.strip().lower()
    matches = [
        e for e in _read_all_goals()
        if e.get("status") == "active" and query in e["description"].lower()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def get_active_goals() -> list:
    return [e for e in _read_all_goals() if e.get("status") == "active"]


def get_due_goals() -> list:
    """Aktive Ziele, deren eigener check_in_days-Takt ueberschritten ist —
    das fragt goal_progress_pass ab."""
    now = time.time()
    due = []
    for e in get_active_goals():
        last_checked = e.get("last_checked", 0)
        if now - last_checked >= e.get("check_in_days", 3) * 86400:
            due.append(e)
    return due


def update_goal(title_query: str, note: str = "", status: str = "") -> str:
    """Haengt eine Notiz an und/oder aendert den Status, setzt last_checked
    zurueck (ein Update IST ein frischer Check-in, kein weiterer Nudge noetig
    bis zum naechsten Takt). ERROR-String wenn title_query nichts Eindeutiges
    trifft."""
    if status and status not in VALID_STATUSES:
        return f"ERROR: status muss eine von {', '.join(VALID_STATUSES)} sein."

    entries = _read_all_goals()
    query = title_query.strip().lower()
    matches = [e for e in entries if e.get("status") == "active" and query in e["description"].lower()]
    if not matches:
        return f"ERROR: Kein aktives Ziel gefunden das zu '{title_query}' passt."
    if len(matches) > 1:
        titles = "; ".join(m["description"] for m in matches)
        return f"ERROR: Mehrere Ziele passen zu '{title_query}' ({titles}) — bitte praeziser benennen."

    target = matches[0]
    if note:
        target.setdefault("notes", []).append({"text": note, "added": time.strftime("%Y-%m-%d %H:%M:%S")})
    if status:
        target["status"] = status
    target["last_checked"] = time.time()
    _write_all_goals(entries)
    return f"Ziel '{target['description']}' aktualisiert."


def format_active_goals() -> str:
    active = get_active_goals()
    if not active:
        return "Keine offenen Ziele."
    lines = []
    for e in active:
        last_note = e["notes"][-1]["text"] if e.get("notes") else "noch kein Update"
        lines.append(f"- {e['description']} (letzter Stand: {last_note})")
    return "\n".join(lines)
