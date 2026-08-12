"""
Jarvis V2 — Memory
Persistent conversation history (survives server restarts — the old design
kept it in a dict that vanished every time the server restarted, i.e. every
double-clap) plus a long-term memory file Jarvis writes to itself whenever
something is worth keeping, so he actually knows where things stand instead
of starting fresh every time.

Long-term memory is CATEGORIZED (memory/profile.json), not a flat
chronological log — Ahmad's explicit call, 2026-08-06. The old flat log
(memory.md) had a real problem: once it grew past get_longterm_memory()'s
character budget, only the MOST RECENT entries survived into context —
meaning an early fact about, say, an important person in Ahmad's life could
silently fall out months later just because a burst of business notes got
added after it. Splitting into categories and giving each a fair share of
the budget means no single category can crowd another one out entirely.
"""

import json
import os
import time

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
HISTORY_PATH = os.path.join(MEMORY_DIR, "conversation_log.jsonl")
LONGTERM_PATH = os.path.join(MEMORY_DIR, "memory.md")  # legacy flat log — read once for migration, no longer written
PROFILE_PATH = os.path.join(MEMORY_DIR, "profile.json")
QUESTIONS_PATH = os.path.join(MEMORY_DIR, "pending_questions.jsonl")
KNOWLEDGE_PATH = os.path.join(MEMORY_DIR, "knowledge.md")
BRIEFING_STATE_PATH = os.path.join(MEMORY_DIR, "briefing_state.json")
PROFILE_ARCHIVE_PATH = os.path.join(MEMORY_DIR, "profile_archive.jsonl")
CONVERSATION_ARCHIVE_PATH = os.path.join(MEMORY_DIR, "conversation_log_archive.jsonl")
os.makedirs(MEMORY_DIR, exist_ok=True)

MEMORY_CATEGORIES = ("preferences", "people", "decisions", "business", "self", "general")
CATEGORY_LABELS = {
    "preferences": "Vorlieben & Arbeitsstil",
    "people": "Wichtige Menschen",
    "decisions": "Entscheidungen & Ergebnisse",
    "business": "Business-Fakten",
    "self": "Selbstreflektion (eigene Fehler & Grenzen)",
    "general": "Sonstiges",
}

MAX_HISTORY_TURNS = 30


