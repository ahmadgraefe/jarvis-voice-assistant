"""
Jarvis V2 — Daily Content Strategy for Jerome
Compiles ONE complete, per-account content brief with REAL video links —
own top performers (Winner Tracking), competitor top performers (already-
collected video-analysis history), and fresh trend discovery via Instagram
hashtag search — then sends it to Jerome as a SINGLE message.

Built 2026-08-06 after a live incident: Ahmad asked Jarvis mid-conversation
to send Jerome a detailed multi-account strategy, and Jarvis tried to do it
ad-hoc through the live chat loop — repeatedly starting a WhatsApp message
before it had actually finished gathering real links, so Jerome got several
broken, incomplete "Hi Jerome," fragments instead of one real answer. This
module exists so that flow ALWAYS runs as one atomic gather-then-compose-
then-send pipeline, never piecemeal through live conversation turns.
"""

import asyncio
import json
import os
import re
import time

import anthropic

import instagram_tools
import jerome_comm
import memory
import sheets_tools

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-content-strategy.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-content-strategy.log")
)

# Niche-relevant hashtags per Luna Vale account, per Accounts Overview's own
# persona/niche columns (lunaxvale=dark feminine/alt-goth, cowgirllunavale=
# cowgirl/country, lunavalethegoth=cosplay/fandom despite the handle name).
# Ahmad's explicit call (2026-08-06): search WIDER, like a real person typing
# into Instagram's search bar ("goth girl", not just one compressed tag) —
# more natural phrases surface different posts than a single generic tag.
NICHE_KEYWORDS = {
    "lunaxvale": ["gothaesthetic", "altfashion", "gothgirl", "darkfeminine"],
    "cowgirllunavale": ["cowgirlaesthetic", "westernfashion", "cowgirlstyle", "countrygirl"],
    "lunavalethegoth": ["cosplaytransformation", "animecosplay", "cosplaygirl", "characterreveal"],
}
KEYWORD_POSTS_PER_TAG = 3

# Every current competitor is goth/alt-niche (Target Creator List) — only
# genuinely relevant to lunaxvale for now. Not hardcoded per-account beyond
# that because there's no cowgirl/cosplay competitor list yet; those two
# accounts lean entirely on fresh hashtag discovery until discovery_pass
# finds competitors there too. Reads config's LIVE competitor_accounts list
# (not a frozen copy) — discovery_pass/video_analysis_pass's "similar
# accounts" suggestions already grow this list over time, so competitor
# coverage widens automatically as Jarvis finds more accounts, exactly
# Ahmad's ask to also weigh "vorgeschlagene Profile".
COMPETITOR_RELEVANT_ACCOUNT = "lunaxvale"
COMPETITOR_TOP_N = 5  # Ahmad: "er soll VIEL bei unserer Konkurrenz schauen" — weigh competitors heavily


LUNA_VALE_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _get_luna_vale_knowledge() -> str:
    """Base playbook (claude_app_status.md) PLUS whatever live-Jarvis has
    learned mid-conversation and saved via REMEMBER (category=business) —
    dieselbe kleine, bewusst duplizierte Hilfsfunktion wie in
    background_brain.py/jerome_comm.py (siehe deren Docstrings), hier
    gebraucht seit 2026-08-12 fuer die neuen Feed-Video-Ideen im taeglichen
    Brief: die muessen auf den dokumentierten BEWAEHRTEN MUSTERN aufbauen,
    nicht nur auf den rohen Sheet-Zahlen."""
    try:
        with open(LUNA_VALE_STATUS_PATH, "r", encoding="utf-8") as f:
            base = f.read().strip()
    except OSError:
        base = ""
    live_updates = memory.get_category("business")
    if live_updates:
        base += f"\n\n## Live von Ahmad ergaenzt (waehrend Gespraechen)\n{live_updates}"
    return base


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


