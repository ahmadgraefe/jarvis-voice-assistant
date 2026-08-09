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
