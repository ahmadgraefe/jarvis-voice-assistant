"""
Jarvis V2 — Habit Tracker
Fuer Ahmads Cockpit (2026-08-11): taegliche Gewohnheiten mit Erledigt-Status und
Streak. Exaktes Grundmuster wie goal_tracker.py — eigenes Modul statt Anbau an
goal_tracker (Gewohnheiten sind wiederkehrend/taeglich, Ziele sind einmalig mit
Check-in-Intervall, unterschiedliche Formen). Reine lokale Datei-Operationen,
synchron, kein run_in_executor noetig.
"""

import json
import os
import time

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
HABITS_PATH = os.path.join(MEMORY_DIR, "habits.jsonl")
CHECKINS_PATH = os.path.join(MEMORY_DIR, "habit_checkins.jsonl")
os.makedirs(MEMORY_DIR, exist_ok=True)


def _read_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _append_jsonl(path: str, entry: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_all_habits(entries: list):
    with open(HABITS_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _read_all_habits() -> list:
    return _read_jsonl(HABITS_PATH)


def add_habit(name: str) -> str:
    name = name.strip()
    if not name:
        return "ERROR: kein Name angegeben."
    habits = _read_all_habits()
    if any(h["name"].lower() == name.lower() for h in habits):
        return f"Gewohnheit '{name}' gibt es schon."
    entry = {"id": int(time.time() * 1000), "name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _append_jsonl(HABITS_PATH, entry)
    return f"Gewohnheit '{name}' angelegt."


def find_habit(name_query: str) -> dict:
    name_query = name_query.strip().lower()
    for h in _read_all_habits():
        if name_query in h["name"].lower():
            return h
    return None


def check_in(name_query: str, date: str = None) -> str:
    habit = find_habit(name_query)
    if not habit:
        return f"ERROR: keine Gewohnheit gefunden, die zu '{name_query}' passt."
    date = date or time.strftime("%Y-%m-%d")
    checkins = _read_jsonl(CHECKINS_PATH)
    if any(c["habit_id"] == habit["id"] and c["date"] == date for c in checkins):
        return f"'{habit['name']}' war fuer {date} schon als erledigt markiert."
    _append_jsonl(CHECKINS_PATH, {"habit_id": habit["id"], "date": date})
    return f"'{habit['name']}' fuer {date} als erledigt markiert."


def _streak_for(habit_id, checkin_dates: set, days: int) -> int:
    """Laufende Streak rueckwarts ab heute, abgebrochen an der ersten Luecke."""
    streak = 0
    for i in range(days):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        if d in checkin_dates:
            streak += 1
        else:
            break
    return streak


def get_habits_with_status(days: int = 7) -> list:
    """Liste aller Gewohnheiten mit: heute erledigt (bool), laufende Streak, und
    einer Liste der letzten `days` Tage als bool-Array (fuer eine einfache
    Verlaufs-Anzeige im Cockpit, aelteste zuerst)."""
    habits = _read_all_habits()
    checkins = _read_jsonl(CHECKINS_PATH)
    today = time.strftime("%Y-%m-%d")
    result = []
    for h in habits:
        dates = {c["date"] for c in checkins if c["habit_id"] == h["id"]}
        history = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            history.append(d in dates)
        result.append({
            "name": h["name"],
            "done_today": today in dates,
            "streak": _streak_for(h["id"], dates, 60),
            "history": history,
        })
    return result


def format_habits_today() -> str:
    habits = get_habits_with_status()
    if not habits:
        return "Noch keine Gewohnheiten angelegt."
    lines = []
    for h in habits:
        status = "erledigt" if h["done_today"] else "offen"
        lines.append(f"- {h['name']}: {status} (Streak {h['streak']} Tage)")
    return "\n".join(lines)