async def _gather_own_winners(account: str, top_n: int = 3) -> list:
    """Own account's best CONFIRMED performers straight from Winner Tracking
    — already-verified real data (the sheet's own Decision formula), never
    a fresh guess. Can be SPARSE for a newer account (e.g. cowgirllunavale)
    that hasn't had many videos logged yet — see _gather_own_profile_winners
    below for the direct-profile-scan complement Ahmad asked for."""
    try:
        rows = await sheets_tools.read_winner_tracking(account=account, limit=15)
    except Exception as e:
        _log(f"Fehler beim Lesen von Winner Tracking fuer {account}: {e}")
        return []
    winners = [
        r for r in rows
        if not r.get("error") and r.get("video_link") and str(r.get("decision") or "").upper() == "KEEP"
    ]
    return winners[:top_n]


OWN_PROFILE_SCAN_COUNT = 10  # deeper than the usual 6 — Ahmad: "er muss alles detailliert stoebern"


async def _gather_own_profile_winners(account: str, anthropic_client, top_n: int = 5) -> list:
    """Directly scans the account's OWN profile grid (not just Winner
    Tracking, which only has whatever was manually/automatically logged and
    can be thin for a newer account) — ranks by actual likes so real
    winners surface even if nobody ever logged them into the sheet."""
    try:
        result = await instagram_tools.analyze_recent_videos(account, anthropic_client, count=OWN_PROFILE_SCAN_COUNT)
    except Exception as e:
        _log(f"Fehler beim Profil-Scan von {account}: {e}")
        return []
    videos = [v for v in result.get("videos", []) if "raw" in v]
    videos.sort(key=lambda v: _likes_from_raw(v["raw"]), reverse=True)
    return videos[:top_n]


def _likes_from_raw(raw: str) -> int:
    match = re.search(r'likes=([\d.,]+)', raw or "")
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", "").replace(".", ""))
    except ValueError:
        return 0


