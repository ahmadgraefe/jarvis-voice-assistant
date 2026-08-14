"""
Zusammenhaengender Kontext aus verstreuten Aufzeichnungen.

Was ueber Ahmad, sein Geschaeft und seine Accounts bekannt ist, liegt bewusst
in vielen getrennten Ablagen: dauerhaft Gemerktes, der Gespraechsverlauf, die
Bildschirm-Beobachtungen, das Handlungsprotokoll, Ziele und Gewohnheiten, die
Instagram-Messwerte, die Beitrags-Messungen und der Recherche-Wissensstand.
Jede einzelne Ablage beantwortet nur eine schmale Frage sauber. Dieses Modul
beantwortet die drei Fragen, die QUER dazu liegen und deshalb bisher nirgends
beantwortet wurden:

  1. Wie arbeitet er gerade, was mag er, woran sitzt er?  -> build_snapshot
  2. Was waere jetzt der naechste sinnvolle Zug?          -> suggest_next_action
  3. Was von dem, was gerade laeuft, passt auf welchen
     Luna-Vale-Account?                                   -> bridge_trends

Drei Regeln gelten fuer die gesamte Ausgabe dieses Moduls:

- Sie ist fuer Ahmad, nicht fuer einen Entwickler. Keine Dateinamen, keine
  Werkzeug- oder Feldnamen, kein Format-Kauderwelsch — nur der praktische
  Nutzen. Deshalb gibt es _ACTION_LABELS: das Handlungsprotokoll speichert
  Werkzeugnamen, hier draussen haben die nichts zu suchen.
- Fehlt etwas, heisst das "ich habe es nicht aufgezeichnet", nie "es ist nicht
  passiert". Instagram zeigt Followerzahlen ab 10.000 nur gerundet — eine
  unveraenderte Zahl ist deshalb KEIN Beleg fuer Stillstand, und genau das
  steht dann auch so in der Ausgabe statt eines stillen "+0".
- Die zwei urteilenden Funktionen (Vorschlag, Trend-Bruecke) sammeln die
  Belege selbst und lassen nur die Bewertung vom Sprachmodell machen. Faellt
  der Modellaufruf aus, kommen die Belege trotzdem zurueck — lieber rohe
  Fakten als gar keine Antwort.
"""

import json
import os
import re
import time

import goal_tracker
import habit_tracker
import instagram_tools
import memory

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# Gleicher Sprachmodell-Stand wie der Rest des Servers (server.py), damit es
# nur eine Stelle gibt, an der ein Modellwechsel nachgezogen werden muss.
_MODEL = "claude-haiku-4-5-20251001"

# Ein "Arbeitstag" beginnt hier um 05:00, nicht um Mitternacht: Ahmads
# aktivste Stunden sind 19-01 Uhr (nachgezaehlt im Gespraechsverlauf), und
# bei einer Tagesgrenze um Mitternacht wuerde jede Abendsitzung in zwei
# halbe Tage zerfallen -- das Zeitfenster "13 bis 1 Uhr" waere dann nicht
# mehr als solches erkennbar.
_DAY_START_HOUR = 5

_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

_HOUR_BLOCKS = (
    ("Vormittag", tuple(range(6, 12))),
    ("Nachmittag", tuple(range(12, 17))),
    ("Abend", tuple(range(17, 22))),
    ("Nacht", tuple(range(22, 24)) + tuple(range(0, 6))),
)

