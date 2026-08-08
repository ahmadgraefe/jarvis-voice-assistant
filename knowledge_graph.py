"""
Jarvis V2 — Wissensgraph (Tier 2, Punkt 10).

Personen (Jerome, Geschaeftskontakte), Projekte (Luna Vale, Fanplace) und
Entscheidungen lebten bisher nur als Freitext in memory.py's Kategorien
people/business/decisions — beantwortet "was weiss ich ueber Jerome" grob,
aber nicht strukturiert "was haben wir bei Jerome zuletzt entschieden und
warum". Dieses Modul haelt ab jetzt Beziehungen zwischen zwei benannten
Dingen strukturiert fest (Person->Projekt, Entscheidung->Grund/Person),
statt sie im Fliesstext zu verstecken.

Bewusst NICHT Teil davon: automatisches Extrahieren von Relationen aus
bereits vorhandenem Freitext, und Outcome-Tracking (Entscheidung->Ergebnis)
— das ist Roadmap-Punkt 12, ein eigener kuenftiger Schritt.

Reine lokale SQLite-Datei, synchrone Aufrufe direkt aus async Handlern
heraus — gleiches Muster wie memory.py/goal_tracker.py (kein
run_in_executor, das ist nur fuer echte Netzwerk-Calls da).
"""

import os
import sqlite3
import time

import memory