def _gather_competitor_winners(handles: list, top_n: int = 3) -> list:
    """Best ALREADY-KNOWN competitor videos from the local video-analysis
    log video_analysis_pass already collected — no extra Instagram traffic
    needed for this part, just re-reading what's already on disk."""
    if not os.path.exists(instagram_tools.VIDEO_ANALYSIS_PATH):
        return []
    entries = []
    with open(instagram_tools.VIDEO_ANALYSIS_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("handle") in handles and "raw" in e:
                entries.append(e)

    entries.sort(key=lambda e: _likes_from_raw(e["raw"]), reverse=True)
    seen, top = set(), []
    for e in entries:
        if e["url"] in seen:
            continue
        seen.add(e["url"])
        top.append(e)
        if len(top) >= top_n:
            break
    return top


async def gather_daily_account_data(account: str) -> dict:
    """Taeglicher Pfad (2026-08-12, Ahmad: 'wir arbeiten mit unseren Videos
    die funktionieren... nicht mehr nach neuem Content suchen'): NUR eigene,
    bereits im Sheet bestaetigte Winner. Reine Sheet-Read, kein Playwright/
    Vision mehr -- der fruehere Live-Rescan des eigenen Profils entfaellt
    hier bewusst, seit instagram_insights_pass das Sheet zuverlaessig mit
    echten Zahlen fuellt und ein zusaetzlicher Live-Scan daneben redundant
    waere."""
    _log(f"Sammle taegliche Daten fuer {account}...")
    return {"own_winners_tracked": await _gather_own_winners(account)}


async def gather_weekly_competitor_data(account: str, anthropic_client) -> dict:
    """Woechentlicher Pfad (2026-08-12, Ahmad: 'vielleicht 1x pro Woche
    neuem Content suchen bei der Konkurrenz'): Konkurrenz-Winner + frische
    Hashtag-Trends. video_analysis_pass laeuft nicht mehr automatisch im
    Hintergrund (Kostenreduktion) -- ruft darum hier selbst einmal frisch
    instagram_tools.analyze_recent_videos() pro Konkurrenz-Account auf, bevor
    _gather_competitor_winners liest, sonst waere die Datenquelle
    (video_analysis.jsonl) dauerhaft eingefroren."""
    config = _load_config()
    _log(f"Sammle woechentliche Konkurrenz-Daten fuer {account}...")

    competitor_winners = []
    if account == COMPETITOR_RELEVANT_ACCOUNT:
        competitor_handles = config.get("competitor_accounts", [])
        for handle in competitor_handles:
            try:
                await instagram_tools.analyze_recent_videos(handle, anthropic_client, count=6)
            except Exception as e:
                _log(f"Fehler beim frischen Scan von Konkurrent @{handle}: {e}")
            await asyncio.sleep(2)
        try:
            competitor_winners = _gather_competitor_winners(competitor_handles, top_n=COMPETITOR_TOP_N)
        except Exception as e:
            # Ein einzelner Account-Fehler soll nicht die ganze woechentliche
            # Pass fuer alle Accounts kosten.
            _log(f"Fehler beim Lesen der Konkurrenz-Daten fuer {account}: {e}")
            competitor_winners = []

    trend_posts = []
    for keyword in NICHE_KEYWORDS.get(account, []):
        try:
            found = await instagram_tools.search_hashtag_top_videos(
                keyword, anthropic_client, count=KEYWORD_POSTS_PER_TAG
            )
            trend_posts.extend(found)
        except Exception as e:
            _log(f"Fehler bei Hashtag-Suche #{keyword} fuer {account}: {e}")
        await asyncio.sleep(2)

    return {"competitor_winners": competitor_winners, "trend_posts": trend_posts}


async def _get_persona_summaries() -> dict:
    """Account handle -> Persona Summary from the Accounts Overview tab —
    the actual brand-fit description (e.g. lunaxvale's 'dry, mysterious,
    confident, non-explicit' vs just 'goth'). Used to filter out trend
    posts that are merely IN the niche but don't fit the specific brand."""
    try:
        rows = await sheets_tools.read_tab("Accounts Overview")
    except Exception as e:
        _log(f"Fehler beim Lesen der Accounts Overview fuer Persona-Daten: {e}")
        return {}
    if not rows:
        return {}
    header = rows[0]
    # Loose substring match (sheets_tools._find_column), not exact .index() —
    # a wrapped/renamed header cell (happens elsewhere in this workbook,
    # e.g. Winner Tracking's multi-line headers) would otherwise silently
    # return None here and disable the brand-fit filter with NO error at all.
    handle_idx = sheets_tools._find_column(header, "account handle", "handle") or 0
    summary_idx = sheets_tools._find_column(header, "persona summary")
    if summary_idx is None:
        _log("WARNUNG: Spalte 'Persona Summary' nicht gefunden — Marken-Filter laeuft ohne Persona-Daten.")
        return {}
    return {
        row[handle_idx]: row[summary_idx]
        for row in rows[1:] if len(row) > max(handle_idx, summary_idx) and row[handle_idx]
    }


async def build_daily_content_brief() -> dict:
    """Gathers real data for EVERY Luna Vale account -- taeglicher Pfad, nur
    Sheet-Winner (siehe gather_daily_account_data). Returns a dict keyed by
    account handle — ein Account ohne Daten bekommt einfach eine leere Liste,
    nie erfundene Fuellsel.

    Each account is isolated: an unexpected failure on ONE (Instagram
    hiccup, malformed data) still lets the other accounts' real data reach
    Jerome instead of losing the whole day's brief over one account."""
    config = _load_config()
    accounts = config.get("luna_vale_accounts", [])
    account_data = {}
    for account in accounts:
        try:
            account_data[account] = await gather_daily_account_data(account)
        except Exception as e:
            _log(f"Fehler beim Sammeln der taeglichen Daten fuer {account} — Account uebersprungen: {e}")
            account_data[account] = {"own_winners_tracked": []}
    return account_data


async def _compose_and_send_brief(anthropic_client, prompt_text: str, log_label: str) -> str:
    """Gemeinsamer Kern fuer taeglichen und woechentlichen Content-Brief: EIN
    Claude-Call, parsen, EINE WhatsApp-Nachricht senden, ins Wissen und in
    die Daily Production List eintragen. Gather-then-compose-then-send in
    genau dieser Reihenfolge, nie piecemeal — das war der urspruengliche
    Fehler (siehe Modul-Docstring)."""
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1600,
            messages=[{"role": "user", "content": prompt_text}],
        )
    except Exception as e:
        # Both callers have their own outer safety nets (server.py's
        # execute_action try/except, background_brain's *_pass try/except +
        # Retry), so nothing crashes — aber ohne das hier waere die Meldung
        # eine rohe Python-Exception statt einer klaren, und die schon
        # gesammelten echten Daten wuerden stillschweigend verworfen statt
        # als "gesammelt, aber nicht komponiert" gemeldet.
        _log(f"FEHLER beim Komponieren ({log_label}): {e}")
        return f"ERROR: Daten gesammelt, aber Nachricht konnte nicht komponiert werden: {e}"

    raw = response.content[0].text.strip()
    message_body, production_rows = _parse_brief_response(raw)
    if not message_body:
        _log(f"FEHLER ({log_label}): Konnte WhatsApp-Abschnitt nicht aus der Antwort extrahieren: {raw[:300]!r}")
        return "ERROR: Daten gesammelt, aber Nachricht konnte nicht aus der Antwort extrahiert werden."

    result = await jerome_comm.send_raw_message(message_body)
    _log(f"{log_label} gesendet: {result}")

    # "Das Gehirn erweitern" (Ahmad's own words) — this research shouldn't
    # just fire-and-forget into a WhatsApp message and vanish. Folding a
    # compact version into the knowledge base means future briefs, live
    # conversation, and daily summaries can all draw on what was found today.
    if not result.startswith("ERROR"):
        memory.add_knowledge(
            f"{log_label} ({time.strftime('%Y-%m-%d')}):\n{message_body}",
            category="content-strategy",
        )

        # Ahmad (2026-08-07): the Daily Production List tab sat empty — the
        # brief went out as a WhatsApp wall of text but was never tracked as
        # actual per-video tasks. Same data, same selection, just also
        # written as structured rows Jerome/Ahmad can check off.
        today = time.strftime("%Y-%m-%d")
        for row in production_rows:
            try:
                await sheets_tools.add_daily_production_row({**row, "date": today})
            except Exception as e:
                _log(f"FEHLER beim Eintragen in Daily Production List ({row.get('account')}): {e}")

    return result