# Das Handlungsprotokoll haelt Werkzeugnamen fest (maschinenlesbar, richtig so).
# Nach draussen geht daraus Klartext. Unbekannte Eintraege fallen auf ihren
# Detailtext zurueck, nicht auf den rohen Bezeichner.
_ACTION_LABELS = {
    "remember": "etwas dauerhaft gemerkt",
    "track_goal": "ein Ziel festgehalten",
    "list_decisions": "frueher Entschiedenes nachgeschlagen",
    "query_relations": "Zusammenhaenge nachgeschlagen",
    "screen": "auf den Bildschirm geschaut",
    "screen_history": "im Bildschirm-Verlauf nachgesehen",
    "screen_click": "am Bildschirm etwas angeklickt",
    "screen_type": "am Bildschirm etwas eingetippt",
    "instagram_trend": "die Followerstaende durchgesehen",
    "video_analysis": "die Beitraege eines Accounts ausgewertet",
    "reel_analysis": "ein Reel ausgewertet",
    "account_history": "die Historie eines Accounts nachgeschlagen",
    "post_author_check": "geprueft, wer einen Beitrag veroeffentlicht hat",
    "post_published_check": "geprueft, ob ein Beitrag draussen ist",
    "own_action_check": "im eigenen Handlungsprotokoll nachgesehen",
    "trial_reel_check": "den Stand der Trial-Reels geprueft",
    "winner_track": "einen Winner eingetragen",
    "winner_status": "den Winner-Stand durchgesehen",
    "scaling_log": "eine Skalierung angestossen",
    "add_competitor": "einen Konkurrenz-Account aufgenommen",
    "remove_competitor": "einen Konkurrenz-Account gestrichen",
    "research": "zu einem Thema recherchiert",
    "search": "etwas nachgeschlagen",
    "search_compare": "mehrere Quellen verglichen",
    "news": "die Nachrichten durchgesehen",
    "browse": "eine Seite gelesen",
    "gmail_check": "die Mails durchgesehen",
    "whatsapp_send": "eine WhatsApp verschickt",
    "whatsapp_check": "die WhatsApp-Nachrichten durchgesehen",
    "whatsapp_an_jerome_gesendet": "Jerome eine Nachricht geschickt",
    "jerome_msg": "Jerome eine Nachricht geschickt",
    "jerome_brief": "Jerome ein Briefing geschickt",
    "jerome_check": "nachgesehen, ob Jerome geschrieben hat",
    "calendar_list": "in den Kalender geschaut",
    "calendar_add": "einen Termin eingetragen",
    "calendar_delete": "einen Termin geloescht",
    "fanplace_snapshot": "die Fanplace-Zahlen geholt",
    "sltbio_snapshot": "die Link-in-Bio-Zahlen geholt",
    "flight_search": "Fluege gesucht",
    "open_url": "eine Seite geoeffnet",
    "open_app": "eine App geoeffnet",
    "open_camera": "die Kamera geoeffnet",
    "claude_code_exec": "am eigenen Aufbau gearbeitet",
    "claude_code_context": "am eigenen Aufbau gearbeitet",
    "self_improve_log": "die eigenen Korrekturen durchgesehen",
    "skill_growth_log": "die neu gebauten Faehigkeiten durchgesehen",
    "simulate_decision": "eine Entscheidung durchgespielt",
    "boardroom": "eine Entscheidung im Beraterkreis geprueft",
    "delegate_subagents": "Arbeit parallel verteilt",
    "transition_test_delegated": "einen Transition-Test in Auftrag gegeben",
    "hintergrund_durchlauf": "im Hintergrund mitgelaufen",
}