def append_turn(role: str, text: str):
    """role: 'user' or 'assistant'"""
    if not text:
        return
    entry = {"role": role, "text": text, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_recent_history(max_turns: int = MAX_HISTORY_TURNS) -> str:
    if not os.path.exists(HISTORY_PATH):
        return ""
    entries = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    recent = entries[-max_turns:]
    lines = []
    for e in recent:
        role = "Ahmad" if e["role"] == "user" else "Jarvis"
        lines.append(f"[{e.get('timestamp', '')}] {role}: {e['text']}")
    return "\n".join(lines)


def archive_old_conversation_log(days: int = 30) -> str:
    """Tier 2, Punkt 11 ('Dreaming') — conversation_log.jsonl waechst nur,
    wird aber nie bereinigt (get_recent_history() liest ohnehin nur die
    letzten MAX_HISTORY_TURNS). Alles aelter als `days` wandert nach
    CONVERSATION_ARCHIVE_PATH statt geloescht zu werden, rein alters-basiert,
    kein LLM-Urteil noetig."""
    if not os.path.exists(HISTORY_PATH):
        return "Kein Gespraechsverlauf vorhanden."

    entries = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    cutoff = time.time() - days * 86400
    old, recent = [], []
    for e in entries:
        try:
            ts = time.mktime(time.strptime(e.get("timestamp", ""), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            recent.append(e)  # unparsbarer Timestamp -> lieber behalten als faelschlich archivieren
            continue
        (old if ts < cutoff else recent).append(e)

    if not old:
        return "Nichts zu archivieren."

    with open(CONVERSATION_ARCHIVE_PATH, "a", encoding="utf-8") as f:
        for e in old:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        for e in recent:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    return f"{len(old)} Eintraege archiviert, {len(recent)} verbleiben aktiv."


def _load_profile() -> dict:
    if not os.path.exists(PROFILE_PATH):
        return _migrate_legacy_memory()
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {cat: [] for cat in MEMORY_CATEGORIES}


def _save_profile(profile: dict):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def _migrate_legacy_memory() -> dict:
    """One-time: fold the old flat memory.md log into the new categorized
    profile's 'general' bucket, so nothing Jarvis already learned gets lost
    in the switch. Runs at most once — after this, profile.json exists and
    this is never called again."""
    profile = {cat: [] for cat in MEMORY_CATEGORIES}
    if os.path.exists(LONGTERM_PATH):
        with open(LONGTERM_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("- ["):
                    continue
                try:
                    ts_end = line.index("]")
                    added = line[3:ts_end]
                    text = line[ts_end + 1:].strip()
                except ValueError:
                    continue
                if text:
                    profile["general"].append({"text": text, "added": added})
    _save_profile(profile)
    return profile


def get_longterm_memory(max_chars: int = 6000) -> str:
    """Every category with content gets a FAIR SHARE of max_chars (not a
    single 'keep the most recent N chars overall' cutoff) — so a category
    with few entries (e.g. people) can't get silently squeezed out just
    because another category (e.g. business) has many more entries."""
    profile = _load_profile()
    active = {cat: entries for cat in MEMORY_CATEGORIES if (entries := profile.get(cat))}
    if not active:
        return ""

    budget = max_chars // len(active)
    sections = []
    for cat, entries in active.items():
        lines = [f"- [{e['added']}] {e['text']}" for e in entries]
        kept = []
        total = 0
        for line in reversed(lines):  # most recent first, keep within budget
            total += len(line) + 1
            if total > budget and kept:
                break
            kept.insert(0, line)
        sections.append(f"[{CATEGORY_LABELS.get(cat, cat)}]\n" + "\n".join(kept))

    return "\n\n".join(sections)


def get_all_profile_entries() -> list:
    """Every entry across every category, flat, tagged with its category —
    for callers that rank/filter across categories themselves (e.g.
    semantic_memory.search_longterm_memory) instead of taking a fixed
    per-category slice like get_longterm_memory() does."""
    profile = _load_profile()
    entries = []
    for cat in MEMORY_CATEGORIES:
        for e in profile.get(cat, []):
            entries.append({"category": cat, "text": e["text"], "added": e["added"]})
    return entries


def consolidate_category(category: str, merges: list, archive_indices: list) -> str:
    """Tier 2, Punkt 11 ('Dreaming') — wendet eine bereits getroffene
    Konsolidierungs-Entscheidung mechanisch an (die Entscheidung selbst
    trifft background_brain.py's LLM-Klassifikation, diese Funktion tut
    nichts als Anwenden). merges: [{"keep_text": str, "merge_indices": [int,...]}, ...].
    archive_indices: Indizes die ganz verschwinden sollen (veraltet/ueberholt),
    ohne Ersatz-Eintrag. NIEMALS destruktiv: jeder betroffene Original-Eintrag
    landet unveraendert in PROFILE_ARCHIVE_PATH, bevor er aus profile.json
    verschwindet — ein Fehler in der Klassifikation kann so nie einen echten
    Fakt endgueltig vernichten."""
    profile = _load_profile()
    entries = profile.get(category, [])

    remove_indices = set(archive_indices)
    for m in merges:
        remove_indices.update(m["merge_indices"])

    archived = [entries[i] for i in sorted(remove_indices) if 0 <= i < len(entries)]
    if not archived:
        return "Nichts zu konsolidieren."

    with open(PROFILE_ARCHIVE_PATH, "a", encoding="utf-8") as f:
        for e in archived:
            f.write(json.dumps({**e, "category": category, "archived": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False) + "\n")

    survivors = [e for i, e in enumerate(entries) if i not in remove_indices]
    merged_new = [{"text": m["keep_text"], "added": time.strftime("%Y-%m-%d %H:%M")} for m in merges]
    profile[category] = survivors + merged_new
    _save_profile(profile)

    return f"{len(merges)} zusammengefuehrt, {len(archive_indices)} archiviert."


def get_category(category: str, max_chars: int = 3000) -> str:
    """Just ONE category's entries, most-recent-first — e.g. 'business', so
    a caller that only cares about business facts (jerome_comm's Jerome-chat
    knowledge, content_strategy's brand context) doesn't have to pull in
    unrelated categories like 'people' or 'preferences'. Live-Jarvis's own
    REMEMBER updates land here immediately (2026-08-06, Ahmad's ask: he
    should be able to update Jarvis's own knowledge mid-conversation, same
    as Claude Code updating files, not just via the separate background
    research process)."""
    profile = _load_profile()
    entries = profile.get(category, [])
    if not entries:
        return ""
    lines = [f"- [{e['added']}] {e['text']}" for e in entries]
    kept = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        if total > max_chars and kept:
            break
        kept.insert(0, line)
    return "\n".join(kept)


def remember(fact: str, category: str = "general") -> str:
    """Append a durable fact/insight Jarvis decided is worth keeping long-term.
    category: one of MEMORY_CATEGORIES — unknown/missing values fall back to
    'general' rather than erroring, since a slightly-off category from the
    LLM shouldn't lose the fact itself."""
    category = category if category in MEMORY_CATEGORIES else "general"
    profile = _load_profile()
    profile.setdefault(category, []).append({
        "text": fact,
        "added": time.strftime("%Y-%m-%d %H:%M"),
    })
    _save_profile(profile)
    return "Gemerkt."


# ---------------------------------------------------------------------------
# Pending questions — things Jarvis wants to ask Ahmad but shouldn't block on.
# He raises them once in the next "activate" briefing, then they're marked
# asked so they don't get repeated every single time.
# ---------------------------------------------------------------------------

_URGENCY_RANK = {"high": 0, "medium": 1, "low": 2}


def add_pending_question(question: str, urgency: str = "medium") -> str:
    """Queue a question Jarvis wants to ask Ahmad next time they talk.
    urgency: 'low'|'medium'|'high', normally set by background_brain.py's
    _classify_question_urgency() — see get_open_questions() for how this
    affects ordering."""
    if urgency not in _URGENCY_RANK:
        urgency = "medium"
    entry = {
        "question": question,
        "status": "open",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "asked": None,
        "urgency": urgency,
    }
    with open(QUESTIONS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return "Frage vorgemerkt."


def _read_all_questions() -> list:
    if not os.path.exists(QUESTIONS_PATH):
        return []
    entries = []
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_all_questions(entries: list):
    with open(QUESTIONS_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def get_open_questions(limit: int = 5) -> list:
    """Questions never yet surfaced to Ahmad, most urgent first (high before
    medium before low), oldest first within the same urgency so nothing
    starves indefinitely behind a steady stream of higher-urgency items.
    Sorting here (not per-consumer) means every reader — the passive
    per-turn context (format_open_questions) AND handle_activate's explicit
    'ask this now' block — gets the same ordering for free."""
    open_q = [e for e in _read_all_questions() if e.get("status") == "open"]

    def _sort_key(e):
        rank = _URGENCY_RANK.get(e.get("urgency", "medium"), 1)
        try:
            created = time.mktime(time.strptime(e["created"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError):
            created = 0
        return (rank, created)

    open_q.sort(key=_sort_key)
    return open_q[:limit]


def mark_questions_asked(questions: list = None):
    """Flip open questions to 'asked' so they aren't repeated every briefing.
    If `questions` is None, marks ALL currently-open questions as asked."""
    entries = _read_all_questions()
    targets = set(questions) if questions else None
    for e in entries:
        if e.get("status") == "open" and (targets is None or e["question"] in targets):
            e["status"] = "asked"
            e["asked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_all_questions(entries)


def has_recent_question_about(*fragments: str, hours: float = 20) -> bool:
    """True if ANY question (open OR already 'asked') containing every
    fragment was created within the last `hours`. For callers that requeue
    the same nudge periodically (e.g. trial_wave_nudge_pass) and need to
    dedup against MORE than just currently-open questions — a question
    flips to 'asked' the instant it's included in an activate prompt (see
    mark_questions_asked above), NOT once Ahmad has actually heard it (the
    LLM might not voice it, or the reply might get cut short), so checking
    only get_open_questions() let the same nudge get silently re-queued a
    few minutes after the first one was swallowed unheard (caught live,
    2026-08-06 — same trial-reel question queued twice in 6 minutes)."""
    cutoff = time.time() - hours * 3600
    for q in _read_all_questions():
        if not all(f in q["question"] for f in fragments):
            continue
        try:
            created = time.mktime(time.strptime(q["created"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError):
            continue
        if created >= cutoff:
            return True
    return False


def format_open_questions() -> str:
    """Human-readable block of unanswered questions, for the system prompt.
    Already sorted most-urgent-first by get_open_questions(); 'high' gets a
    visible marker so the model actually treats it differently, not just
    silently benefits from position in the list — medium/low stay plain,
    otherwise every question ends up with a label and the marker stops
    meaning anything."""
    open_q = get_open_questions()
    if not open_q:
        return ""
    lines = []
    for e in open_q:
        prefix = "[WICHTIG] " if e.get("urgency") == "high" else ""
        lines.append(f"- {prefix}{e['question']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live events — background_brain.py's bridge into an ALREADY-OPEN conversation
# in server.py. Unlike pending_questions above (only surfaced at the START of
# the next "activate"), these are polled by server.py's own background task
# and spoken into a live session the moment one is connected. background_brain
# is the sole writer (append-only, via _alert()); server.py is the sole
# resolver (read-all/mutate/rewrite, same convention as mark_questions_asked
# above). Has an `id` unlike pending_questions — needed because many polls
# from a DIFFERENT process match against the same entries, where plain-text
# matching would be fragile against a repeated alert string.
# ---------------------------------------------------------------------------

LIVE_EVENTS_PATH = os.path.join(MEMORY_DIR, "live_events.jsonl")
LIVE_EVENT_MAX_AGE_SECONDS = 30 * 60  # older than this: skip, don't speak out of context


def add_live_event(text: str):
    """Queue a finding for server.py to speak into a live conversation, if
    one happens to be open. Best-effort — must never raise, this runs from
    inside background_brain's _alert() which itself only wraps this in a
    try/except as a second line of defense."""
    try:
        entry = {
            "id": int(time.time() * 1000),
            "text": text,
            "status": "pending",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_epoch": time.time(),
        }
        with open(LIVE_EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_all_live_events() -> list:
    if not os.path.exists(LIVE_EVENTS_PATH):
        return []
    entries = []
    with open(LIVE_EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_all_live_events(entries: list):
    with open(LIVE_EVENTS_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def get_pending_live_events() -> list:
    return [e for e in _read_all_live_events() if e.get("status") == "pending"]


def resolve_live_events(ids: list, status: str):
    """status: 'delivered' or 'skipped_stale'."""
    id_set = set(ids)
    entries = _read_all_live_events()
    for e in entries:
        if e.get("id") in id_set:
            e["status"] = status
            e["resolved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_all_live_events(entries)


# ---------------------------------------------------------------------------
# Self-Improve-Changelog (Roadmap Punkt 22) — self_improve_pass in
# background_brain.py laesst Claude Code unsupervised Fehler fixen, das
# Ergebnis war bisher nur in einer Log-Datei sichtbar (bewusst still im
# laufenden Chat, siehe Kommentar dort). Dieses Log macht es NACHTRAEGLICH
# nachvollziehbar (morgens im Briefing, oder auf explizite Nachfrage), ohne
# den laufenden Chat zu unterbrechen.
# ---------------------------------------------------------------------------

SELF_IMPROVE_CHANGELOG_PATH = os.path.join(MEMORY_DIR, "self_improve_changelog.jsonl")


def add_self_improve_entry(errors_summary: str, result: str, commit_hash: str = None):
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "errors_summary": errors_summary[:500],
            "result": result[:1000],
            "commit_hash": commit_hash,
        }
        with open(SELF_IMPROVE_CHANGELOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_recent_self_improve_entries(hours: int = 24) -> list:
    if not os.path.exists(SELF_IMPROVE_CHANGELOG_PATH):
        return []
    cutoff = time.time() - hours * 3600
    entries = []
    with open(SELF_IMPROVE_CHANGELOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("epoch", 0) >= cutoff:
                entries.append(entry)
    return entries


def format_recent_self_improve_summary(hours: int = 24) -> str:
    """Ein knapper Satz fuers Morgen-Briefing (Roadmap Punkt 6), leer wenn
    nichts passiert ist — bleibt so still, wenn es nichts zu berichten gibt."""
    entries = get_recent_self_improve_entries(hours)
    if not entries:
        return ""
    if len(entries) == 1:
        return f"Nachts hat sich Jarvis selbst um einen Fehler gekuemmert: {entries[0]['result'][:150]}"
    return f"Nachts hat sich Jarvis selbststaendig um {len(entries)} Fehler gekuemmert, zuletzt: {entries[-1]['result'][:150]}"


# ---------------------------------------------------------------------------
# Skill-Growth (Ahmad, 2026-08-10: "ich brauche es, damit er eigenstaendiger
# wird") — skill_growth_pass in background_brain.py laesst Claude Code
# gezielt NEUE Werkzeuge bauen (nicht nur Fehler fixen wie Self-Improve
# oben), reaktiv auf beobachtete Luecken oder eigene Ideen. Gleiches
# Sichtbarkeits-Prinzip wie beim Self-Improve-Changelog: still im laufenden
# Chat, aber nachvollziehbar im Morgen-Briefing / auf Nachfrage.
# ---------------------------------------------------------------------------

SKILL_GROWTH_CHANGELOG_PATH = os.path.join(MEMORY_DIR, "skill_growth_changelog.jsonl")
SKILL_GROWTH_STATE_PATH = os.path.join(MEMORY_DIR, "skill_growth_state.json")
SELF_BUILT_SKILLS_PATH = os.path.join(MEMORY_DIR, "self_built_skills_confirmed.json")

MAX_SKILL_BUILDS_PER_DAY = 2  # bewusst niedrig gegen unkontrolliertes Wachstum


def add_skill_growth_entry(gap_or_idea: str, result: str, commit_hash: str = None):
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "gap_or_idea": gap_or_idea[:500],
            "result": result[:1000],
            "commit_hash": commit_hash,
        }
        with open(SKILL_GROWTH_CHANGELOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_recent_skill_growth_entries(hours: int = 24) -> list:
    if not os.path.exists(SKILL_GROWTH_CHANGELOG_PATH):
        return []
    cutoff = time.time() - hours * 3600
    entries = []
    with open(SKILL_GROWTH_CHANGELOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("epoch", 0) >= cutoff:
                entries.append(entry)
    return entries


def format_recent_skill_growth_summary(hours: int = 24) -> str:
    """Ein knapper Satz fuers Morgen-Briefing, leer wenn nichts passiert ist."""
    entries = get_recent_skill_growth_entries(hours)
    if not entries:
        return ""
    if len(entries) == 1:
        return f"Jarvis hat sich selbst eine neue Faehigkeit gegeben: {entries[0]['result'][:150]}"
    return f"Jarvis hat sich selbst {len(entries)} neue Faehigkeiten gegeben, zuletzt: {entries[-1]['result'][:150]}"


def get_skill_builds_today() -> int:
    """Tageslimit-Zaehler (MAX_SKILL_BUILDS_PER_DAY) — resettet sich selbst
    bei Datumswechsel, indem ein alter Stand einfach als 0 gilt."""
    if not os.path.exists(SKILL_GROWTH_STATE_PATH):
        return 0
    try:
        with open(SKILL_GROWTH_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    if state.get("date") != time.strftime("%Y-%m-%d"):
        return 0
    return state.get("builds_today", 0)


def increment_skill_builds_today():
    today = time.strftime("%Y-%m-%d")
    current = get_skill_builds_today()  # bereits datumsbewusst (0 bei neuem Tag)
    with open(SKILL_GROWTH_STATE_PATH, "w") as f:
        json.dump({"date": today, "builds_today": current + 1}, f)


def is_self_built_skill_confirmed(tool_name: str) -> bool:
    """Bestaetigungs-Gate fuer selbst gebaute Werkzeuge mit echtem
    Seiteneffekt (Nachricht senden, Kalender, Geld, Kauf) — von
    GENERIERTEM Tool-Code aus server.py aufgerufen, siehe skill_growth_pass'
    Task-Prompt in background_brain.py. Erste echte Nutzung braucht Ahmads
    ausdrueckliche Bestaetigung, danach laeuft es automatisch."""
    if not os.path.exists(SELF_BUILT_SKILLS_PATH):
        return False
    try:
        with open(SELF_BUILT_SKILLS_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get(tool_name))


def mark_self_built_skill_confirmed(tool_name: str):
    data = {}
    if os.path.exists(SELF_BUILT_SKILLS_PATH):
        try:
            with open(SELF_BUILT_SKILLS_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
    data[tool_name] = {"confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(SELF_BUILT_SKILLS_PATH, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Handlungsprotokoll — was JARVIS SELBST getan hat (Ahmad, 2026-08-12: er
# fragte, ob Jarvis selbst gestern ein Trial Reel gepostet hatte oder Jerome.
# Darauf gab es keine Antwort, weil Jarvis ueber eigene vergangene Handlungen
# ueberhaupt kein Gedaechtnis hatte — Werkzeug-Aufrufe verschwanden nach dem
# Chat, autonome Hintergrund-Aktionen standen nur in einer Log-Datei, die
# kein Werkzeug lesen konnte.)
#
# Bewusst getrennt von memory.md/profile.json (das ist GESPRAECHS-Wissen) und
# vom Self-Improve-/Skill-Growth-Changelog (das sind Code-Aenderungen an sich
# selbst): hier stehen ausgefuehrte HANDLUNGEN mit Zeitstempel, damit die
# Frage "war das ich?" aus Belegen und nicht aus Erinnerungsgefuehl
# beantwortet wird. Geschrieben wird an genau drei Stellen, siehe
# `own_action_check` in server.py, das die Abdeckung auch im Ergebnis nennt.
# ---------------------------------------------------------------------------

ACTION_JOURNAL_PATH = os.path.join(MEMORY_DIR, "action_journal.jsonl")
ACTION_JOURNAL_MAX_BYTES = 2 * 1024 * 1024  # ab hier wird auf ACTION_JOURNAL_KEEP_DAYS gekuerzt
ACTION_JOURNAL_KEEP_DAYS = 120


def add_action_entry(
    action: str,
    target: str = "",
    detail: str = "",
    outcome: str = "ok",
    initiator: str = "ahmad",
) -> None:
    """Eine ausgefuehrte eigene Handlung protokollieren.

    action    — was getan wurde, kurz und maschinenlesbar (z.B. Tool-Name)
    target    — worauf (Empfaenger, Account, Datei), leer wenn nichts passt
    detail    — eine Zeile Klartext, gekappt
    outcome   — 'ok' | 'error' | 'timeout' | 'blocked'
    initiator — 'ahmad' (im Gespraech angestossen) | 'autonom' (Hintergrund)

    Best-effort: darf NIE werfen. Das Protokoll ist Beleg, aber kein Grund,
    eine laufende Aktion scheitern zu lassen — ein fehlgeschlagener Schreib-
    versuch wuerde sonst z.B. eine bereits gesendete WhatsApp als Fehler
    dastehen lassen."""
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "action": str(action)[:120],
            "target": str(target)[:200],
            "detail": str(detail)[:400],
            "outcome": str(outcome)[:20],
            "initiator": str(initiator)[:20],
        }
        with open(ACTION_JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _prune_action_journal_if_large()
    except OSError:
        pass


def _prune_action_journal_if_large() -> None:
    """Das Protokoll waechst mit jedem Werkzeug-Aufruf und jedem autonomen
    Hintergrund-Durchlauf, also dauerhaft. Erst ab ACTION_JOURNAL_MAX_BYTES
    kuerzen (nicht bei jedem Schreiben eine Datei neu schreiben) und dabei
    nur wirklich alte Eintraege wegwerfen. Wichtig: dass gekuerzt wurde, ist
    ueber get_action_journal_start() sichtbar — sonst wuerde ein leeres
    Protokoll spaeter wie 'da war nichts' aussehen statt wie 'so weit reicht
    mein Protokoll nicht zurueck'."""
    try:
        if os.path.getsize(ACTION_JOURNAL_PATH) < ACTION_JOURNAL_MAX_BYTES:
            return
        cutoff = time.time() - ACTION_JOURNAL_KEEP_DAYS * 86400
        kept = [e for e in _read_action_entries() if e.get("epoch", 0) >= cutoff]
        with open(ACTION_JOURNAL_PATH, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_action_entries() -> list:
    if not os.path.exists(ACTION_JOURNAL_PATH):
        return []
    entries = []
    try:
        with open(ACTION_JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def get_action_entries(since_epoch: float = None, until_epoch: float = None) -> list:
    """Protokollierte Handlungen in einem Zeitfenster, aufsteigend sortiert."""
    entries = [
        e for e in _read_action_entries()
        if (since_epoch is None or e.get("epoch", 0) >= since_epoch)
        and (until_epoch is None or e.get("epoch", 0) < until_epoch)
    ]
    return sorted(entries, key=lambda e: e.get("epoch", 0))


def get_action_journal_start() -> str:
    """Zeitstempel des aeltesten vorhandenen Eintrags, oder "" wenn das
    Protokoll leer ist. Damit laesst sich sagen, ob ein Tag ueberhaupt
    abgedeckt ist — ohne diese Angabe wuerde 'keine Eintraege fuer gestern'
    faelschlich als 'ich habe gestern nichts getan' gelesen."""
    entries = _read_action_entries()
    if not entries:
        return ""
    return min(entries, key=lambda e: e.get("epoch", 0)).get("timestamp", "")


# ---------------------------------------------------------------------------
# Knowledge base — durable facts Jarvis researched/collected himself in the
# background (algorithm changes, niche trends, business insights). Separate
# from memory.md (which is conversation-derived) so autonomous research
# doesn't get mixed up with things said in a live conversation.
# ---------------------------------------------------------------------------

def add_knowledge(entry_text: str, category: str = "general") -> str:
    """Append a researched fact/insight to the persistent knowledge base."""
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    with open(KNOWLEDGE_PATH, "a", encoding="utf-8") as f:
        f.write(f"- [{timestamp}] ({category}) {entry_text}\n")
    return "Wissen gespeichert."


def get_knowledge(max_chars: int = 6000) -> str:
    if not os.path.exists(KNOWLEDGE_PATH):
        return ""
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        text = f.read().strip()
    return text[-max_chars:] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Daily briefing/summary cadence — Ahmad's explicit call: the full
# Fanplace/Mail/Instagram rundown should happen once per day, not on every
# single "Jarvis, lets go". A second, separate end-of-day summary (sent
# autonomously by background_brain.py, not tied to Ahmad opening Jarvis at
# all) rounds out the day. Tracked as plain date strings — simplest thing
# that correctly survives restarts and day boundaries.
# ---------------------------------------------------------------------------

def has_full_briefing_today() -> bool:
    if not os.path.exists(BRIEFING_STATE_PATH):
        return False
    try:
        with open(BRIEFING_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return state.get("last_full_briefing_date") == time.strftime("%Y-%m-%d")


def mark_full_briefing_done():
    state = {}
    if os.path.exists(BRIEFING_STATE_PATH):
        try:
            with open(BRIEFING_STATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    state["last_full_briefing_date"] = time.strftime("%Y-%m-%d")
    with open(BRIEFING_STATE_PATH, "w") as f:
        json.dump(state, f)


def has_daily_summary_today() -> bool:
    if not os.path.exists(BRIEFING_STATE_PATH):
        return False
    try:
        with open(BRIEFING_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return state.get("last_daily_summary_date") == time.strftime("%Y-%m-%d")


def mark_daily_summary_done():
    state = {}
    if os.path.exists(BRIEFING_STATE_PATH):
        try:
            with open(BRIEFING_STATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    state["last_daily_summary_date"] = time.strftime("%Y-%m-%d")
    with open(BRIEFING_STATE_PATH, "w") as f:
        json.dump(state, f)


def has_morning_briefing_today() -> bool:
    """Roadmap Punkt 6 — Gegenstueck zu has_daily_summary_today(), gleiches Muster."""
    if not os.path.exists(BRIEFING_STATE_PATH):
        return False
    try:
        with open(BRIEFING_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return state.get("last_morning_briefing_date") == time.strftime("%Y-%m-%d")


def mark_morning_briefing_done():
    state = {}
    if os.path.exists(BRIEFING_STATE_PATH):
        try:
            with open(BRIEFING_STATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    state["last_morning_briefing_date"] = time.strftime("%Y-%m-%d")
    with open(BRIEFING_STATE_PATH, "w") as f:
        json.dump(state, f)


def has_content_brief_today() -> bool:
    if not os.path.exists(BRIEFING_STATE_PATH):
        return False
    try:
        with open(BRIEFING_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    return state.get("last_content_brief_date") == time.strftime("%Y-%m-%d")


def mark_content_brief_done():
    state = {}
    if os.path.exists(BRIEFING_STATE_PATH):
        try:
            with open(BRIEFING_STATE_PATH) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            state = {}
    state["last_content_brief_date"] = time.strftime("%Y-%m-%d")
    with open(BRIEFING_STATE_PATH, "w") as f:
        json.dump(state, f)


# --- Screen-Bewusstsein (Roadmap Punkt 19) ---
# Eigener, separater Log statt ueber remember()/das Profil-Fakten-System zu
# laufen — 48-70 Ambient-Eintraege am Tag wuerden das kuratierte Langzeit-
# gedaechtnis verwaessern, das fuer bewusst wichtige Fakten gedacht ist.
# Ahmads bewusste Entscheidung (per Rueckfrage): nur Text, nie Bilder,
# 30 Tage rollierend.

SCREEN_AWARENESS_LOG_PATH = os.path.join(MEMORY_DIR, "screen_awareness_log.jsonl")
SCREEN_AWARENESS_RETENTION_DAYS = 30


def add_screen_awareness_entry(text: str):
    if not text:
        return
    os.makedirs(MEMORY_DIR, exist_ok=True)
    entries = _load_screen_awareness_entries()
    entries.append({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text})

    cutoff = time.time() - SCREEN_AWARENESS_RETENTION_DAYS * 86400
    entries = [e for e in entries if _parse_timestamp(e.get("timestamp", "")) >= cutoff]

    with open(SCREEN_AWARENESS_LOG_PATH, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _parse_timestamp(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def _load_screen_awareness_entries() -> list:
    if not os.path.exists(SCREEN_AWARENESS_LOG_PATH):
        return []
    entries = []
    with open(SCREEN_AWARENESS_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def get_recent_screen_awareness(max_entries: int = 3) -> str:
    """Fuer die Ambient-Einbindung ins System-Prompt — Jarvis 'weiss' ohne
    Nachfrage grob woran Ahmad zuletzt gearbeitet hat."""
    entries = _load_screen_awareness_entries()[-max_entries:]
    if not entries:
        return ""
    return "\n".join(f"[{e['timestamp']}] {e['text']}" for e in entries)


def search_screen_awareness(query: str, max_results: int = 15) -> str:
    """Einfache Substring-Suche fuer explizite Nachfragen ("was hab ich
    heute gemacht") — bewusst KEINE Embedding-Suche, das waere fuer dieses
    hochfrequente, niedrigwertige Log ueberdimensioniert (siehe semantic_
    memory.py fuer die richtige Stelle fuer kuratierte Fakten)."""
    entries = _load_screen_awareness_entries()
    if not entries:
        return "Noch keine Bildschirm-Beobachtungen aufgezeichnet."
    query = (query or "").strip().lower()
    if query:
        matches = [e for e in entries if query in e["text"].lower() or query in e.get("timestamp", "")]
    else:
        matches = entries
    matches = matches[-max_results:]
    if not matches:
        return f"Keine Bildschirm-Beobachtungen zu '{query}' in den letzten {SCREEN_AWARENESS_RETENTION_DAYS} Tagen gefunden."
    return "\n".join(f"[{e['timestamp']}] {e['text']}" for e in matches)


# --- Mahlzeiten-Log (Ahmads Cockpit, 2026-08-11) ---
# Gleiches Prinzip wie der Screen-Awareness-Log oben: ein einfaches, simples
# Tagebuch, KEIN Kalorien-/Makro-Tracking (dafuer gibt es keine Datenquelle,
# nichts erfinden). Eigener Log statt remember()/Profil-System, damit taegliche
# Mahlzeiten-Eintraege nicht das kuratierte Fakten-Gedaechtnis verwaessern.

MEAL_LOG_PATH = os.path.join(MEMORY_DIR, "meal_log.jsonl")


def add_meal_entry(text: str):
    if not text:
        return
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(MEAL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text}, ensure_ascii=False) + "\n")


def get_recent_meals(days: int = 3) -> list:
    if not os.path.exists(MEAL_LOG_PATH):
        return []
    entries = []
    with open(MEAL_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    cutoff = time.time() - days * 86400
    return [e for e in entries if _parse_timestamp(e.get("timestamp", "")) >= cutoff]


# --- Apple-Health-Bruecke (Ahmads Cockpit, 2026-08-11) ---
# Apple Health selbst hat keine Server-API -- die etablierte Loesung ist die
# iOS-App "Health Auto Export" (REST-Automation, schickt HealthKit-Daten
# periodisch als JSON an eine eigene URL). Rohes Payload wird IMMER
# unveraendert geloggt, egal ob die folgende Best-Effort-Extraktion die
# tatsaechliche Feldform trifft -- die wird erst mit einem echten Payload von
# Ahmads Geraet final verifiziert, nichts hier ist geraten oder erfunden,
# nur eine defensive erste Annahme.

HEALTH_LOG_PATH = os.path.join(MEMORY_DIR, "health_log.jsonl")
HEALTH_LATEST_PATH = os.path.join(MEMORY_DIR, "health_latest.json")

_KNOWN_HEALTH_METRIC_FIELDS = {
    "sleep_analysis": ("asleep", "value", "qty"),
    "step_count": ("qty", "value"),
    "heart_rate": ("Avg", "avg", "value", "qty"),
    "resting_heart_rate": ("qty", "value"),
    "active_energy": ("qty", "value"),
}

# Live vorgefunden (2026-08-11, Ahmads erster echter Export): Health Auto
# Export liefert diese Groessen als VIELE Minuten-Datenpunkte, nicht als
# fertige Tagessumme -- ein Punkt wie "29,68" fuer step_count war in
# Wahrheit nur die letzte einzelne Minute, nicht der Tag. Fuer diese
# summierbaren Groessen wird deshalb ueber alle Punkte des letzten
# EXPORTIERTEN Tages aufsummiert. Alles andere (Herzfrequenz, Gehgeschwindigkeit
# etc.) sind Momentaufnahmen, dafuer bleibt der letzte Punkt richtig.
_CUMULATIVE_HEALTH_METRICS = {
    "step_count", "flights_climbed", "active_energy", "basal_energy_burned",
    "walking_running_distance", "distance_walking_running", "sleep_analysis",
    "water", "apple_exercise_time", "apple_stand_time",
}


def add_health_import(payload: dict):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(HEALTH_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"received_at": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": payload}, ensure_ascii=False) + "\n")

    def value_of(point, fields):
        for field in fields:
            if field in point:
                return point[field]
        return None

    latest = {}
    try:
        for m in (payload.get("data") or {}).get("metrics") or []:
            name = m.get("name")
            points = m.get("data") or []
            if not name or not points:
                continue
            fields = _KNOWN_HEALTH_METRIC_FIELDS.get(name, ("value", "qty"))
            points_sorted = sorted(points, key=lambda p: p.get("date", ""))
            last_point = points_sorted[-1]
            last_value = value_of(last_point, fields)
            if last_value is None:
                continue

            if name in _CUMULATIVE_HEALTH_METRICS:
                last_day = str(last_point.get("date", ""))[:10]
                day_total = sum(
                    (value_of(p, fields) or 0) for p in points_sorted
                    if str(p.get("date", ""))[:10] == last_day
                )
                latest[name] = {"value": round(day_total, 2), "date": last_day, "units": m.get("units")}
            else:
                latest[name] = {"value": last_value, "date": last_point.get("date"), "units": m.get("units")}
    except (AttributeError, TypeError):
        pass

    if not latest:
        return
    existing = {}
    if os.path.exists(HEALTH_LATEST_PATH):
        try:
            with open(HEALTH_LATEST_PATH) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(latest)
    existing["_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(HEALTH_LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def get_latest_health() -> dict:
    if not os.path.exists(HEALTH_LATEST_PATH):
        return {}
    try:
        with open(HEALTH_LATEST_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