async def send_daily_content_brief(anthropic_client) -> str:
    """The full pipeline: gather ALL data first, THEN compose ONE message,
    THEN send it ONCE. Taeglicher Pfad seit 2026-08-12 (Ahmad: 'wir arbeiten
    mit unseren Videos die funktionieren... nicht mehr nach neuem Content
    suchen') nur noch mit den eigenen, im Sheet bestaetigten Winnern.

    Zwei Auftragsarten seit 2026-08-12 (Ahmad: 'Jerome soll auch nicht nur
    Trials machen sondern auch fuer den normalen Feed' — 'du bzw Jarvis
    entscheidet wie was wann gemacht werden soll'): Trial Reels (Variation
    EINES bestehenden Winners, unveraendert) UND neue Feed Videos (neue,
    eigenstaendige Konzepte auf einem der dokumentierten bewaehrten Muster,
    siehe claude_app_status.md Kernregeln) — zusammen in EINER taeglichen
    Nachricht, Mischung/Menge entscheidet Claude je Account selbst anhand
    der echten Datenlage, genau das an Jarvis delegierte 'entscheide du'.
    Zielgroesse ist Jeromes reale Kapazitaet von ca. 8-10 Videos PRO TAG
    INSGESAMT ueber alle aktiven Accounts zusammen (Ahmad, 2026-08-12) —
    NICHT pro Account, siehe claude_app_status.md Kernregeln."""
    account_data = await build_daily_content_brief()
    personas = await _get_persona_summaries()
    knowledge = _get_luna_vale_knowledge()

    has_anything = any(d["own_winners_tracked"] for d in account_data.values())
    if not has_anything:
        _log("Keine bestaetigten eigenen Winner im Sheet gefunden, keine Nachricht gesendet.")
        return "ERROR: Keine bestaetigten eigenen Winner im Sheet gefunden — keine Nachricht an Jerome geschickt."

    payload = json.dumps(account_data, ensure_ascii=False, indent=2)
    persona_block = json.dumps(personas, ensure_ascii=False, indent=2)
    prompt = (
        "Bewaehrte Business-Patterns (Kernregeln + bestaetigte starke Muster aus dem "
        f"Tracking-Wissen — Grundlage fuer NEUE Feed-Video-Ideen unten):\n\n{knowledge[:3000]}\n\n"
        "Hier ist die MARKEN-DEFINITION pro Account (Persona Summary aus dem Tracking-"
        f"Sheet — das ist der Massstab fuer 'passt zur Marke'):\n\n{persona_block}\n\n"
        "Hier sind Ahmads EIGENE, im Sheet bereits als KEEP bestaetigte Gewinner-Videos pro "
        f"Luna-Vale-Account (own_winners_tracked, aus dem Google Sheet, echte Zahlen):\n\n{payload}\n\n"
        "AUFGABE — ZWEI verschiedene Auftragsarten, Zielgroesse INSGESAMT ueber ALLE Accounts "
        "zusammen ca. 8-10 Videos pro Tag (Trial Reels + Feed Videos kombiniert) — das ist "
        "Jeromes reale Tageskapazitaet als Editor, NICHT 8-10 pro einzelnem Account. Verteile "
        "die 8-10 nach Datenlage: Accounts mit mehr/staerkeren bestaetigten Winnern bekommen "
        "mehr Auftraege, schwache oder datenlose Accounts weniger oder in dem Zyklus auch mal "
        "keinen. Lieber insgesamt unter 8-10 bleiben als Videos oder Links erfinden, die die "
        "echten Daten nicht hergeben.\n\n"
        "WICHTIG ZUM FORMAT: denk die Verteilung/Auswahl STILL fuer dich durch, schreib sie "
        "NICHT als eigenen Analyse-Abschnitt aus. Deine Antwort beginnt UNMITTELBAR mit der "
        "Zeile '===WHATSAPP===' — keine Ueberschrift, keine Zusammenfassung, kein Vorspann "
        "davor.\n\n"
        "1. TRIAL REELS: aus einem guten Winner-Video EINE konkrete VARIANTE ableiten, kein "
        "neues Konzept. GENAU EINE Variable aendern (z.B. anderes Outfit, anderer Ort/Setting, "
        "anderer Einstiegssatz/Hook-Wortlaut, anderer Sound) — der Rest bleibt wie im bewaehrten "
        "Original, das ist der ganze Sinn eines Trial Reels: das Bewaehrte gezielt variieren "
        "statt komplett neu zu raten.\n\n"
        "2. FEED VIDEOS: NEUE, eigenstaendige Videoideen fuer den normalen Feed — kein Trial "
        "Reel eines einzelnen Videos, sondern ein komplett neues Konzept, gebaut auf einem der "
        "oben dokumentierten BEWAEHRTEN MUSTER (z.B. Debatten-Hook, Outfit-Transition, Comedy-"
        "auf-der-Buehne-mit-starkem-Outfit — je nachdem was fuer den Account/die Marke passt). "
        "Nimm als Referenz das own_winners_tracked-Video, das dieses Muster am klarsten schon "
        "bewiesen hat (fuer den LINK im Production-Block), aber die Idee selbst muss neu sein, "
        "nicht nur eine Variable am Original geaendert.\n\n"
        "Schreib daraus ZWEI Dinge, in GENAU diesem Format (zwei Abschnitte mit den "
        "exakten Markern, sonst nichts drumherum):\n\n"
        "===WHATSAPP===\n"
        "EINE vollstaendige WhatsApp-Nachricht an Jerome, auf ENGLISCH (er spricht kein "
        "Deutsch). Struktur: kurze Begruessung, dann PRO ACCOUNT (nur wenn fuer diesen Account "
        "ueberhaupt etwas vorhanden ist — Account komplett weglassen wenn nichts da ist) eine "
        "kurze Ueberschrift mit dem Account-Handle, darunter GETRENNT die zwei Unterabschnitte "
        "'Trial Reels' und 'Feed Videos' (einen Unterabschnitt weglassen wenn dafuer nichts "
        "Passendes gefunden wurde). Fuer JEDE Trial-Reel-Empfehlung GENAU dieses 4-Punkte-"
        "Format als kompakter Block (keine langen Absaetze):\n"
        "1. Original: kurzer Verweis auf das bewaehrte Original-Video (Link)\n"
        "2. What to change: GENAU EINE Variable, klar benannt\n"
        "3. Why it should still work: warum die Variation den bewaehrten Kern nicht verliert\n"
        "4. Link: der EXAKTE Original-Link aus den Rohdaten (NIEMALS einen Link erfinden, "
        "aendern oder auslassen)\n"
        "Fuer JEDE Feed-Video-Idee GENAU dieses 4-Punkte-Format:\n"
        "1. Concept: die neue Videoidee in 1-2 Saetzen, konkret genug zum Drehen\n"
        "2. Proven pattern: welches dokumentierte Muster das ist und warum es bei diesem "
        "Account nachweislich funktioniert\n"
        "3. Reference: kurzer Verweis auf das eigene KEEP-Video, das dieses Muster schon bewiesen hat\n"
        "4. Link: der EXAKTE Link dieses Referenz-Winners aus den Rohdaten (NIEMALS erfinden)\n"
        "Nur der fertige Nachrichtentext, keine Meta-Kommentare, KEINE eigene Signatur am Ende "
        "(wird automatisch ergaenzt).\n\n"
        "===PRODUCTION===\n"
        "Fuer JEDEN Punkt aus der WhatsApp-Nachricht oben (Trial Reels UND Feed Videos "
        "zusammen, gleiche Auswahl, gleiche Reihenfolge) EINEN Block in GENAU diesem Format — "
        "bewusst KEIN JSON, damit Anfuehrungszeichen in Zitaten/Hooks nichts kaputt machen:\n"
        "---VIDEO---\n"
        "ACCOUNT: <handle ohne @>\n"
        "TYPE: Trial Reel ODER Feed Video — je nachdem was dieser Block ist\n"
        "LINK: <der exakte Link (Original bei Trial Reel, Referenz-Winner bei Feed Video), "
        "identisch zum WhatsApp-Abschnitt>\n"
        "HOOK: <die Kernidee/der Hook in einem Satz, Englisch>\n"
        "OUTFIT: <konkreter Vorschlag Outfit/Setting fuer die Umsetzung, Englisch — leer "
        "lassen (Zeile trotzdem schreiben, nur ohne Text danach) wenn nicht die geaenderte "
        "Variable ist und aus den Rohdaten nicht klar erkennbar>\n"
        "SOUND: <Sound/Audio-Hinweis falls aus den Rohdaten erkennbar, sonst leer>\n"
        "INSTRUCTION: <EINE klare, direkte Anweisung an Jerome in einfachem Englisch, was "
        "genau zu tun ist — bei Trial Reel: was geaendert wird, bei Feed Video: was gedreht wird>\n"
        "CAPTION: <ein vorgeschlagener finaler Caption-Text, Englisch>\n"
        "---END---"
    )
    return await _compose_and_send_brief(anthropic_client, prompt, "Taeglicher Content-Brief")