# ---------------------------------------------------------------------------
# Kleine Helfer
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: str) -> list:
    """Best effort: eine kaputte Zeile darf nie die ganze Auswertung kosten."""
    if not path or not os.path.exists(path):
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def _epoch(timestamp) -> float:
    text = str(timestamp or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return 0.0


def _to_number(raw):
    """'14.800' -> 14800, '216.000' -> 216000, '14,8K' -> 14800, '1,2 Mio' -> 1200000.

    Der Punkt ist im Deutschen Tausender-, im Englischen Dezimaltrenner --
    beide Schreibweisen kommen in den Messwerten vor. Aufloesung: steht eine
    Einheit dabei (K/M/Mio), ist das Zeichen ein Dezimaltrenner, sonst ein
    Tausendertrenner.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(" ", " ")
    if not text:
        return None

    factor = 1
    if "mio" in text or "mrd" in text or re.search(r"\dm\b", text):
        factor = 1_000_000
    elif "tsd" in text or re.search(r"\dk\b", text) or text.endswith("k"):
        factor = 1_000

    digits = re.sub(r"[^\d.,]", "", text)
    if not digits:
        return None

    if factor > 1:
        digits = digits.replace(",", ".")
        if digits.count(".") > 1:
            head, _, tail = digits.rpartition(".")
            digits = head.replace(".", "") + "." + tail
        try:
            return int(round(float(digits) * factor))
        except ValueError:
            return None

    try:
        return int(digits.replace(".", "").replace(",", ""))
    except ValueError:
        return None


def _fmt(number) -> str:
    try:
        return f"{int(number):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "?"


def _visible_step(value) -> int:
    """Kleinste von aussen sichtbare Veraenderung einer Followerzahl.

    Instagram zeigt unter 10.000 die exakte Zahl (4.591, 528 -- so auch in
    den eigenen Messwerten), ab 10.000 auf Hunderter gerundet (14.800,
    46.700) und ab 100.000 auf Tausender (216.000, 165.000). Ohne diese
    Unterscheidung wuerde ein Zuwachs von 60 Followern bei einem grossen
    Account als 'unveraendert' durchgehen und als Stillstand gemeldet.
    """
    if value is None or value < 10_000:
        return 1
    if value < 100_000:
        return 100
    return 1_000


def _metric(raw, name):
    """Holt 'likes=190' / 'views=12,4K' aus einer Messnotiz. 'nicht sichtbar' -> None."""
    match = re.search(rf"{name}\s*=\s*([\d.,]+\s*(?:k|m|mio|tsd)?)", str(raw or ""), re.I)
    return _to_number(match.group(1)) if match else None


def _action_label(entry: dict) -> str:
    action = str(entry.get("action") or "")
    label = _ACTION_LABELS.get(action)
    if label:
        return label
    detail = str(entry.get("detail") or "").strip()
    if detail:
        return detail[:90]
    return action.replace("_", " ").strip() or "etwas getan"


def _days_ago(epoch: float) -> str:
    if not epoch:
        return "Zeitpunkt unbekannt"
    days = int((time.time() - epoch) // 86400)
    if days <= 0:
        return "heute"
    if days == 1:
        return "gestern"
    return f"vor {days} Tagen"


def _trim(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _own_accounts() -> list:
    return [h for h in _load_config().get("luna_vale_accounts", []) if h]


def _competitor_accounts() -> list:
    return [h for h in _load_config().get("competitor_accounts", []) if h]


# ---------------------------------------------------------------------------
# 1) Snapshot — Arbeitsmuster, Vorlieben, aktueller Fokus
# ---------------------------------------------------------------------------

def _work_day_key(epoch: float):
    """(Datum, Wochentag) des ARBEITSTAGS zu einem Zeitpunkt (Grenze 05:00)."""
    shifted = time.localtime(epoch - _DAY_START_HOUR * 3600)
    return time.strftime("%Y-%m-%d", shifted), shifted.tm_wday


def _hour_index(epoch: float) -> int:
    """Stunde als Position im Arbeitstag (0 = 05:00, 23 = 04:00)."""
    return (time.localtime(epoch).tm_hour - _DAY_START_HOUR) % 24


def _clock_hour(index: int) -> int:
    return (index + _DAY_START_HOUR) % 24


def _median(values):
    values = sorted(values)
    if not values:
        return None
    return values[len(values) // 2]


def _work_rhythm(days: int) -> list:
    """Beobachteter Arbeitsrhythmus aus den Zeitpunkten der Gespraeche.

    Bewusst NUR die Gespraeche: das Handlungsprotokoll besteht zu ueber 98%
    aus automatischen Hintergrund-Durchlaeufen, die rund um die Uhr laufen
    und ueber Ahmads eigenen Rhythmus nichts aussagen -- sie wuerden das
    Bild komplett zudecken.
    """
    cutoff = time.time() - days * 86400
    stamps = [
        _epoch(e.get("timestamp"))
        for e in _read_jsonl(memory.HISTORY_PATH)
        if e.get("role") == "user" and _epoch(e.get("timestamp")) >= cutoff
    ]
    if not stamps:
        return []

    per_day = {}
    weekdays = {}
    blocks = {name: 0 for name, _ in _HOUR_BLOCKS}

    for epoch in stamps:
        day, weekday = _work_day_key(epoch)
        per_day.setdefault(day, []).append(_hour_index(epoch))
        weekdays[weekday] = weekdays.get(weekday, 0) + 1
        clock = time.localtime(epoch).tm_hour
        for name, hours in _HOUR_BLOCKS:
            if clock in hours:
                blocks[name] += 1
                break

    lines = []
    active_days = len(per_day)
    lines.append(
        f"- Aktiv an {active_days} von {days} Tagen, im Schnitt "
        f"{round(len(stamps) / active_days)} Wortmeldungen pro aktivem Tag."
    )

    ranked = sorted(blocks.items(), key=lambda kv: kv[1], reverse=True)
    share = [f"{name} {round(100 * count / len(stamps))}%" for name, count in ranked if count]
    if share:
        lines.append("- Schwerpunkt der Aktivitaet: " + ", ".join(share[:3]) + ".")

    start = _median([min(hours) for hours in per_day.values()])
    end = _median([max(hours) for hours in per_day.values()])
    if start is not None and end is not None:
        first, last = _clock_hour(start), _clock_hour(end)
        # Endet der Tag nach Mitternacht, waere "00 Uhr" als Ende missverstaendlich
        # (klaenge nach Tagesbeginn) -- als 24 bzw. 25/26 Uhr durchgezaehlt bleibt
        # erkennbar, dass das Fenster in die Nacht laeuft.
        shown_last = last if last >= first else last + 24
        lines.append(
            f"- Uebliches Zeitfenster: etwa {first} bis {shown_last} Uhr "
            "(Mittelwert ueber die aktiven Tage)."
        )

    if weekdays:
        best = max(weekdays.items(), key=lambda kv: kv[1])[0]
        lines.append(f"- Staerkster Wochentag im Zeitraum: {_WEEKDAYS[best]}.")

    days_sorted = sorted(per_day)
    gaps = []
    for earlier, later in zip(days_sorted, days_sorted[1:]):
        gap = int((_epoch(later) - _epoch(earlier)) // 86400) - 1
        if gap > 0:
            gaps.append(gap)
    if gaps:
        lines.append(f"- Laengste ruhige Strecke am Stueck: {max(gaps)} Tage.")

    return lines


def _focus_lines(days: int) -> list:
    """Woran er zuletzt tatsaechlich sass — Beobachtungen plus eigene Handlungen."""
    lines = []

    recent = memory.get_recent_screen_awareness(3)
    if recent:
        for entry in recent.splitlines():
            text = entry.split("] ", 1)[-1] if "] " in entry else entry
            lines.append(f"- Zuletzt am Rechner beobachtet: {_trim(text, 180)}")

    cutoff = time.time() - days * 86400
    own = [
        e for e in memory.get_action_entries(since_epoch=cutoff)
        if e.get("initiator") in ("ahmad", "nachgetragen")
    ]
    # Zusammenfassen statt auflisten: fuenfmal hintereinander "am eigenen Aufbau
    # gearbeitet" ist keine fuenffache Information, sondern eine laengere Sitzung.
    condensed = []
    for entry in own[::-1]:
        label = _action_label(entry)
        day = _days_ago(entry.get("epoch", 0))
        if condensed and condensed[-1][0] == label and condensed[-1][1] == day:
            condensed[-1][2] += 1
            continue
        target = str(entry.get("target") or "").strip()
        condensed.append([label, day, 1, target])
        if len(condensed) >= 5:
            break

    for label, day, count, target in condensed:
        suffix = f" ({_trim(target, 60)})" if target else ""
        repeat = f", {count}x" if count > 1 else ""
        lines.append(f"- {day.capitalize()}: {label}{suffix}{repeat}")

    handles = _own_accounts() + _competitor_accounts()
    mentions = {}
    for entry in _read_jsonl(memory.HISTORY_PATH):
        if _epoch(entry.get("timestamp")) < cutoff:
            continue
        text = str(entry.get("text") or "").lower()
        for handle in handles:
            if handle.lower() in text:
                mentions[handle] = mentions.get(handle, 0) + 1
    if mentions:
        top = sorted(mentions.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lines.append(
            "- Im Gespraech am haeufigsten Thema: "
            + ", ".join(f"@{handle} ({count}x)" for handle, count in top)
            + "."
        )

    return lines


def _memory_lines(category: str, limit: int, max_chars: int = 180) -> list:
    entries = [e for e in memory.get_all_profile_entries() if e.get("category") == category]
    return [f"- {_trim(e.get('text'), max_chars)}" for e in entries[-limit:][::-1]]


def build_snapshot(days: int = 14, max_chars: int = 7000) -> str:
    """Ein zusammenhaengendes Bild: Fokus, Rhythmus, Vorlieben, Ziele, Offenes."""
    today = time.strftime("%d.%m.%Y")
    sections = [f"LAGEBILD (Stand {today}, Zeitraum: letzte {days} Tage)"]

    def add(title: str, lines: list):
        if lines:
            sections.append(f"— {title} —\n" + "\n".join(lines))

    add("Woran gerade gearbeitet wird", _focus_lines(days))
    add("Beobachteter Arbeitsrhythmus", _work_rhythm(days))
    add("Vorlieben und Zusammenarbeit", _memory_lines("preferences", 8))
    add("Feste Entscheidungen", _memory_lines("decisions", 5))
    add("Geschaeftliche Linie", _memory_lines("business", 5))
    add("Menschen im Umfeld", _memory_lines("people", 4))
    add("Grundsaetzliches", _memory_lines("general", 4))

    goals = goal_tracker.get_active_goals()
    if goals:
        add("Laufende Ziele", [f"- {_trim(g.get('description'), 140)}" for g in goals])

    try:
        habits = habit_tracker.get_habits_with_status()
    except Exception:
        habits = []
    if habits:
        add(
            "Gewohnheiten",
            [
                f"- {h['name']}: heute {'erledigt' if h['done_today'] else 'offen'}, "
                f"Serie {h['streak']} Tage"
                for h in habits
            ],
        )

    open_items = []
    for question in memory.get_open_questions(limit=4):
        open_items.append(f"- Noch ungeklaert: {_trim(question.get('question'), 160)}")
    for event in memory.get_pending_live_events()[:3]:
        open_items.append(f"- Noch nicht durchgegeben: {_trim(event.get('text'), 160)}")
    add("Offene Punkte", open_items)

    sections.append(
        "Grundlage: meine eigenen Aufzeichnungen aus dem genannten Zeitraum — Gespraeche, "
        "Bildschirm-Beobachtungen, dauerhaft Gemerktes, Ziele und Gewohnheiten. Was ich nicht "
        "gesehen habe, steht hier nicht — das heisst nicht, dass es nicht passiert ist."
    )

    text = "\n\n".join(sections)
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


# ---------------------------------------------------------------------------
# Account-Historie — gemeinsame Grundlage fuer Vorschlag und Trend-Bruecke
# ---------------------------------------------------------------------------

def _snapshots_by_handle() -> dict:
    grouped = {}
    for entry in _read_jsonl(instagram_tools.SNAPSHOTS_PATH):
        handle = entry.get("handle")
        if not handle or "followers" not in entry:
            continue
        grouped.setdefault(handle, []).append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda e: _epoch(e.get("timestamp")))
    return grouped


def _entry_at_or_before(entries: list, target_epoch: float):
    found = None
    for entry in entries:
        if _epoch(entry.get("timestamp")) <= target_epoch:
            found = entry
        else:
            break
    return found


def _videos_by_handle(days: int) -> dict:
    """Beitrags-Messungen je Account, pro Beitrag nur die juengste Messung."""
    cutoff = time.time() - days * 86400
    grouped = {}
    for entry in _read_jsonl(instagram_tools.VIDEO_ANALYSIS_PATH):
        handle = entry.get("handle")
        if not handle or "raw" not in entry or _epoch(entry.get("timestamp")) < cutoff:
            continue
        key = instagram_tools.normalize_video_url(str(entry.get("url") or ""))
        per_handle = grouped.setdefault(handle, {})
        previous = per_handle.get(key)
        if not previous or _epoch(entry.get("timestamp")) >= _epoch(previous.get("timestamp")):
            per_handle[key] = entry
    return {handle: list(posts.values()) for handle, posts in grouped.items()}


def _account_report(handle: str, snapshots: list, videos: list, actions: list, days: int) -> dict:
    """Alles Gemessene zu EINEM Account, als Klartextzeilen plus harte Zahlen."""
    now = time.time()
    lines = []
    facts = {"handle": handle, "followers": None, "new_posts_7d": None, "avg_likes": None}

    # Ein Messwert kann leer zurueckkommen (Profil nicht erreichbar, Check
    # gescheitert) -- bei @lunas.crypt ist das seit Tagen der Fall. Solche
    # Eintraege duerfen weder als Zahl noch als "0" durchgehen: gerechnet wird
    # nur mit verwertbaren Messungen, und dass danach nichts mehr kam, wird
    # ausdruecklich gesagt statt als "?" versteckt.
    usable = [s for s in snapshots if _to_number(s.get("followers")) is not None]
    latest = usable[-1] if usable else None
    blind_since = None
    if snapshots:
        newest_usable_epoch = _epoch(latest.get("timestamp")) if latest else 0
        blind = [s for s in snapshots if _epoch(s.get("timestamp")) > newest_usable_epoch]
        if blind:
            blind_since = (_epoch(blind[0].get("timestamp")), len(blind))

    snapshots = usable
    if latest:
        followers = _to_number(latest.get("followers"))
        facts["followers"] = followers
        head = f"@{handle}: {_fmt(followers)} Follower"

        for label, seconds in (("24 Stunden", 86400), ("7 Tagen", 7 * 86400)):
            earlier = _entry_at_or_before(snapshots, now - seconds)
            before = _to_number(earlier.get("followers")) if earlier else None
            if before is None or followers is None:
                continue
            delta = followers - before
            if delta:
                head += f", {delta:+d} in {label}"
            else:
                step = _visible_step(followers)
                if step > 1:
                    head += (
                        f", in {label} unveraendert — Instagram zeigt hier nur "
                        f"{step}er-Schritte, kleinere Zuwaechse sind von aussen gar nicht sichtbar"
                    )
                else:
                    head += f", in {label} unveraendert"
        lines.append(head)

        posts_now = _to_number(latest.get("posts"))
        week_ago = _entry_at_or_before(snapshots, now - 7 * 86400)
        posts_before = _to_number(week_ago.get("posts")) if week_ago else None
        if posts_now is not None:
            detail = f"  Beitraege insgesamt: {_fmt(posts_now)}"
            if posts_before is not None:
                new_posts = posts_now - posts_before
                facts["new_posts_7d"] = new_posts
                detail += f", davon {new_posts} in den letzten 7 Tagen"
            lines.append(detail)

        last_growth = None
        for earlier, later in zip(snapshots, snapshots[1:]):
            a, b = _to_number(earlier.get("posts")), _to_number(later.get("posts"))
            if a is not None and b is not None and b > a:
                last_growth = _epoch(later.get("timestamp"))
        if last_growth:
            lines.append(f"  Letzter gemessener neuer Beitrag: {_days_ago(last_growth)}")
        elif posts_now is not None:
            lines.append(
                f"  In meinen Messungen kein neuer Beitrag aufgetaucht, solange ich mitschreibe"
            )
    else:
        lines.append(f"@{handle}: keine einzige verwertbare Messung vorhanden")

    if blind_since:
        since, attempts = blind_since
        lines.append(
            f"  ACHTUNG: seit {_days_ago(since)} kommt bei diesem Account keine Zahl mehr zurueck "
            f"({attempts} Versuche ohne Ergebnis). Der Stand oben ist der letzte verwertbare — "
            "ueber die Zeit danach weiss ich nichts."
        )

    likes = [v for v in (_metric(e.get("raw"), "likes") for e in videos) if v is not None]
    if likes:
        average = round(sum(likes) / len(likes))
        facts["avg_likes"] = average
        lines.append(
            f"  Gemessene Beitraege im Zeitraum: {len(videos)}, im Schnitt {_fmt(average)} Likes"
        )
        best = max(videos, key=lambda e: _metric(e.get("raw"), "likes") or 0)
        best_likes = _metric(best.get("raw"), "likes")
        if best_likes and best_likes > average:
            lines.append(
                f"  Staerkster gemessener Beitrag: {_fmt(best_likes)} Likes "
                f"({round(best_likes / max(average, 1), 1)}-fach ueber dem eigenen Schnitt) — {best.get('url')}"
            )
    else:
        lines.append("  Keine Beitrags-Messungen im Zeitraum")

    touching = [
        entry for entry in actions
        if handle.lower() in (str(entry.get("target", "")) + " " + str(entry.get("detail", ""))).lower()
    ]
    if touching:
        last = touching[-1]
        lines.append(
            f"  Zuletzt daran gearbeitet: {_days_ago(last.get('epoch', 0))} "
            f"({_action_label(last)}), im Zeitraum insgesamt {len(touching)} Vorgaenge"
        )
    else:
        lines.append(f"  Im Zeitraum kein eigener Vorgang zu diesem Account aufgezeichnet")

    for entry in _remembered_about(handle)[-2:]:
        lines.append(f"  Dauerhaft gemerkt: {_trim(entry.get('text'), 200)}")

    facts["lines"] = lines
    return facts


def _mentioned_handles(text: str) -> int:
    lowered = str(text or "").lower()
    return sum(1 for handle in _own_accounts() if handle.lower() in lowered)


def _remembered_about(handle: str) -> list:
    """Nur was WIRKLICH diesen einen Account betrifft.

    Ein Satz, der drei oder mehr der eigenen Accounts nennt, ist eine allgemeine
    Feststellung ueber Luna Vale -- er wuerde sonst unter jedem einzelnen Account
    noch einmal auftauchen und die Belege um ein Vielfaches aufblaehen. Solche
    Saetze kommen ueber _shared_memory_lines genau einmal vor.
    """
    return [
        e for e in memory.get_all_profile_entries()
        if handle.lower() in str(e.get("text", "")).lower() and _mentioned_handles(e.get("text")) < 3
    ]


def _shared_memory_lines(limit: int = 3) -> list:
    """Dauerhaft Gemerktes, das fuer mehrere Accounts zugleich gilt — einmal genannt."""
    general = [
        e for e in memory.get_all_profile_entries()
        if _mentioned_handles(e.get("text")) >= 3
    ]
    return [f"- {_trim(e.get('text'), 260)}" for e in general[-limit:]]


def _account_signals(facts: dict) -> list:
    """Rein rechnerische Auffaelligkeiten — auch ohne Sprachmodell brauchbar."""
    signals = []
    handle = facts["handle"]

    new_posts = facts.get("new_posts_7d")
    if new_posts is not None and new_posts <= 0:
        signals.append(f"@{handle}: seit mindestens 7 Tagen kein neuer Beitrag gemessen.")
    elif new_posts is not None and new_posts >= 5:
        signals.append(f"@{handle}: hohe Schlagzahl, {new_posts} neue Beitraege in 7 Tagen.")

    followers = facts.get("followers")
    if followers is not None and followers < 1000:
        signals.append(
            f"@{handle}: mit {_fmt(followers)} Followern noch klein — hier ist jede Bewegung exakt "
            "messbar, Tests zahlen sich also am schnellsten aus."
        )

    if facts.get("avg_likes") is None:
        signals.append(
            f"@{handle}: keine Beitrags-Zahlen im Zeitraum — ueber die Wirkung kann ich nichts sagen."
        )

    return signals


def _gather_account_evidence(account: str, days: int):
    handles = _own_accounts()
    if account:
        wanted = account.strip().lstrip("@").lower()
        handles = [h for h in handles if h.lower() == wanted] or [wanted]

    snapshots = _snapshots_by_handle()
    videos = _videos_by_handle(days)
    actions = memory.get_action_entries(since_epoch=time.time() - days * 86400)

    reports = [
        _account_report(handle, snapshots.get(handle, []), videos.get(handle, []), actions, days)
        for handle in handles
    ]
    return reports


# ---------------------------------------------------------------------------
# 2) Naechste sinnvolle Aktion
# ---------------------------------------------------------------------------

async def suggest_next_action(ai_client, account: str = "", days: int = 14) -> str:
    """Ein bis drei begruendete naechste Schritte, gestuetzt auf die Account-Historie."""
    reports = _gather_account_evidence(account, days)
    if not reports:
        return "Zu diesen Accounts habe ich noch nichts aufgezeichnet, worauf ich einen Vorschlag stuetzen koennte."

    evidence = []
    shared = _shared_memory_lines()
    if shared:
        evidence.append("Gilt fuer alle Luna-Vale-Accounts gleichermassen:")
        evidence.extend(shared)
        evidence.append("")

    evidence.append("Gemessener Stand der eigenen Accounts (Zeitraum: letzte %d Tage):" % days)
    for report in reports:
        evidence.extend(report["lines"])

    signals = []
    for report in reports:
        signals.extend(_account_signals(report))
    if signals:
        evidence.append("")
        evidence.append("Rechnerisch auffaellig:")
        evidence.extend(f"- {s}" for s in signals)

    decisions = memory.get_category("decisions", max_chars=1200)
    business = memory.get_category("business", max_chars=1200)
    goals = goal_tracker.get_active_goals()

    constraints = []
    if decisions:
        constraints.append("Frueher getroffene Entscheidungen (gelten weiter):\n" + decisions)
    if business:
        constraints.append("Geschaeftliche Linie:\n" + business)
    if goals:
        constraints.append(
            "Laufende Ziele:\n" + "\n".join(f"- {g.get('description')}" for g in goals)
        )

    evidence_text = "\n".join(evidence)
    fallback = (
        evidence_text
        + "\n\n(Die Bewertung konnte ich gerade nicht erstellen — das oben sind die reinen Messwerte.)"
    )

    if ai_client is None:
        return fallback

    prompt = (
        "Du bist Ahmads Assistent. Unten stehen ausschliesslich Dinge, die ich wirklich gemessen "
        "oder festgehalten habe.\n\n"
        f"{evidence_text}\n\n"
        + ("\n\n".join(constraints) + "\n\n" if constraints else "")
        + "Schlage die 2 bis 3 naechsten sinnvollen Schritte vor, wichtigster zuerst.\n"
        "Regeln:\n"
        "- Jeder Schritt stuetzt sich auf eine konkrete Zahl oder Beobachtung von oben.\n"
        "- Erfinde keine Zahl, keinen Beitrag und keinen Zeitpunkt, der oben nicht steht.\n"
        "- Eine unveraenderte Followerzahl ist KEIN Beleg fuer Stillstand, wenn oben steht, "
        "dass Instagram nur gerundet anzeigt — behandle sie als 'nicht messbar'.\n"
        "- Widersprich keiner der oben genannten Entscheidungen.\n"
        "- Keine Dateinamen, keine Werkzeugnamen, keine technischen Bezeichner.\n"
        "- Deutsch, knapp. Pro Schritt genau eine Zeile im Format: "
        "'Schritt: … — weil: …'"
    )

    try:
        response = await ai_client.messages.create(
            model=_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        recommendation = response.content[0].text.strip()
    except Exception as e:
        return fallback + f"\n(Grund: {e})"

    return recommendation + "\n\nWoran ich das festmache:\n" + evidence_text


# ---------------------------------------------------------------------------
# 3) Trends mit den Luna-Vale-Accounts verbinden
# ---------------------------------------------------------------------------

_KNOWLEDGE_HEAD = re.compile(r"^- \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*\(([^)]*)\)\s*(.*)$")

_TREND_CATEGORIES = ("research", "discovery", "content-strategy", "boardroom", "on-demand")

# Aus den hinterlegten Nischen-Stichworten der Content-Planung abgeleitet, damit
# es nur EINE Wahrheit zur Positionierung gibt. Der Handle-Name taugt dafuer
# nicht: lunavalethegoth heisst "goth", bespielt aber Cosplay.
_KEYWORD_NICHE = {
    "gothaesthetic": "Goth / Dark Feminine",
    "cowgirlaesthetic": "Cowgirl / Western",
    "cosplaytransformation": "Cosplay / Charakter-Transformation",
}


def _positioning(handle: str) -> str:
    try:
        import content_strategy

        keywords = content_strategy.NICHE_KEYWORDS.get(handle) or []
    except Exception:
        keywords = []

    for keyword in keywords:
        if keyword in _KEYWORD_NICHE:
            return _KEYWORD_NICHE[keyword]

    lowered = handle.lower()
    for fragment, niche in (
        ("cowgirl", "Cowgirl / Western"),
        ("cosplay", "Cosplay / Charakter-Transformation"),
        ("goth", "Goth / Dark Feminine"),
        ("crypt", "Goth / Dark Feminine"),
        ("succubus", "Dark Fantasy"),
    ):
        if fragment in lowered:
            return niche + " (aus dem Namen geschlossen, nicht hinterlegt)"

    return "Positionierung nicht hinterlegt"


def _trend_blocks(days: int, topic: str = "", limit: int = 5) -> list:
    """Die juengsten Rechercheergebnisse als ganze Bloecke, nicht als Zeilen."""
    path = memory.KNOWLEDGE_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.read().splitlines()
    except OSError:
        return []

    cutoff = time.time() - days * 86400
    blocks, current = [], None
    for line in raw_lines:
        head = _KNOWLEDGE_HEAD.match(line)
        if head:
            if current:
                blocks.append(current)
            current = {
                "epoch": _epoch(head.group(1)),
                "category": head.group(2),
                "text": head.group(3),
            }
        elif current is not None:
            current["text"] += " " + line.strip()
    if current:
        blocks.append(current)

    fresh = [
        b for b in blocks
        if b["epoch"] >= cutoff and b["category"] in _TREND_CATEGORIES and b["text"].strip()
    ]
    if topic:
        needle = topic.strip().lower()
        matching = [b for b in fresh if needle in b["text"].lower()]
        if matching:
            fresh = matching

    return fresh[-limit:]


def _competitor_winners(days: int, top_n: int = 5) -> list:
    videos = _videos_by_handle(days)
    niches = instagram_tools.get_competitor_niches()
    entries = []
    for handle in _competitor_accounts():
        for video in videos.get(handle, []):
            likes = _metric(video.get("raw"), "likes")
            if likes is None:
                continue
            entries.append((likes, handle, video))
    entries.sort(key=lambda item: item[0], reverse=True)

    lines = []
    for likes, handle, video in entries[:top_n]:
        niche = niches.get(handle, "Nische nicht hinterlegt")
        comments = _metric(video.get("raw"), "comments")
        extra = f", {comments} Kommentare" if comments is not None else ""
        lines.append(f"- @{handle} ({niche}): {_fmt(likes)} Likes{extra} — {video.get('url')}")
    return lines


async def bridge_trends(ai_client, topic: str = "", days: int = 14) -> str:
    """Verbindet, was gerade laeuft, mit dem jeweils passenden eigenen Account."""
    trends = _trend_blocks(days, topic)
    reports = _gather_account_evidence("", days)
    winners = _competitor_winners(days)

    parts = []

    if trends:
        parts.append("Was ich zuletzt an Trends und Erkenntnissen aufgenommen habe:")
        for block in trends:
            when = time.strftime("%d.%m.", time.localtime(block["epoch"]))
            parts.append(f"[{when}] {_trim(block['text'], 900)}")
    else:
        parts.append("Zu Trends liegt mir aus dem Zeitraum nichts Frisches vor.")

    shared = _shared_memory_lines()
    if shared:
        parts.append("")
        parts.append("Gilt fuer alle Luna-Vale-Accounts gleichermassen:")
        parts.extend(shared)

    parts.append("")
    parts.append("Stand der eigenen Accounts:")
    for report in reports:
        parts.append(f"{report['lines'][0]}  |  Ausrichtung: {_positioning(report['handle'])}")
        for line in report["lines"][1:]:
            parts.append(line)

    if winners:
        parts.append("")
        parts.append("Was bei der Konkurrenz zuletzt am besten lief:")
        parts.extend(winners)

    evidence_text = "\n".join(parts)
    fallback = (
        evidence_text
        + "\n\n(Die Verknuepfung konnte ich gerade nicht erstellen — das oben sind die reinen Beobachtungen.)"
    )

    if ai_client is None:
        return fallback

    prompt = (
        "Du bist Ahmads Assistent. Unten stehen ausschliesslich Dinge, die ich wirklich "
        "aufgezeichnet oder recherchiert habe.\n\n"
        f"{evidence_text}\n\n"
        + (f"Besonderer Fokus: {topic}\n\n" if topic else "")
        + "Verbinde die Trends mit den einzelnen Accounts. Fuer jeden Account, zu dem die Daten "
        "das hergeben, genau eine Zeile im Format:\n"
        "'@account — Trend: … | Konkret: … | Beleg: …'\n"
        "Regeln:\n"
        "- 'Konkret' ist eine Sache, die diese Woche umsetzbar waere, kein allgemeiner Ratschlag.\n"
        "- 'Beleg' nennt die Zahl oder Beobachtung von oben, auf der das fusst.\n"
        "- Passt zu einem Account kein Trend oder fehlt die Ausrichtung, schreib das offen hin, "
        "statt etwas zu erfinden.\n"
        "- Erfinde keine Zahl, keinen Beitrag und keinen Account, der oben nicht steht.\n"
        "- Keine Dateinamen, keine Werkzeugnamen, keine technischen Bezeichner.\n"
        "- Deutsch, knapp."
    )

    try:
        response = await ai_client.messages.create(
            model=_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        bridge = response.content[0].text.strip()
    except Exception as e:
        return fallback + f"\n(Grund: {e})"

    return bridge + "\n\nWoran ich das festmache:\n" + evidence_text