DB_PATH = os.path.join(memory.MEMORY_DIR, "knowledge_graph.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entities ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "type TEXT NOT NULL, "
        "name TEXT NOT NULL, "
        "created TEXT NOT NULL, "
        "UNIQUE(type, name COLLATE NOCASE))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS relations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "from_id INTEGER NOT NULL, "
        "to_id INTEGER NOT NULL, "
        "relation TEXT NOT NULL, "
        "description TEXT, "
        "created TEXT NOT NULL)"
    )
    # Tier 2, Punkt 12 (Outcome-Tracking) — SQLite kennt kein ADD COLUMN IF
    # NOT EXISTS, daher idempotent per try/except. Nur fuer type='decision'
    # inhaltlich relevant, bei anderen Typen bleiben die Spalten leer.
    for column in ("status TEXT", "outcome TEXT", "outcome_updated TEXT"):
        try:
            conn.execute(f"ALTER TABLE entities ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    return conn


ENTITY_TYPES = ("person", "project", "decision", "other")


def _get_or_create_entity(conn: sqlite3.Connection, type_: str, name: str) -> int:
    type_ = type_ if type_ in ENTITY_TYPES else "other"
    name = name.strip()
    row = conn.execute(
        "SELECT id FROM entities WHERE type = ? AND name = ? COLLATE NOCASE", (type_, name)
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO entities (type, name, created) VALUES (?, ?, ?)",
        (type_, name, time.strftime("%Y-%m-%d %H:%M")),
    )
    return cur.lastrowid


def add_relation(from_name: str, from_type: str, relation: str, to_name: str, to_type: str, description: str = "") -> str:
    """Haelt eine Beziehung zwischen zwei benannten Dingen fest (z.B. Person
    Jerome, Relation 'arbeitet an', Projekt Luna Vale). Kein Dedup, gleiches
    Prinzip wie memory.remember() — Fakten sammeln sich einfach an."""
    conn = _get_conn()
    try:
        from_id = _get_or_create_entity(conn, from_type, from_name)
        to_id = _get_or_create_entity(conn, to_type, to_name)
        conn.execute(
            "INSERT INTO relations (from_id, to_id, relation, description, created) VALUES (?, ?, ?, ?, ?)",
            (from_id, to_id, relation.strip(), description.strip(), time.strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        return "Gemerkt."
    finally:
        conn.close()


def query_entity(name: str) -> str:
    """Alle bekannten Beziehungen zu einem benannten Ding, neueste zuerst —
    deckt Fragen wie 'was haben wir bei Jerome zuletzt entschieden' direkt
    ab. Kein Treffer -> expliziter Hinweis, nie raten (gleiches Prinzip wie
    goal_tracker.find_goal)."""
    conn = _get_conn()
    try:
        matches = conn.execute(
            "SELECT id, type, status, outcome FROM entities WHERE name LIKE ? COLLATE NOCASE",
            (f"%{name.strip()}%",),
        ).fetchall()
        if not matches:
            return f"Nichts zu '{name}' im Wissensgraph gefunden."
        entity_ids = [m[0] for m in matches]

        outcome_lines = []
        for _id, type_, status, outcome in matches:
            if type_ == "decision" and outcome:
                outcome_lines.append(f"Ergebnis: {status or 'mixed'} — {outcome}")

        placeholders = ",".join("?" * len(entity_ids))
        rows = conn.execute(
            f"""SELECT r.created, ef.name, r.relation, et.name, r.description
                FROM relations r
                JOIN entities ef ON ef.id = r.from_id
                JOIN entities et ON et.id = r.to_id
                WHERE r.from_id IN ({placeholders}) OR r.to_id IN ({placeholders})
                ORDER BY r.created DESC, r.id DESC""",
            entity_ids + entity_ids,
        ).fetchall()

        lines = list(outcome_lines)
        for created, from_name, relation, to_name, description in rows:
            line = f"- [{created}] {from_name} {relation} {to_name}"
            if description:
                line += f": {description}"
            lines.append(line)

        if not lines:
            return f"'{name}' ist bekannt, aber es gibt noch keine festgehaltenen Beziehungen dazu."
        return "\n".join(lines)
    finally:
        conn.close()


def record_outcome(decision_query: str, outcome: str, status: str = "mixed") -> str:
    """Tier 2, Punkt 12 — verknuepft eine bereits bekannte Entscheidung
    (angelegt ueber add_relation) mit ihrem spaeteren Ergebnis. Sucht NUR
    unter type='decision', nie raten welche gemeint war — bei keinem oder
    mehrdeutigem Treffer ein ERROR-String statt eine Vermutung (gleiches
    Prinzip wie goal_tracker.update_goal bei Mehrdeutigkeit). Legt NIE
    selbst eine neue Entscheidung an, wenn keine passt — Outcome-Tracking
    setzt voraus dass die Entscheidung schon bekannt ist."""
    conn = _get_conn()
    try:
        matches = conn.execute(
            "SELECT id, name FROM entities WHERE type = 'decision' AND name LIKE ? COLLATE NOCASE",
            (f"%{decision_query.strip()}%",),
        ).fetchall()
        if not matches:
            return f"ERROR: keine Entscheidung mit '{decision_query}' gefunden."
        if len(matches) > 1:
            names = ", ".join(f"'{m[1]}'" for m in matches)
            return f"ERROR: mehrere Entscheidungen passen auf '{decision_query}' ({names}), sei genauer."

        conn.execute(
            "UPDATE entities SET status = ?, outcome = ?, outcome_updated = ? WHERE id = ?",
            (status.strip(), outcome.strip(), time.strftime("%Y-%m-%d %H:%M"), matches[0][0]),
        )
        conn.commit()
        return "Ergebnis vermerkt."
    finally:
        conn.close()


def list_decisions(status: str = "") -> str:
    """Tier 2, Punkt 12 — alle bekannten Entscheidungen mit ihrem Stand,
    optional gefiltert nach Status. Deckt 'was haben wir bisher entschieden,
    was hat sich bewaehrt' direkt ab, das eigentliche Lern-Element aus der
    Roadmap-Beschreibung."""
    conn = _get_conn()
    try:
        if status.strip():
            rows = conn.execute(
                "SELECT name, status, outcome, created FROM entities "
                "WHERE type = 'decision' AND status = ? ORDER BY created DESC",
                (status.strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, status, outcome, created FROM entities "
                "WHERE type = 'decision' ORDER BY created DESC",
            ).fetchall()
        if not rows:
            return "Keine Entscheidungen im Wissensgraph gefunden."

        lines = []
        for name, status_, outcome, created in rows:
            lines.append(f"- [{created}] {name}: {status_ or 'offen'} — {outcome or 'noch kein Ergebnis vermerkt'}")
        return "\n".join(lines)
    finally:
        conn.close()