async def build_weekly_competitor_brief(anthropic_client) -> dict:
    """Woechentliches Gegenstueck zu build_daily_content_brief -- Konkurrenz-
    Winner + frische Hashtag-Trends statt eigener Sheet-Winner (2026-08-12,
    Ahmad: 'vielleicht 1x pro Woche neuem Content suchen bei der Konkurrenz')."""
    config = _load_config()
    accounts = config.get("luna_vale_accounts", [])
    account_data = {}
    for account in accounts:
        try:
            account_data[account] = await gather_weekly_competitor_data(account, anthropic_client)
        except Exception as e:
            _log(f"Fehler beim Sammeln der woechentlichen Konkurrenz-Daten fuer {account} — Account uebersprungen: {e}")
            account_data[account] = {"competitor_winners": [], "trend_posts": []}
    return account_data


async def send_weekly_competitor_brief(anthropic_client) -> str:
    """Woechentliches Gegenstueck zu send_daily_content_brief. Ausdruecklich
    als INSPIRATION gekennzeichnet, nicht als Ahmads eigene bestaetigte
    Winner — damit in Jeromes Kopf (und im Sheet) nichts vermischt wird."""
    account_data = await build_weekly_competitor_brief(anthropic_client)
    personas = await _get_persona_summaries()

    has_anything = any(d["competitor_winners"] or d["trend_posts"] for d in account_data.values())
    if not has_anything:
        _log("Keine verwertbaren Konkurrenz-/Trend-Daten gefunden, keine woechentliche Nachricht gesendet.")
        return "ERROR: Keine verwertbaren Konkurrenz-/Trend-Daten gefunden — keine Nachricht an Jerome geschickt."

    payload = json.dumps(account_data, ensure_ascii=False, indent=2)
    persona_block = json.dumps(personas, ensure_ascii=False, indent=2)
    prompt = (
        "Hier ist die MARKEN-DEFINITION pro Account (Persona Summary aus dem Tracking-"
        f"Sheet — das ist der Massstab fuer 'passt zur Marke', NICHT nur 'ist im richtigen "
        f"Genre'):\n\n{persona_block}\n\n"
        "Hier sind FREMDE Videos pro Luna-Vale-Account (competitor_winners = bekannte starke "
        "Konkurrenz-Videos, trend_posts = frisch per Hashtag-Suche gefundene aktuelle Top-"
        f"Posts) — das sind KEINE eigenen bestaetigten Winner, sondern Inspiration von "
        f"aussen:\n\n{payload}\n\n"
        "WICHTIG — Marken-Filter zuerst: Ahmads ausdruecklicher Wunsch ist, dass NUR "
        "Videos empfohlen werden, die WIRKLICH zur jeweiligen Marken-Persona passen, "
        "nicht einfach alles was im Hashtag gut performt. Ein Video kann gute Zahlen "
        "haben und trotzdem NICHT passen — so etwas AUSSORTIEREN, auch wenn die Zahlen "
        "verlockend sind. Priorisiere competitor_winners hoeher als generische trend_posts, "
        "wenn beide vorhanden sind — Ahmad will dass viel auf die Konkurrenz geschaut wird. "
        "Bevorzuge Videos mit: starkem Hook in den ersten 1-2 Sekunden, klarer "
        "Transformation/Kontrast (z.B. soft girl -> full goth), Outfit-Wechsel/Transitions, "
        "hoher Energie/Bewegung, dark-feminine/selbstbewusst-mysterioeser Ton (v.a. bei "
        "lunaxvale), Comedy/Street-Interview-Punchlines, und sichtbaren US/englischsprachigen "
        "Signalen (us_signals-Feld). SORTIERE AUS: niedrige Qualitaet/schlechte Beleuchtung "
        "(quality-Feld), Content der nur in nicht-englischsprachigen Maerkten funktioniert, "
        "und alles was nicht wirklich zur jeweiligen Nische passt.\n\n"
        "Schreib daraus ZWEI Dinge, in GENAU diesem Format (zwei Abschnitte mit den "
        "exakten Markern, sonst nichts drumherum):\n\n"
        "===WHATSAPP===\n"
        "EINE vollstaendige WhatsApp-Nachricht an Jerome, auf ENGLISCH (er spricht kein "
        "Deutsch), klar als woechentliche Konkurrenz-/Trend-Inspiration eingeleitet (nicht als "
        "Ahmads eigene Winner). Struktur: kurze Begruessung, dann PRO ACCOUNT (nur wenn fuer "
        "diesen Account nach dem Filter wirklich passende Daten uebrig bleiben — Account "
        "komplett weglassen wenn nichts passt) eine kurze Ueberschrift mit dem Account-Handle. "
        "Fuer JEDES empfohlene Video GENAU dieses 4-Punkte-Format als kompakter Block:\n"
        "1. Concept: kurze Beschreibung worum es geht\n"
        "2. Why it fits: warum es zu diesem Account/dieser Marke passt\n"
        "3. Key elements: was macht es funktionieren (Hook/Struktur/Text/Energie)\n"
        "4. Link: der EXAKTE Link aus den Rohdaten (NIEMALS einen Link erfinden, aendern "
        "oder auslassen)\n"
        "Maximal 2-3 Videos pro Account — Qualitaet vor Quantitaet. Nur der fertige "
        "Nachrichtentext, keine Meta-Kommentare, KEINE eigene Signatur am Ende (wird "
        "automatisch ergaenzt).\n\n"
        "===PRODUCTION===\n"
        "Fuer JEDES Video aus der WhatsApp-Nachricht oben (gleiche Auswahl, gleiche "
        "Reihenfolge) EINEN Block in GENAU diesem Format — bewusst KEIN JSON, damit "
        "Anfuehrungszeichen in Zitaten/Hooks nichts kaputt machen:\n"
        "---VIDEO---\n"
        "ACCOUNT: <handle ohne @>\n"
        "TYPE: Inspiration\n"
        "LINK: <der exakte Link, identisch zum WhatsApp-Abschnitt>\n"
        "HOOK: <die Kernidee/der Hook in einem Satz, Englisch>\n"
        "OUTFIT: <konkreter Vorschlag Outfit/Setting fuer die Umsetzung, Englisch — leer "
        "lassen (Zeile trotzdem schreiben, nur ohne Text danach) wenn aus den Rohdaten "
        "nicht klar erkennbar>\n"
        "SOUND: <Sound/Audio-Hinweis falls aus den Rohdaten erkennbar, sonst leer>\n"
        "INSTRUCTION: <EINE klare, direkte Anweisung an Jerome in einfachem Englisch, was "
        "genau zu tun ist>\n"
        "CAPTION: <ein vorgeschlagener finaler Caption-Text, Englisch>\n"
        "---END---"
    )
    return await _compose_and_send_brief(anthropic_client, prompt, "Woechentliche Konkurrenz-Inspiration")


def _parse_brief_response(raw: str) -> tuple:
    """Splits the two marked sections and parses the PRODUCTION block with
    plain 'LABEL: value' lines instead of JSON — deliberately, after a real
    incident (2026-08-07) where a Vision response's quoted hook text broke
    strict JSON parsing. Returns (whatsapp_message, [row_dict, ...])."""
    whatsapp_match = re.search(r'===WHATSAPP===\s*(.*?)\s*===PRODUCTION===', raw, re.DOTALL)
    message_body = whatsapp_match.group(1).strip() if whatsapp_match else ""

    production_match = re.search(r'===PRODUCTION===\s*(.*)', raw, re.DOTALL)
    production_text = production_match.group(1) if production_match else ""

    rows = []
    for block in re.findall(r'---VIDEO---\s*(.*?)\s*---END---', production_text, re.DOTALL):
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            label, _, value = line.partition(":")
            fields[label.strip().upper()] = value.strip()
        if not fields.get("LINK"):
            continue
        rows.append({
            "account": fields.get("ACCOUNT", ""),
            "video_type": fields.get("TYPE", "Normal"),
            "reference_link": fields.get("LINK", ""),
            "hook_idea": fields.get("HOOK", ""),
            "outfit_setting": fields.get("OUTFIT", ""),
            "sound": fields.get("SOUND", ""),
            "instruction": fields.get("INSTRUCTION", ""),
            "caption": fields.get("CAPTION", ""),
        })
    return message_body, rows
