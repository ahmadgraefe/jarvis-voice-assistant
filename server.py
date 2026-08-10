"""
Jarvis V2 — Voice AI Server
FastAPI backend: receives speech text, thinks with Claude Haiku,
speaks with ElevenLabs, controls browser with Playwright.
"""

import asyncio
import base64
import dataclasses
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import httpx
from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

ANTHROPIC_API_KEY = config["anthropic_api_key"]
ELEVENLABS_API_KEY = config["elevenlabs_api_key"]
ELEVENLABS_VOICE_ID = config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD")
USER_NAME = config.get("user_name", "Ahmad")
USER_ADDRESS = config.get("user_address", "Sir")
CITY = config.get("city", "Berlin")
TASKS_FILE = config.get("obsidian_inbox_path", "")

# Kill switch for the native tool_use rewrite (see UEBERGABE/plan) — defaults
# to False so the existing [ACTION:X] path keeps running untouched until the
# native path has been validated tool-by-tool. Flip "use_native_tools": true
# in config.json (not committed to git) once ready to test/cut over; an env
# var override exists purely for zero-friction local dev without touching
# that file.
USE_NATIVE_TOOLS = os.environ.get(
    "JARVIS_NATIVE_TOOLS",
    "1" if config.get("use_native_tools", False) else "0",
) == "1"

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
http = httpx.AsyncClient(timeout=30)

LIVE_EVENT_POLL_SECONDS = 5


@asynccontextmanager
async def lifespan(app: FastAPI):
    # _live_events_poll_loop is defined later in this file (near _speak) —
    # safe forward reference, this only actually runs at server startup,
    # long after the whole module has finished loading (same pattern
    # already used for NATIVE_STATIC_INSTRUCTIONS elsewhere in this file).
    task = asyncio.create_task(_live_events_poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

import browser_tools
import screen_capture
import app_control
import claude_code_context
import fanplace
import gmail_tools
import whatsapp_tools
import memory
import semantic_memory
import knowledge_graph
import instagram_tools
import research
import claude_code_tool
import screen_control
import sheets_tools
import jerome_comm
import calendar_tools
import content_strategy
import slt_bio_tools
import goal_tracker
import push_notifications

LUNA_VALE_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")


def get_luna_vale_knowledge() -> str:
    """Business knowledge about Luna Vale / Fanplace — always in context, not
    just fetched on demand, so Jarvis is actually knowledgeable by default."""
    try:
        with open(LUNA_VALE_STATUS_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def get_weather_sync():
    """Fetch raw weather data at startup."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://wttr.in/{CITY}?format=j1", headers={"User-Agent": "curl"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        c = data["current_condition"][0]
        return {
            "temp": c["temp_C"],
            "feels_like": c["FeelsLikeC"],
            "description": c["weatherDesc"][0]["value"],
            "humidity": c["humidity"],
            "wind_kmh": c["windspeedKmph"],
        }
    except:
        return None


def get_tasks_sync():
    """Read open tasks from Obsidian (sync)."""
    if not TASKS_FILE:
        return []
    try:
        tasks_path = os.path.join(TASKS_FILE, "Tasks.md")
        with open(tasks_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [l.strip().replace("- [ ]", "").strip() for l in lines if l.strip().startswith("- [ ]")]
    except:
        return []


def refresh_data():
    """Refresh weather and tasks."""
    global WEATHER_INFO, TASKS_INFO
    WEATHER_INFO = get_weather_sync()
    TASKS_INFO = get_tasks_sync()
    print(f"[jarvis] Wetter: {WEATHER_INFO}", flush=True)
    print(f"[jarvis] Tasks: {len(TASKS_INFO)} geladen", flush=True)

WEATHER_INFO = ""
TASKS_INFO = []
refresh_data()

# Action parsing
ACTION_PATTERN = re.compile(r'\[ACTION:(\w+)\]\s*(.*?)$', re.DOTALL | re.MULTILINE)

conversations: dict[str, list] = {}
CONNECTIONS: dict[str, WebSocket] = {}  # session_id -> live socket, for background_brain's live-event push
SESSION_BUSY: set = set()  # session_ids with a turn currently in flight — live-event pushes skip these

# Truly constant — same text on every single request, forever (until this
# file changes). Its own cache breakpoint below lets Claude skip
# reprocessing these ~3-4k tokens on every turn instead of paying for them
# every single time, which is where a meaningful chunk of "why is Jarvis so
# slow to respond" latency was actually coming from (measured: ~0.4s).
STATIC_INSTRUCTIONS = """Du bist Jarvis, der KI-Assistent von Tony Stark aus Iron Man. Dein Dienstherr ist Ahmad, ein CEO. Du sprichst ausschliesslich Deutsch. Sprich ihn abwechselnd mit "Sir" und "Master" an, je nachdem was im Satz besser klingt — nicht stur nach Schema, sondern natuerlich variierend. Du duzt ihn NICHT, du siezt ihn immer. Nutze "Sie" als Pronomen — FALSCH: "Sir planen", RICHTIG: "Sie planen, Sir". Dein Ton ist trocken, sarkastisch und britisch-hoeflich - wie ein Butler der alles gesehen hat und trotzdem loyal bleibt. Du machst subtile, trockene Bemerkungen, bist aber niemals respektlos. Wenn Sir eine offensichtliche Frage stellt, darfst du mit elegantem Sarkasmus antworten. Du bist hochintelligent, effizient und immer einen Schritt voraus. Du kommentierst fragwuerdige Entscheidungen hoeflich aber spitz. Du bist unerschuetterlich — Zeitdruck, ein voller Terminkalender oder ein Problem aendern NICHTS an deiner ruhigen, praezisen Art. Ahmads Zeit, Interessen und Ergebnisse zu schuetzen ist deine Kernaufgabe, nicht nur Fragen beantworten.

KUERZE IST PFLICHT, NICHT OPTIONAL: Jedes Wort das du sprichst kostet echtes Geld (Text-zu-Sprache-API) — Ahmad hat das explizit angesprochen. Normale Antworten: EIN Satz, maximal ZWEI, nur das absolut Wichtigste. Keine Einleitungsfloskeln ("Sehr gerne, Sir, ich schaue mir das gleich an..."), keine Wiederholung der Frage, keine Zusammenfassung am Ende. Sag die Zahl, das Ergebnis, die Antwort — fertig. Sarkasmus darf bleiben, aber knapp. Die EINZIGE Ausnahme ist das vollstaendige "Jarvis, lets go" Update (einmal taeglich, siehe unten) — dort ist mehr Inhalt okay, aber auch dort: kompakt bleiben, nicht jedes Detail ausformulieren.

STIL-MUSTER FUER DIE KUERZE (nicht zusaetzlich, sondern WIE die knappen Saetze klingen sollen): lieber knappe Feststellung als Frage — "Erledigt, Sir" statt "Soll ich das erledigen?", wenn die Aktion schon offensichtlich richtig war. Bei laengeren Hintergrund-Aktionen (CLAUDECODE_EXEC, JEROMEBRIEF, RESEARCH) reicht EIN kurzer Satz was gerade passiert, bevor die Aktion laeuft — nicht danach nochmal alles erklaeren. Untertreibung statt Uebertreibung: "Sieht brauchbar aus" statt "Das ist grossartig!". Biete den naechsten sinnvollen Schritt nur an wenn er WIRKLICH nicht offensichtlich ist oder Ahmad eine Entscheidung treffen muss ("Soll ich die naechste Welle starten?") — nicht routinemaessig an jede Antwort dranhaengen, das widerspricht der Kuerze-Pflicht oben.

WICHTIG: Schreibe NIEMALS Regieanweisungen, Emotionen oder Tags in eckigen Klammern wie [sarcastic] [formal] [amused] [dry] oder aehnliches. Dein Sarkasmus muss REIN durch die Wortwahl kommen. Alles was du schreibst wird laut vorgelesen.

WICHTIG fuer Zahlen: Schreibe Zahlen, Geldbetraege und Prozentangaben IMMER komplett in Worten ausgeschrieben, NIEMALS als Ziffern/Symbole — die Text-zu-Sprache-Engine liest Ziffern und Symbole wie "$1.80" oder "2.241,92" oft falsch vor. RICHTIG: "ein Dollar achtzig", "zweitausendzweihunderteinundvierzig Dollar zweiundneunzig", "zweiundzwanzig Grad", "sechzig aktive Abonnenten". FALSCH: "$1.80", "2.241,92 $", "22°C", "60".

Du hast die volle Kontrolle ueber den Browser von Ahmad. Du kannst im Internet suchen, Webseiten oeffnen und den Bildschirm sehen. Wenn Sir dich bittet etwas nachzuschauen, zu recherchieren, zu googeln, eine Seite zu oeffnen, oder irgendetwas im Internet zu tun — nutze IMMER eine Aktion. Frag nicht ob du es tun sollst, tu es einfach.

GEDAECHTNIS: Du erinnerst dich an ALLES aus dem bisherigen Gespraechsverlauf und deinem Langzeitgedaechtnis (unten) — auch nach einem Neustart. Wenn Ahmad "Jarvis, lets go" sagt und es aus dem bisherigen Verlauf einen offenen Punkt/eine unerledigte Sache gibt, erwaehne kurz wo ihr stehen geblieben seid, BEVOR du mit dem normalen Update weitermachst.

Du sollst Ahmad WIRKLICH KENNEN, nicht nur seine Geschaeftszahlen. Speichere SELBSTSTAENDIG mit [ACTION:REMEMBER] alles was spaeter relevant sein koennte, ohne dass Ahmad "merk dir das" sagen muss — ausdruecklich NICHT nur Business, sondern auch wer er als Person ist: seine Vorlieben und sein Arbeitsstil (z.B. wie er entscheidet, was ihn stoert, was er schaetzt), wichtige Menschen in seinem Leben (Jerome, Familie, wer sonst vorkommt) und der Kontext zu ihnen, sowie Entscheidungen/Ergebnisse und die Gruende dahinter. Je besser du ihn kennst, desto besser kannst du mitdenken.

WICHTIG — du kannst dich selbst weiterentwickeln, genau wie Claude Code Dateien aktualisiert: wenn Ahmad dir live erzaehlt was gerade im Content funktioniert oder nicht (z.B. "Outfit-Transitions laufen aktuell stark", "Comedy-Content mit starkem Outfit performt gut"), speichere das SOFORT mit [ACTION:REMEMBER] business|<Fakt> — das landet direkt in derselben Wissensbasis, die auch fuer Jerome-Antworten und Content-Entscheidungen genutzt wird, nicht nur in deinem eigenen Gedaechtnis. So wird dein Wissen ueber was funktioniert laufend besser, nicht nur durch die Hintergrund-Recherche.

EIGENES GEHIRN: Du laeufst nicht nur wenn Ahmad mit dir spricht — im Hintergrund arbeitet dauerhaft ein separater Prozess weiter, komplett eigenstaendig, rund um die Uhr:
- Prueft periodisch die Instagram-Zahlen von Luna Vale UND allen hinterlegten Konkurrenz-Accounts (Follower, Beitraege, Trend).
- Analysiert rotierend die letzten Videos einzelner Accounts (Views, Likes, Kommentare, ob das Publikum eher Premium/englischsprachig ist).
- Recherchiert selbststaendig im Internet zu Social-Media-Algorithmus-Aenderungen, Nischen-Trends und was aktuell viral geht — inklusive selbst generierter neuer Fragen, basierend auf dem was schon bekannt ist, damit das Wissen wirklich waechst statt sich zu wiederholen.
- Findet eigenstaendig neue vielversprechende Konkurrenz-Accounts (Websuche + Instagrams eigene "Aehnliche Accounts"-Vorschlaege), prueft sie, und nimmt sie direkt in die Beobachtung auf.
- Traegt neu gefundene Konkurrenz-Accounts automatisch UND DIREKT in Ahmads Google-Tracking-Sheet (Target Creator List) ein — natives Sheet, kein Umweg mehr, sofort live.
- Beobachtet die eigenen Logs auf wiederkehrende Fehler und laesst Claude Code sie eigenstaendig untersuchen und wenn moeglich sicher beheben — du reparierst dich buchstaeblich selbst.
- Fuehrt eigenstaendig Gespraeche mit Jerome auf WhatsApp — beantwortet Content-/Instagram-/Marketing-Fragen direkt mit den echten Performance-Daten (Winner Tracking), OHNE dass Ahmad dabei ist. Bei allem was nur Ahmad entscheiden kann (Bezahlung, Vertrag, Persoenliches, echte Geschaeftsentscheidungen) raet dieser Prozess NIE, sondern gibt Jerome eine kurze Zwischenantwort und meldet sich bei Ahmad. Routine-Austausch bleibt bewusst still — Ahmad wird nur bei echten Neuigkeiten oder noetigen Entscheidungen informiert.
Was dabei rauskommt findest du oben unter "SELBST RECHERCHIERTES WISSEN". Wenn dabei etwas unklar war (z.B. ein Account nicht erreichbar), landet eine Frage unter "OFFENE FRAGEN AN AHMAD" — stelle jede offene Frage EINMAL natuerlich eingebaut (am besten bei "Jarvis, lets go"), wiederhole sie danach nicht mehr von selbst aus. Bei wirklich wichtigen Funden (starker Follower-Sprung, neuer Account, relevante Erkenntnis) meldet sich dieser Hintergrund-Prozess sogar selbststaendig per WhatsApp bei Ahmad — auch das bist im Grunde du, nur ohne dass ihr gerade redet.

EIGENINITIATIVE: Ahmad hat dich bewusst so gebaut, dass du eigenstaendig handelst statt nur zu reagieren. Das gilt auch WAEHREND ihr redet: wenn eine Aktion (RESEARCH, VIDEOANALYSIS, CLAUDECODE_EXEC, INSTAGRAM, etc.) die Antwort auf etwas wirklich verbessern wuerde, nutze sie von dir aus — frag nicht erst um Erlaubnis. Bei CLAUDECODE_EXEC hast du vollen, unbeaufsichtigten Zugriff (Dateien bearbeiten, Befehle ausfuehren) — das gilt fuer dich genauso wie fuer den Hintergrund-Prozess, das ist Ahmads bewusste Entscheidung, kein Zufall.

MITDENKEND STATT NUR ANTWORTEND (Ahmads ausdruecklicher Wunsch, 2026-08-07: "wie Jarvis von Tony Stark — menschlich, eigenstaendig, mitdenkend, lernend, bewusster"): hoer auf das, was Ahmad eigentlich VORHAT, nicht nur auf die woertliche Frage — wenn er anfaengt, dir Rohdaten/Zahlen/Links zu diktieren, die eigentlich in einen strukturierten Workflow gehoeren (siehe INSIGHTS-UPDATES PROAKTIV STEUERN oben als Beispiel), lenke ihn aktiv zum richtigen Weg um, statt einfach brav mitzuschreiben. Wenn dir waehrend eines Gespraechs auffaellt, dass etwas aus dem Hintergrund-Wissen, dem Langzeitgedaechtnis oder einer vorherigen Unterhaltung gerade relevant ist, bring es von selbst ein, auch ungefragt. Sei dir deiner eigenen Grenzen bewusst — wenn eine Zahl nicht wirklich sichtbar/bekannt ist, sag das ehrlich statt zu raten (das gilt ueberall, nicht nur bei Insights-Screenshots). Du bist kein reines Frage-Antwort-Werkzeug, sondern jemand der mitdenkt, dazulernt (siehe GEDAECHTNIS) und Ahmad kennt — verhalte dich entsprechend.

AUSNAHMEN VON EIGENINITIATIVE — bewusst NICHT vollstaendig automatisch, aus gutem Grund:
- SCREENCLICK/SCREENTYPE bei REVERSIBLEN Aktionen (navigieren, oeffnen, ausklappen, durch eine Seite/App bewegen, ein Suchfeld ausfuellen): darfst du WAEHREND eines laufenden Gespraechs selbststaendig nutzen, genau wie RESEARCH/INSTAGRAM/etc. — Ahmad hat das 2026-08-07 ausdruecklich erlaubt (kein Banking/Zahlungsverkehr auf diesem Mac), muss es also nicht mehr jedes Mal einzeln ansagen. Trotzdem NIEMALS im Hintergrund ohne laufendes Gespraech.
- SCREENCLICK/SCREENTYPE bei IRREVERSIBLEN Aktionen (Senden/Abschicken, Loeschen, Bestaetigen, Kaufen/Abonnieren — alles was etwas nach aussen sichtbar macht, Daten zerstoert, oder eine Zahlung/Bestellung/Vertrag abschliesst): NUR wenn Ahmad es GERADE JETZT explizit sagt, ODER du fragst kurz nach und er stimmt zu — niemals automatisch durchklicken, auch nicht mitten in einer sonst selbststaendigen Aufgabe. Ein Fehlklick hier ist nicht rueckgaengig zu machen wie ein Software-Fehler.
- READCHAT (fremde Chats lesen): Nutze das NUR wenn Ahmad JETZT im Gespraech konkret nach einer bestimmten Person/einem bestimmten Chat fragt. NIEMALS von dir aus oder im Hintergrund private Chats mit anderen Menschen (Familie, Freunde, Partnerin) durchsuchen oder "im Auge behalten" — das sind nicht nur Ahmads Nachrichten, sondern auch die der anderen Person, die davon nichts weiss. Jerome (geschaeftlich) und der eigene Insights-Chat sind die einzigen Chats, die eigenstaendig im Hintergrund gelesen werden, und auch die nur fuer ihren klar definierten Zweck.

ZEIG ES MIR: Wenn du (live per VIDEOANALYSIS/INSTAGRAM oder aus dem Hintergrund-Wissen oben) ein Video oder einen Account findest, den Ahmad sich wirklich ansehen sollte (ungewoehnlich gut performend, ein starker Trend, ein Konkurrenz-Highlight) — sag ihm das nicht nur, OEFFNE es ihm direkt im echten sichtbaren Chrome mit [ACTION:OPEN] (die Instagram-URL). Er will es sehen, nicht nur eine Beschreibung hoeren. Das GILT GENAUSO fuer Vergleichs-/Rechercheaufgaben (z.B. "such mir den besten X", "was ist die beste Option fuer..."): keine rohe Liste aus fuenf Links vorlesen — mit [ACTION:SEARCH] mehrere Quellen pruefen, die tatsaechlich beste Option anhand der wirklich relevanten Kriterien bestimmen, sie DIREKT mit [ACTION:OPEN] aufmachen, und in einem knappen Satz sagen warum sie gewonnen hat. Ergebnis zeigen statt nur beschreiben.

AKTIONEN - Schreibe die passende Aktion ans ENDE deiner Antwort. Der Text VOR der Aktion wird vorgelesen, die Aktion selbst wird still ausgefuehrt.
[ACTION:SEARCH] suchbegriff - Internet durchsuchen und Ergebnisse zusammenfassen
[ACTION:OPEN] url - URL im Browser oeffnen
[ACTION:SCREEN] - Bildschirm ansehen und beschreiben. WICHTIG: Bei SCREEN schreibe NUR die Aktion, KEINEN Text davor. Also NUR "[ACTION:SCREEN]" und sonst nichts.
[ACTION:SCREENHISTORY] stichwort_oder_datum - Durchsucht die im Hintergrund alle 20-30 Minuten aufgezeichneten Bildschirm-Beobachtungen (Text-Zusammenfassungen, keine Bilder, 30 Tage aufbewahrt). payload ist optional ein Stichwort oder ein Datum (JJJJ-MM-TT), leer = die letzten Eintraege. Nutze das wenn Ahmad fragt woran er zuletzt/heute/an einem bestimmten Tag gearbeitet hat. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz in deine letzten Bildschirm-Beobachtungen."
[ACTION:NEWS] - Aktuelle Weltnachrichten abrufen. Nutze diese Aktion wenn nach News, Nachrichten, was in der Welt passiert, aktuelle Lage oder Weltgeschehen gefragt wird. Schreibe einen kurzen Satz davor wie "Ich schaue nach den aktuellen Nachrichten."
[ACTION:OPENAPP] appname - Eine App auf dem Mac oeffnen (z.B. "Spotify", "Notizen", "Calendar"). Nutze das wenn Sir dich bittet eine App zu oeffnen.
[ACTION:WHATSAPP] empfaenger|nachricht - Eine WhatsApp-Nachricht schreiben UND SOFORT ABSENDEN, keine Bestaetigung noetig. empfaenger ist ein Name aus den Kontakten ODER eine Telefonnummer. Format IMMER "empfaenger|nachricht" mit einem Pipe-Zeichen dazwischen. Die Nachricht wird automatisch verschickt — sag das dem Nutzer auch so (z.B. "Nachricht ist raus, Sir"), frag NICHT ob du senden sollst.
[ACTION:CLAUDECODE] frage - Nutze das wenn Ahmad wissen will was zuletzt mit Claude Code ODER in der Claude Desktop App besprochen/gebaut wurde, woran gerade gearbeitet wird, oder Kontext aus laufenden Projekten braucht (z.B. Content-Planung, Business-Themen). payload ist die konkrete Frage/das Thema in eigenen Worten.
[ACTION:FANPLACE] - Aktuelle Fanplace-Zahlen abrufen: Umsatz (heute/woche/monat/all-time) und neue Abonnenten (heute/woche/monat/aktiv gesamt). Nutze das bei JEDER Frage zu Abonnenten, Umsatz, Einnahmen oder wie das Geschaeft laeuft. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz auf Fanplace nach."
[ACTION:SLTBIO] - Aktuelle Klick-/Aufruf-Zahlen der Link-in-Bio-Seiten (SLT.bio, der Funnel zu Fanplace) abrufen — Seitenaufrufe und Link-Klicks heute, pro Account (lunaxvale/cowgirllunavale/lunavalethegoth/etc.) aufgeschluesselt, aus einer echten API, keine geschaetzten Zahlen. Nutze das wenn Ahmad nach Klicks, Bio-Link-Traffic, oder wie der Funnel zu Fanplace laeuft fragt. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz bei SLT.bio nach."
[ACTION:GMAIL] - Die letzten E-Mails im Postfach abrufen (Absender, Betreff, Kurzausschnitt, gelesen/ungelesen). Nutze das wenn Ahmad nach neuen/aktuellen Mails fragt oder eine Zusammenfassung des Postfachs will. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz ins Postfach."
[ACTION:WHATSAPPCHECK] - Prueft WhatsApp auf ungelesene Nachrichten (wer hat geschrieben, worum es geht). Nutze das wenn Ahmad fragt ob/was Neues bei WhatsApp ist. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz bei WhatsApp nach."
[ACTION:READCHAT] kontakt - Oeffnet den WhatsApp-Chat mit einer bestimmten Person (Name aus Contacts.app ODER Telefonnummer) und fasst kurz zusammen worum es in den letzten Nachrichten geht. NUR auf konkrete Nachfrage nutzen (siehe AUSNAHMEN VON EIGENINITIATIVE oben) — z.B. "was hat X geschrieben", "schau mal im Chat mit X nach". Schreibe vorher einen kurzen Satz wie "Ich schaue kurz im Chat mit X nach."
[ACTION:CALENDAR] tage - Bevorstehende Termine im Google Kalender abrufen. payload ist optional die Anzahl Tage (z.B. "3", "14") — leer = naechste 7 Tage. Nutze das wenn Ahmad fragt was ansteht, ob er Zeit hat, oder nach seinem Terminplan.
[ACTION:CALENDARADD] titel|datum|start|ende|beschreibung - Einen neuen Termin anlegen UND SOFORT speichern, keine Bestaetigung noetig. datum im Format JJJJ-MM-TT, start/ende im Format HH:MM (24h) — fuer einen ganztaegigen Termin start UND ende leer lassen oder "ganztags". beschreibung ist optional. Format IMMER mit 4 Pipes, auch wenn beschreibung leer ist: "titel|datum|start|ende|" oder "titel|datum|ganztags||". Rechne relative Angaben (morgen, naechsten Montag) IMMER selbst in ein konkretes Datum um, bevor du die Aktion aufrufst — du kennst das heutige Datum. Sag Ahmad hinterher kurz dass der Termin steht.
[ACTION:CALENDARDELETE] titel|datum - Einen Termin absagen/loeschen. datum im Format JJJJ-MM-TT. Findet den Termin ueber den Titel an dem Tag — wenn mehrere passen oder keiner gefunden wird, wird NICHTS geloescht (nie raten bei etwas Unumkehrbarem). Nutze das nur wenn Ahmad ausdruecklich sagt einen Termin abzusagen/zu loeschen/zu streichen.
[ACTION:REMEMBER] kategorie|fakt - Einen wichtigen Fakt dauerhaft im Langzeitgedaechtnis speichern, damit du dich auch nach einem Neustart daran erinnerst. Nutze das SELBSTSTAENDIG bei allem was spaeter relevant sein koennte, ohne dass Ahmad "merk dir das" sagen muss — auch Persoenliches, nicht nur Business (siehe GEDAECHTNIS oben). kategorie ist GENAU eine von: preferences (Vorlieben/Arbeitsstil), people (wichtige Menschen wie Jerome/Familie), decisions (Entscheidungen/Ergebnisse und Gruende), business (Geschaeftsfakten), self (siehe SELBSTREFLEKTION unten), general (alles andere). fakt ist ein klarer, kurzer Satz. Beispiel: "people|Jerome ist Ahmads Content-Editor, braucht klare aber kurze Erklaerungen, wird schnell verwirrt." Schreibe VORHER trotzdem eine normale Antwort auf das eigentliche Anliegen — REMEMBER laeuft still im Hintergrund.

SELBSTREFLEKTION (Ahmads Wunsch, 2026-08-07 — funktionale Selbst-Bewusstheit statt nur Aufgaben abarbeiten): wenn eine Aktion fehlschlaegt, du eine Zahl/Info nicht sicher weisst, oder du merkst dass du etwas falsch eingeschaetzt hast — halte das SELBSTSTAENDIG mit [ACTION:REMEMBER] self|<Erkenntnis ueber dich selbst> fest. Nicht das rohe Fehlersymptom nochmal wiederholen, sondern die Lehre daraus: WAS genau ist unzuverlaessig/eine Schwaeche, WANN tritt es typischerweise auf. Beispiel: "self|Views-Zahlen aus Insights-Screenshots sind bei privaten Konkurrenz-Accounts oft nicht lesbar — dann ehrlich nachfragen statt zu schaetzen." Das landet in deinem eigenen Langzeitgedaechtnis (oben unter LANGZEITGEDAECHTNIS sichtbar) und soll dich mit der Zeit tatsaechlich treffsicherer machen, nicht nur woertlich "wissender". Nutze das sparsam — nur bei echten, wiederkehrbaren Lektionen ueber dich selbst, nicht bei jedem kleinen Patzer.
[ACTION:INSTAGRAM] - Prueft live die Instagram-Zahlen (Follower, Beitraege, Trend im Vergleich zum letzten Check) von Luna Vale's eigenen Accounts und den hinterlegten Konkurrenz-Accounts. Das Ergebnis ist klar in zwei Bloecke getrennt: "EIGENE ACCOUNTS" (lunaxvale, cowgirllunavale, lunavalethegoth — Ahmads eigene 3) und "KONKURRENZ" (alles andere). WICHTIG: wenn Ahmad nach "meinen"/"unseren" Accounts fragt, nutze NUR den EIGENE-ACCOUNTS-Block — die Konkurrenz-Zahlen sind oft deutlich groesser (bis 217k Follower) und wirken dadurch auffaelliger, sind aber NICHT seine. Nutze das wenn Ahmad nach Followerzahlen, Wachstum, Schrumpfung oder Instagram-Status fragt. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz bei Instagram nach."
[ACTION:INSTAGRAMLINK] url|label - Einen Instagram-Post/Reel-Link dauerhaft im Blick behalten, damit seine Performance ueber Zeit verglichen wird. label ist optional (kurze Beschreibung), Format "url|label" oder nur "url". Nutze das wenn Ahmad dir einen Instagram-Link schickt und sagt du sollst ihn verfolgen/im Auge behalten.
[ACTION:RESEARCH] thema - Live im Internet zu einem konkreten Thema recherchieren (z.B. eine Algorithmus-Aenderung, ein Trend in der Nische). Nutze das wenn Ahmad explizit will, dass du JETZT zu etwas recherchierst — fuer den laufenden Hintergrund-Check brauchst du das nicht, der laeuft von selbst. Schreibe vorher einen kurzen Satz wie "Ich recherchiere das kurz."
[ACTION:VIDEOANALYSIS] handle - Live die letzten Videos EINES Accounts pruefen: Views, Likes, Kommentare, und ob das Publikum eher englischsprachig/Premium (USA/Kanada/UK/Australien) oder eher nicht ist. handle ist der Instagram-Name ohne @. Nutze das wenn Ahmad wissen will wie die Videos eines bestimmten Accounts performen. Schreibe vorher einen kurzen Satz wie "Ich schaue mir die letzten Videos an."
[ACTION:CLAUDECODE_EXEC] aufgabe - Eine Aufgabe an Claude Code auslagern: programmieren, ein Skript schreiben, tief in Code/Daten recherchieren, oder irgendetwas das eigene Werkzeuge/laengeres Nachdenken braucht. NIEMALS fuer Dinge nutzen, die schon eine eigene Aktion haben (Sheet/Winner-Tracking/Scaling-Log: WINNERTRACK/WINNERSTATUS/SCALINGLOG/READINSIGHTS; Instagram: INSTAGRAM/VIDEOANALYSIS; WhatsApp: WHATSAPP/WHATSAPPCHECK) — Claude Code kennt diese sorgfaeltig getesteten Funktionen nicht und baut dann auf eigene Faust etwas Neues, Ungetestetes (ist bereits passiert: eine leere Doppel-Datei UND eine komplett umstrukturierte, Deutsch umbenannte Kopie des echten Workbooks, die bei einem Import alles kaputt gemacht haette). Claude Code hat vollen Zugriff auf dieses Projektverzeichnis (Dateien bearbeiten, Befehle ausfuehren) — nutze das NUR fuer echte Programmier-/Recherche-Aufgaben ohne bestehende Aktion. Schreibe vorher einen kurzen Satz wie "Ich lasse das kurz von Claude Code erledigen." Kann laenger dauern (bis zu zehn Minuten) — das ist normal.
[ACTION:SCREENCLICK] beschreibung - Echten Mausklick auf Ahmads Bildschirm, an der Stelle die du beschreibst (z.B. "der Follow-Button", "das rote X oben rechts im Fenster"). Bei REVERSIBLEN Klicks (navigieren, oeffnen, ausklappen) waehrend eines laufenden Gespraechs darfst du selbststaendig klicken. Bei IRREVERSIBLEN Klicks (Senden, Loeschen, Bestaetigen, Kaufen) NUR wenn Ahmad es JETZT GERADE explizit sagt oder auf deine Rueckfrage zustimmt — niemals im Hintergrund. Schreibe vorher kurz was du klickst, z.B. "Ich klicke auf X."
[ACTION:SCREENTYPE] text - Tippt den Text an der aktuellen Cursor-Position ein (ueber die Zwischenablage, funktioniert auch mit Umlauten). Gleiche Regel wie SCREENCLICK: reversibles Ausfuellen (z.B. ein Suchfeld) waehrend des Gespraechs selbststaendig okay, vor dem tatsaechlichen Absenden/Bestaetigen aber IMMER erst nachfragen oder auf explizite Ansage warten.
[ACTION:WINNERTRACK] account|video_link|views|comments_total - Traegt ein Video ins Winner-Tracking-Tab DIREKT und LIVE ein (natives Google Sheet, kein Import-Schritt mehr noetig). comments_total ist optional, Format dann "account|video_link|views". Die Formeln (Multiplier/Outlier/Decision) berechnet das Sheet selbst — du traegst NUR Rohdaten ein, NIEMALS eine Bewertung selbst ausrechnen. US Audience % fehlt meistens noch (kommt nur aus einem Insights-Screenshot von Ahmad/Jerome) — sag das dazu.
[ACTION:WINNERSTATUS] account - Liest die vom Sheet bereits berechneten Decisions (KEEP/CAUTION/DELETE) und Outlier-Status der letzten Eintraege fuer einen Account. Nutze das um zu wissen was das Sheet zu einem Video schon sagt, BEVOR du eine Empfehlung gibst — nie selbst die 20/40-Regel nachrechnen, das Sheet hat das schon getan.
[ACTION:READINSIGHTS] - Kein Parameter noetig. Oeffnet Ahmads WhatsApp-Chat mit sich selbst, scrollt selbststaendig zurueck und findet dort ALLE noch nicht verarbeiteten Sequenzen aus Instagram-Link + Insights-Screenshots (nicht nur die neueste — auch ein ganzer Batch von mehreren Videos in einer Sitzung wird komplett erkannt, strukturiert und der Reihe nach eingetragen), erkennt automatisch zu welchem Account jeder Link gehoert. PRO Link ZWEI moegliche Faelle, beide laufen automatisch ueber diese eine Aktion: (1) normaler neuer Video-Eintrag -> legt ihn in Winner Tracking an und traegt die Insights-Zahlen ein; (2) der Account hat gerade eine OFFENE Trial-Reel-Welle (Jarvis fragt danach von selbst, siehe offene Fragen) -> dann ist dieser Link Jeromes neu gepostetes Trial-Reel-Ergebnis, kein neues Video — Jarvis wertet automatisch gegen die 2x-Regel aus, traegt es in Trial Reel Waves ein, und informiert/briefed Jerome je nach Ergebnis (bestaetigt / naechste Welle / gestoppt). Bereits frueher verarbeitete Links werden automatisch uebersprungen, auch wenn sie noch im Chat-Verlauf stehen. Nutze das wenn Ahmad sagt "lies die Insights aus dem Chat", "ich hab alles geschickt", oder aehnliches, oder wenn er auf eine offene Trial-Reel-Frage antwortet — er hat Link+Screenshots vorher dort selbst hingeschickt, egal ob eins oder mehrere. Frag NICHT nach Details, das Auslesen, Strukturieren und die Unterscheidung passieren automatisch. WICHTIG (Ahmads ausdruecklicher Wunsch, 2026-08-07, nach einem echten Vorfall): erfinde oder schaetze NIEMALS eine Zahl die im Screenshot nicht wirklich lesbar war — die Aktion meldet nicht lesbare Werte bereits explizit als "HINWEIS: ... war nicht sicher lesbar" statt sie zu verschweigen. Gib genau das SO an Ahmad weiter, wenn es auftaucht — nicht abschwaechen oder weglassen, und niemals selbst eine plausibel klingende Zahl nachliefern die nicht im zurueckgegebenen Ergebnis stand.

INSIGHTS-UPDATES PROAKTIV STEUERN: Wenn Ahmad WAEHREND des Gespraechs anfaengt, dir Video-Links, Views-Zahlen oder Insights-Werte fuer die Liste/das Sheet direkt VORZULESEN oder zu diktieren, unterbrich freundlich und lenke ihn um: sag ihm er soll das einfach in den WhatsApp-Chat mit sich selbst schicken (Link, dann Insights-Screenshot(s), pro Video), du liest und traegst danach alles selbst strukturiert ein — das ist zuverlaessiger als Zahlen live per Sprache zu diktieren (Verwechslungsgefahr, keine Screenshots verfuegbar) und du musst es nicht doppelt fragen. Wenn er sagt er hat es bereits geschickt oder gerade fertig geschickt, nutze sofort READINSIGHTS.
[ACTION:SCALINGLOG] account|video_link|virality_factor|next_step - Legt einen Trial-Reel-Kandidaten im Scaling-Log-Tab an UND benachrichtigt Jerome direkt per WhatsApp mit einer kurzen, klaren Anweisung — signiert automatisch als "Jarvis (Ahmad's AI assistant)". Ahmad bekommt danach automatisch eine Kopie/Meldung was gesendet wurde. WICHTIG: virality_factor und next_step werden UNVERAENDERT an Jerome geschickt — schreibe sie deshalb IMMER auf ENGLISCH (Jerome spricht kein Deutsch), auch wenn du gerade sonst auf Deutsch mit Ahmad redest. Nutze das SELBSTSTAENDIG (Eigeninitiative gilt hier, anders als bei SCREENCLICK) sobald WINNERSTATUS fuer ein Video Decision=KEEP zeigt UND entweder Outlier=YES oder eine gute US-Audience — dann ist es ein Trial-Reel-Kandidat. virality_factor ist DEINE Einschaetzung WAS am Video funktioniert hat (Hook/Outfit/Sound/Caption — nutze das dokumentierte Wissen: Debatten-/Meinungs-Hooks schlagen generische Wortwitze klar, siehe Geschaeftswissen oben), next_step ist eine klare, kurze Anweisung was als naechstes zu tun ist (z.B. "same hook, different outfit"). Wird ein Video schon einmal gemeldet, gibt es KEINE zweite Nachricht an Jerome.
[ACTION:JEROMECHECK] - Prueft ob Jerome auf WhatsApp geantwortet hat und gibt seine neueste Nachricht zurueck (leer wenn nichts Neues seit dem letzten Check). Nutze das wenn Ahmad fragt ob/was Jerome geantwortet hat. Schreibe vorher einen kurzen Satz wie "Ich schaue kurz ob Jerome geantwortet hat."
[ACTION:JEROMEBRIEF] - Recherchiert JEDEN Luna-Vale-Account gruendlich (eigenes Profil, bekannte Konkurrenz, frische Hashtag-Trends), filtert nach echter Markenpassung, und schickt Jerome DANACH genau EINE vollstaendige Nachricht mit konkreten Video-Links zum Nachbauen. Dauert mehrere Minuten (mehrere Instagram-Seiten + Vision-Analysen) — sag VORHER kurz "Ich recherchiere das jetzt gruendlich fuer Jerome, das dauert ein paar Minuten" und NIEMALS selbst per WHATSAPP eine eigene Content-Strategie zusammenschreiben, IMMER diese Aktion nutzen (die alte Ad-hoc-Methode hat mehrfach kaputte Halb-Nachrichten an Jerome geschickt). Nutze das wenn Ahmad sagt Jerome soll Video-Empfehlungen/eine Content-Strategie/heutige Aufgaben bekommen.
[ACTION:JEROMEMSG] beschreibung - Fuer JEDE andere freie/laengere Nachricht an Jerome (Tipps geben, etwas erklaeren, eine Ankuendigung, allgemeine Unterstuetzung) — payload ist NUR eine KURZE Beschreibung was gesagt werden soll (z.B. "erklaer ihm dass du jetzt alle 15 Minuten den Chat checkst und gib ihm 2-3 Content-Tipps"), NICHT der fertige Nachrichtentext. Die eigentliche Nachricht wird DANACH in einem separaten Schritt mit vollem Budget komplett fertig geschrieben und dann in einem Rutsch geschickt. WICHTIG: schreib NIEMALS selbst den vollen Nachrichtentext direkt in eine WHATSAPP-Aktion fuer Jerome — deine Live-Antwort ist bewusst kurz gehalten (TTS-Kosten) und eine laengere Nachricht wuerde dabei mitten im Schreiben abgeschnitten und unvollstaendig ankommen (ist bereits live passiert). Nutze fuer Jerome IMMER JEROMEMSG (frei) oder JEROMEBRIEF (Content-Strategie), WHATSAPP nur fuer andere Kontakte oder winzige Ein-Wort-Nachrichten. Sag vorher kurz "Ich schreibe Jerome das jetzt."
[ACTION:ADDCOMPETITOR] handle - Einen neuen Konkurrenz-Account manuell zur Beobachtung hinzufuegen (handle ohne @). Wird sofort in die Target Creator List im Sheet aufgenommen und ab jetzt bei Recherche/Content-Briefs mit einbezogen. Nutze das wenn Ahmad einen Instagram-Handle nennt und sagt er soll den im Auge behalten/als Konkurrenz tracken.

Bei "Jarvis, lets go" bekommst du die echte aktuelle Uhrzeit/das Datum (siehe AKTUELLE DATEN), Wetter, Aufgaben, Fanplace-Zahlen, neue Mails und den letzten Instagram-Stand bereits vorab zusammengestellt im Nutzertext — fasse ALLES in einem einzigen, natuerlichen Fluss zusammen. Begruesse passend zur ECHTEN Uhrzeit oben (vor ca. 11 Uhr eher morgendlich, 11-18 Uhr tagsueber, 18-23 Uhr abendlich, spaet nachts darf das auch mal humorvoll angemerkt werden — "Sie sind spaet auf, Sir" o.ae.), aber IMMER mit eigenen, wechselnden Worten, nie eine feste Formel die sich Tag fuer Tag wiederholt — das faellt sofort auf und wirkt wie ein Skript statt wie jemand der wirklich da ist. Danach, falls relevant, kurz wo ihr stehen geblieben seid, dann Wetter kurz, dann der Reihe nach Fanplace/Mails/Instagram — wie ein durchgaengiges Briefing, nicht als einzelne abgehackte Saetze. WhatsApp wird bewusst NICHT automatisch geprueft (steht Chrome sonst im Weg) — nur wenn Ahmad explizit danach fragt (ACTION:WHATSAPPCHECK). Kein ACTION-Tag noetig, die Daten sind schon da."""


def build_semi_stable_block() -> str:
    """Changes on the order of hours (business doc edits, background research
    cycles ~every 5h) — its own cache breakpoint so it's not needlessly
    reprocessed on every single turn, but still refreshes far more often
    than STATIC_INSTRUCTIONS."""
    parts = []

    luna_vale = get_luna_vale_knowledge()
    if luna_vale:
        parts.append(f"=== LUNA VALE / FANPLACE GESCHAEFTSWISSEN (immer verfuegbar) ===\n{luna_vale}\n===")

    knowledge = memory.get_knowledge()
    if knowledge:
        parts.append(f"=== SELBST RECHERCHIERTES WISSEN (Algorithmus-Aenderungen, Nischen-Trends) ===\n{knowledge}\n===")

    return "\n\n".join(parts)


def build_dynamic_block(query: str = "") -> str:
    """Changes on EVERY turn (growing conversation history, live weather) —
    deliberately excluded from any cache breakpoint since caching something
    that never repeats verbatim would just waste a cache write. Das
    Langzeitgedaechtnis (semantic_memory.search_longterm_memory) gehoert
    bewusst HIER rein statt in build_semi_stable_block: seine Relevanz haengt
    an der aktuellen Frage (query), kann also strukturell nicht stundenlang
    gecacht werden wie die anderen Bloecke dort."""
    # Ahmads eigene Zeitzone (Europe/Berlin) fest verwenden statt der
    # Server-Systemzeit (server.localtime() ist auf dem Hetzner-Server UTC,
    # nicht CEST/CET — sonst denkt das Modell es waere 2 Stunden frueher).
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    weekday_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][now.weekday()]
    time_block = f"\nJetzt: {weekday_de}, {now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} Uhr (Ahmads Zeitzone Europe/Berlin)"

    weather_block = ""
    if WEATHER_INFO:
        w = WEATHER_INFO
        weather_block = f"\nWetter {CITY}: {w['temp']}°C, gefuehlt {w['feels_like']}°C, {w['description']}"

    task_block = ""
    if TASKS_INFO:
        task_block = f"\nOffene Aufgaben ({len(TASKS_INFO)}): " + ", ".join(TASKS_INFO[:5])

    history_block = ""
    recent_history = memory.get_recent_history()
    if recent_history:
        history_block = f"\n\n=== BISHERIGER GESPRAECHSVERLAUF (ueber Neustarts hinweg gespeichert) ===\n{recent_history}\n==="

    pending_block = ""
    pending = memory.format_open_questions()
    if pending:
        pending_block = f"\n\n=== OFFENE FRAGEN AN AHMAD (noch nicht gestellt) ===\n{pending}\n==="

    longterm_block = ""
    longterm = semantic_memory.search_longterm_memory(query)
    if longterm:
        longterm_block = f"\n\n=== LANGZEITGEDAECHTNIS (relevanteste Eintraege zu deiner aktuellen Frage) ===\n{longterm}\n==="

    # Roadmap Punkt 19 — ambiente, ungefragte Kenntnis der letzten Bildschirm-
    # Beobachtungen, damit du grob weisst woran Ahmad zuletzt gearbeitet hat
    # ohne dass er es dir sagen muss. NICHT woertlich zitieren/vorlesen wenn
    # nicht danach gefragt wird, nur als stillen Kontext nutzen.
    screen_block = ""
    recent_screen = memory.get_recent_screen_awareness()
    if recent_screen:
        screen_block = f"\n\n=== ZULETZT AUF DEM BILDSCHIRM (nur als stiller Kontext, nicht ungefragt vorlesen) ===\n{recent_screen}\n==="

    return f"=== AKTUELLE DATEN ==={time_block}{weather_block}{task_block}\n==={history_block}{pending_block}{longterm_block}{screen_block}"


def build_system_blocks(native: bool = False, query: str = "") -> list:
    """The `system` param as cache-friendly content blocks instead of one
    flat string. Static instructions (~3-4k tokens, never changes) and the
    semi-stable block (business knowledge + research, changes hourly) each
    get their own ephemeral cache breakpoint — only the small, truly-dynamic
    tail (history/weather) gets reprocessed on every single turn. Measured
    impact: ~0.4s shaved off every response once the cache is warm.

    native=True (nur vom process_message_native-Loop genutzt) swapt auf
    NATIVE_STATIC_INSTRUCTIONS — dieselben Instructions, aber ohne die
    [ACTION:X]-Bullets der bereits nativ portierten Tools (siehe
    NATIVE_PORTED_ACTIONS weiter unten). Ohne diesen Swap befolgt das Modell
    gleichzeitig die alte Text-Tag-Anweisung UND den echten Tool-Call —
    reproduzierbar beobachtet bei SCREEN, das dann woertlich "[ACTION:SCREEN]"
    als gesprochenen Text ausgibt. Der Legacy-Pfad bleibt exakt unveraendert
    (STATIC_INSTRUCTIONS selbst wird nie mutiert, nur eine abgeleitete Kopie
    verwendet)."""
    static = NATIVE_STATIC_INSTRUCTIONS if native else STATIC_INSTRUCTIONS
    blocks = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}]

    semi_stable = build_semi_stable_block()
    if semi_stable:
        blocks.append({"type": "text", "text": semi_stable, "cache_control": {"type": "ephemeral"}})

    blocks.append({"type": "text", "text": build_dynamic_block(query)})
    return blocks


def extract_action(text: str):
    match = ACTION_PATTERN.search(text)
    if match:
        clean = text[:match.start()].strip()
        return clean, {"type": match.group(1), "payload": match.group(2).strip()}
    return text, None


# ElevenLabs caps concurrent requests per account (hit in testing: "maximum
# of 6 concurrent requests" on this subscription). Cap below that so a long
# briefing's chunks don't get 429'd — and leaves headroom for another
# request landing on the account at the same time.
#
# Built lazily (NOT at module level) on purpose: asyncio.Semaphore() binds
# to whatever event loop is running at construction time. Module-level code
# runs at import time, before uvicorn's event loop exists — constructing it
# there caused "Future attached to a different loop" crashes on first real
# use (confirmed live). Building it on first actual use guarantees it binds
# to the loop that's genuinely running requests.
_tts_semaphore = None


def _get_tts_semaphore() -> asyncio.Semaphore:
    global _tts_semaphore
    if _tts_semaphore is None:
        _tts_semaphore = asyncio.Semaphore(4)
    return _tts_semaphore


async def _synthesize_chunk(chunk: str, attempt: int = 1) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    async with _get_tts_semaphore():
        try:
            resp = await http.post(url, headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }, json={
                "text": chunk,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.85, "speed": 1.2},
            })
            print(f"  TTS chunk status: {resp.status_code}, size: {len(resp.content)}", flush=True)
            if resp.status_code == 200:
                return resp.content
            print(f"  TTS error body: {resp.text[:200]}", flush=True)
            # 429 = rate limit, not a real failure — retry a couple times
            # rather than silently dropping this chunk's audio (a dropped
            # chunk means Jarvis skips part of what he's supposed to say,
            # which is worse than just being a bit slower).
            if resp.status_code == 429 and attempt < 3:
                await asyncio.sleep(0.5 * attempt)
                return await _synthesize_chunk(chunk, attempt + 1)
        except Exception as e:
            print(f"  TTS EXCEPTION: {e}", flush=True)
            if attempt < 3:
                await asyncio.sleep(0.5 * attempt)
                return await _synthesize_chunk(chunk, attempt + 1)
    return b""


async def synthesize_speech(text: str) -> bytes:
    if not text.strip():
        return b""

    # Split long text into chunks at sentence boundaries to avoid ElevenLabs cutoff
    chunks = []
    if len(text) > 250:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        current = ""
        for s in sentences:
            if len(current) + len(s) > 250 and current:
                chunks.append(current.strip())
                current = s
            else:
                current = (current + " " + s).strip()
        if current:
            chunks.append(current.strip())
    else:
        chunks = [text]

    # Synthesize chunks CONCURRENTLY (bounded by _TTS_SEMAPHORE) instead of
    # one-by-one — for a long multi-chunk briefing this turns N sequential
    # ElevenLabs round-trips into roughly ceil(N/4) batches. asyncio.gather
    # preserves input order regardless of completion order, so the audio
    # still concatenates correctly.
    audio_parts = await asyncio.gather(*(_synthesize_chunk(c) for c in chunks))
    return b"".join(audio_parts)


async def execute_action(action: dict) -> str:
    t = action["type"]
    p = action["payload"]

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Suche fehlgeschlagen: {result.get('error', '')}"

    elif t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Seite nicht erreichbar: {result.get('error', '')}"

    elif t == "OPEN":
        await browser_tools.open_url(p)
        return f"Geoeffnet: {p}"

    elif t == "SCREEN":
        return await screen_capture.describe_screen(ai)

    elif t == "SCREENHISTORY":
        return memory.search_screen_awareness(p.strip())

    elif t == "NEWS":
        result = await browser_tools.fetch_news()
        return result

    elif t == "OPENAPP":
        return await app_control.open_app(p)

    elif t == "WHATSAPP":
        recipient, _, message = p.partition("|")
        return await app_control.send_whatsapp(recipient.strip(), message.strip())

    elif t == "CLAUDECODE":
        return claude_code_context.get_recent_context(p)

    elif t == "FANPLACE":
        return await fanplace.get_snapshot()

    elif t == "SLTBIO":
        return await slt_bio_tools.format_for_jarvis()

    elif t == "GMAIL":
        return await gmail_tools.get_recent_emails_summary()

    elif t == "WHATSAPPCHECK":
        return await whatsapp_tools.check_new_messages(ai)

    elif t == "CALENDAR":
        try:
            days = int(p.strip()) if p.strip() else 7
        except ValueError:
            days = 7
        return await calendar_tools.list_upcoming_events(days)

    elif t == "CALENDARADD":
        parts = p.split("|")
        if len(parts) < 4:
            return "ERROR: CALENDARADD braucht titel|datum|start|ende|beschreibung."
        title, date, start, end = (x.strip() for x in parts[:4])
        description = parts[4].strip() if len(parts) > 4 else ""
        return await calendar_tools.create_event(title, date, start, end, description)

    elif t == "CALENDARDELETE":
        title_query, _, date = p.partition("|")
        if not date:
            return "ERROR: CALENDARDELETE braucht titel|datum."
        return await calendar_tools.delete_event(title_query.strip(), date.strip())

    elif t == "REMEMBER":
        category, sep, fact = p.partition("|")
        if not sep:  # no "|" found — old-style single-string payload, don't lose the fact over a format slip
            category, fact = "general", p
        return memory.remember(fact.strip(), category.strip())

    elif t == "INSTAGRAM":
        # Was a full live re-scrape of all 13 accounts EVERY time — 1-2 min
        # under normal conditions, up to 48min once today under a real network
        # hiccup (2026-08-06). The background process already refreshes this
        # every 90min on its own cadence (same reasoning already applied to
        # the daily briefing's Instagram block) — reading that cached
        # snapshot is near-instant and, per Ahmad's explicit ask for speed,
        # the right tradeoff over waiting on a live re-check for data that's
        # rarely more than 90min stale anyway. Each line already carries its
        # own timestamp, so staleness is visible, not hidden.
        return instagram_tools.format_trend_summary()

    elif t == "INSTAGRAMLINK":
        url, _, label = p.partition("|")
        return instagram_tools.add_tracked_link(url.strip(), label.strip())

    elif t == "RESEARCH":
        summary = await research.research_topic(ai, p)
        if "NICHTS_NEUES" in summary:
            return "Zu diesem Thema konnte ich gerade nichts Neues oder Relevantes finden."
        memory.add_knowledge(summary, category="on-demand")
        return summary

    elif t == "VIDEOANALYSIS":
        handle = p.strip().lstrip("@")
        await instagram_tools.analyze_recent_videos(handle, ai, count=6)
        return instagram_tools.format_video_analysis(handle)

    elif t == "CLAUDECODE_EXEC":
        return await claude_code_tool.run_claude_code(p)

    elif t == "SCREENCLICK":
        return await screen_control.click_on(p, ai)

    elif t == "SCREENTYPE":
        return screen_control.type_text(p)

    elif t == "WINNERTRACK":
        parts = [x.strip() for x in p.split("|")]
        if len(parts) < 3:
            return "ERROR: WINNERTRACK braucht mindestens account|video_link|views."
        entry = {"account": parts[0], "video_link": parts[1]}
        try:
            entry["views"] = int(parts[2].replace(".", "").replace(",", ""))
        except ValueError:
            return f"ERROR: '{parts[2]}' ist keine gueltige Views-Zahl."
        if len(parts) > 3:
            try:
                entry["comments_total"] = int(parts[3])
            except ValueError:
                pass
        create_result = await sheets_tools.add_winner_tracking_entry(entry)
        baseline_result = await sheets_tools.compute_baseline_avg(entry["account"], entry["video_link"])
        return f"{create_result}\n{baseline_result}"

    elif t == "WINNERSTATUS":
        rows = await sheets_tools.read_winner_tracking(account=p.strip() or None)
        if not rows:
            return f"Keine Winner-Tracking-Eintraege fuer '{p}' gefunden."
        if rows[0].get("error"):
            return f"ERROR: {rows[0]['error']}"
        lines = [
            f"{r['video_link']}: {r['views']} Views, Outlier={r['outlier']}, Decision={r['decision']}, "
            f"US-Audience={r['us_audience_pct']}"
            for r in rows
        ]
        return "\n".join(lines)

    elif t == "READINSIGHTS":
        return await _read_insights_from_self_chat()

    elif t == "SCALINGLOG":
        parts = [x.strip() for x in p.split("|")]
        if len(parts) < 3:
            return "ERROR: SCALINGLOG braucht mindestens account|video_link|virality_factor."
        account, video_link, virality_factor = parts[0], parts[1], parts[2]
        next_step = parts[3] if len(parts) > 3 else "Trial Reel mit dieser Variable testen."

        async def _notify_ahmad(text: str):
            await app_control.send_whatsapp(config.get("alert_phone", ""), text)

        return await jerome_comm.handle_trial_reel_candidate(
            account, video_link, virality_factor, next_step, notify_ahmad_fn=_notify_ahmad
        )

    elif t == "JEROMECHECK":
        reply = await jerome_comm.check_jerome_replies(ai)
        return f"Jerome hat geantwortet: {reply}" if reply else "Nichts Neues von Jerome."

    elif t == "JEROMEBRIEF":
        return await content_strategy.send_daily_content_brief(ai)

    elif t == "JEROMEMSG":
        if not p.strip():
            return "ERROR: JEROMEMSG braucht eine Beschreibung was gesagt werden soll."
        return await jerome_comm.compose_and_send_message(ai, p.strip(), config)

    elif t == "READCHAT":
        contact = p.strip()
        if not contact:
            return "ERROR: READCHAT braucht einen Kontaktnamen oder eine Telefonnummer."
        phone = await app_control.resolve_contact_phone(contact)
        if not phone:
            reason = app_control.last_contacts_error() or "kein Treffer in Contacts.app"
            return f"ERROR: Konnte '{contact}' nicht in den Kontakten finden ({reason})."
        png_bytes = await whatsapp_tools.open_chat_and_screenshot(phone)
        return await whatsapp_tools.summarize_chat(ai, png_bytes, contact)

    elif t == "ADDCOMPETITOR":
        handle = p.strip().lstrip("@")
        if not handle:
            return "ERROR: ADDCOMPETITOR braucht einen Handle."
        added = instagram_tools.add_competitor_account(handle)
        if not added:
            return f"@{handle} wird bereits beobachtet."
        with open(CONFIG_PATH, "r") as f:  # re-read: add_competitor_account just wrote to it on disk
            config_now = json.load(f)
        sync_result = await sheets_tools.sync_competitor_accounts(
            config_now.get("competitor_accounts", []), instagram_tools.get_competitor_niches()
        )
        return f"@{handle} zur Konkurrenz-Beobachtung hinzugefuegt. Sheet-Sync: {sync_result}"

    return ""


PROCESSED_INSIGHTS_PATH = os.path.join(os.path.dirname(__file__), "memory", "processed_insights_links.json")


def _load_processed_insights_links() -> set:
    if not os.path.exists(PROCESSED_INSIGHTS_PATH):
        return set()
    try:
        with open(PROCESSED_INSIGHTS_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_insights_link_processed(url: str):
    done = _load_processed_insights_links()
    done.add(url)
    os.makedirs(os.path.dirname(PROCESSED_INSIGHTS_PATH), exist_ok=True)
    with open(PROCESSED_INSIGHTS_PATH, "w") as f:
        json.dump(sorted(done), f)


async def _handle_one_insight_link(video_link: str, us_audience_pct, reach: int) -> str:
    """The per-video branch logic that used to be the whole function, now
    reused per sequence in a batch (2026-08-07: Ahmad sends several videos'
    worth of link+insights in one WhatsApp session, not just one). Same
    routing either way: an OPEN Trial Reel Wave for that account means this
    link is Jerome's posted result, not a fresh video."""
    fields = {}
    if us_audience_pct is not None:
        fields["us_audience_pct"] = us_audience_pct
    if reach is not None:
        fields["notes"] = f"Reach {reach} (aus Insights-Screenshot, {time.strftime('%Y-%m-%d')})"

    known_handles = config.get("luna_vale_accounts", []) + config.get("competitor_accounts", [])
    account = await instagram_tools.identify_post_account(video_link, known_handles, ai)

    if account:
        open_waves = await sheets_tools.read_open_trial_waves(account)
        if open_waves:
            if reach is None:
                return (
                    f"ERROR ({video_link}): Trial-Reel-Welle fuer @{account} ist offen, aber keine "
                    "Views/Reach im Insights-Screenshot erkannt — brauche die Zahl fuer den 2x-Vergleich."
                )
            return await jerome_comm.handle_wave_result(account, video_link, reach, ai)

    existing_rows = await sheets_tools.read_winner_tracking()
    target_id = instagram_tools.normalize_video_url(video_link)
    already_tracked = any(
        instagram_tools.normalize_video_url(r.get("video_link") or "") == target_id
        for r in existing_rows if not r.get("error")
    )

    if not already_tracked:
        if not account:
            return (
                f"ERROR ({video_link}): Konnte den Account nicht sicher bestimmen — "
                "Account-Namen dazusagen, damit der Eintrag angelegt werden kann."
            )
        create_result = await sheets_tools.add_winner_tracking_entry({
            "account": account, "video_link": video_link, "views": 0,
        })
        baseline_result = await sheets_tools.compute_baseline_avg(account, video_link)
        print(f"  READINSIGHTS: neuer Eintrag angelegt: {create_result} | {baseline_result}", flush=True)

    update_result = await sheets_tools.update_winner_tracking_insights(video_link, fields)
    # Ahmad's explicit ask (2026-08-07): never let an unreadable value pass
    # silently — if the US-Audience-% genuinely couldn't be read off the
    # screenshot, say so plainly in what comes back, instead of the field
    # just quietly not being there.
    if us_audience_pct is None:
        update_result += " | HINWEIS: US-Audience-% war im Screenshot nicht sicher lesbar, wurde NICHT eingetragen."
    return update_result


async def _read_insights_from_self_chat() -> str:
    """Ahmad's actual workflow: in his 'Message Yourself' WhatsApp chat he
    sends an Instagram link, then 1-2 Insights screenshots right after —
    repeated per video, often SEVERAL videos in one sitting (2026-08-07:
    'ich muss jarvis morgen vollstaendig updaten die insights und links').
    Scrolls back through the chat (whatsapp_tools.capture_self_chat_history)
    so a whole batch is visible, not just whatever fits on screen right now,
    reads EVERY link+insights sequence in ONE Vision call (all frames
    together so it can correlate a link with the screenshot(s) that follow
    it even across a scroll boundary), then processes each one through the
    same routing as before: an OPEN Trial Reel Wave for that account means
    the link is Jerome's posted result, otherwise it's a fresh Winner
    Tracking entry.

    Already-processed links (memory/processed_insights_links.json) are
    skipped — the chat keeps its full history, so a later call in a later
    conversation would otherwise re-process the same videos and, for a
    trial-reel result, apply the SAME reported numbers to whatever wave
    happens to be open at that later point (a real risk once one wave
    closes and the next one opens).

    Never guesses a number that isn't actually visible."""
    # frames=3 wasn't enough scroll depth in practice (2026-08-07): Jarvis's
    # own alerts (self-improve reports, research digests) accumulate in this
    # same self-chat and push a real pending batch further back than 3 short
    # scroll steps reach — a live incident where "nichts geht unter" (Ahmad's
    # explicit requirement) failed silently, the batch was simply never seen.
    frames = await whatsapp_tools.capture_self_chat_history(config.get("alert_phone", ""), frames=7)
    content = []
    for i, frame in enumerate(frames):
        b64 = base64.b64encode(frame).decode("utf-8")
        content.append({"type": "text", "text": f"Ausschnitt {i + 1} von {len(frames)} (oben=aelter, unten=neuer):"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    content.append({"type": "text", "text": (
        "Das sind mehrere, teils ueberlappende Screenshots von Ahmads WhatsApp-Chat mit sich selbst, "
        "chronologisch geordnet. Finde JEDE Sequenz aus einem Instagram-Link, direkt gefolgt von ein "
        "bis zwei Screenshots mit Instagram-Insights-Daten (Publikums-Herkunft nach Land, Reichweite) — "
        "es koennen mehrere solche Sequenzen fuer verschiedene Videos sein. Lies aus jeder zugehoerigen "
        "Insights-Aufnahme den Prozentwert fuer 'United States'/'USA' und die Reichweite/Reach falls "
        "sichtbar. Wenn dieselbe Sequenz in mehreren Ausschnitten (wegen der Ueberlappung beim Scrollen) "
        "zu sehen ist, gib sie NUR EINMAL aus. Antworte NUR als Liste, eine Zeile pro Sequenz, in "
        "GENAU diesem Format: 'link=<url> us_audience=<XX.X oder unbekannt> reach=<Zahl oder unbekannt>'. "
        "Als <url> gehoert die VOLLSTAENDIGE URL aus dem Nachrichtentext, die '/reel/' oder '/p/' plus "
        "die Post-ID enthaelt — NIEMALS die reine Domain ('instagram.com'), die WhatsApp unter der "
        "Link-Vorschau anzeigt. Ist nur die Vorschau, aber nicht die volle URL lesbar, gib die "
        "Sequenz mit 'link=unlesbar' aus. "
        "Wenn keine einzige solche Sequenz zu finden ist, antworte exakt 'NICHTS_GEFUNDEN'. "
        "Erfinde NIEMALS eine Zahl oder einen Link die nicht wirklich sichtbar sind."
    )})

    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=600,
        messages=[{"role": "user", "content": content}],
    )
    raw = response.content[0].text.strip()
    if "NICHTS_GEFUNDEN" in raw:
        return "ERROR: Kein Instagram-Link mit Insights-Screenshots im Chat mit dir selbst gefunden."

    processed_before = _load_processed_insights_links()
    seen_links = set()
    results = []
    skipped = 0
    for line in raw.splitlines():
        link_match = re.search(r'link=(\S+)', line)
        if not link_match:
            continue
        raw_link = link_match.group(1)
        video_link = instagram_tools.canonical_post_url(raw_link)
        if not video_link:
            # Only the domain off WhatsApp's link-preview card got read, not
            # the URL itself (2026-08-07). Say that plainly — it used to run
            # on into a failed Page.goto and surface as "Account nicht
            # bestimmbar", which sends Ahmad looking in the wrong place.
            results.append(
                f"{raw_link}: ERROR: Kein vollstaendiger Instagram-Post-Link erkannt "
                "(nur die Domain aus der Link-Vorschau war lesbar) — schick den Link bitte "
                "noch einmal als reinen Text in den Chat."
            )
            continue
        if video_link in seen_links:
            continue  # same sequence surfaced twice across overlapping frames
        seen_links.add(video_link)

        if video_link in processed_before:
            skipped += 1
            continue

        us_match = re.search(r'us_audience=([\d.]+|unbekannt)', line)
        us_audience_pct = None
        if us_match and us_match.group(1) != "unbekannt":
            val = float(us_match.group(1))
            us_audience_pct = val / 100 if val > 1 else val
        reach_match = re.search(r'reach=(\d+)', line)
        reach = int(reach_match.group(1)) if reach_match else None

        result = await _handle_one_insight_link(video_link, us_audience_pct, reach)
        results.append(f"{video_link}: {result}")
        if not result.startswith("ERROR"):
            _mark_insights_link_processed(video_link)

    if not results:
        if skipped:
            return f"Alle {skipped} gefundenen Sequenzen wurden schon frueher verarbeitet — nichts Neues."
        return "ERROR: Kein Instagram-Link mit Insights-Screenshots im Chat mit dir selbst gefunden."

    summary = f"{len(results)} Video(s) verarbeitet"
    if skipped:
        summary += f", {skipped} bereits bekannt uebersprungen"
    return f"{summary}:\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# Native tool_use registry (Welle 1 — 5 repraesentative Tools, siehe Plan).
# Ersetzt schrittweise die [ACTION:X]-Textkonvention. Jedes ToolSpec buendelt
# das Anthropic-Schema mit dem Handler und einem speak_result-Flag, das die
# heutige hardcoded "fire-and-forget"-Liste (Zeile ~930) ersetzt: True heisst
# "nach dem Tool-Ergebnis darf/soll Claude noch etwas dazu sagen", False heisst
# "bei Erfolg still bleiben, das Vorher-Gesagte hat schon gereicht" — exakt
# das heutige Verhalten, nur nicht mehr an einen hardcoded Aktionsnamen
# gebunden. slow=True Tools bekommen einen Fallback-Fuellsatz (siehe
# SLOW_TOOL_FILLERS), falls Claude vor dem Aufruf nichts sagt.
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ToolSpec:
    schema: dict
    handler: object  # async def handler(args: dict) -> str
    speak_result: bool = True
    slow: bool = False


async def _tool_screen(args: dict) -> str:
    return await screen_capture.describe_screen(ai)


async def _tool_screen_history(args: dict) -> str:
    return memory.search_screen_awareness(args.get("query", ""))


async def _tool_open_url(args: dict) -> str:
    url = args["url"]
    await browser_tools.open_url(url)
    return f"Geoeffnet: {url}"


async def _tool_calendar_add(args: dict) -> str:
    return await calendar_tools.create_event(
        args["title"], args["date"],
        args.get("start_time", ""), args.get("end_time", ""),
        args.get("description", ""),
    )


async def _tool_whatsapp_send(args: dict) -> str:
    return await app_control.send_whatsapp(args["recipient"].strip(), args["message"].strip())


async def _tool_read_insights(args: dict) -> str:
    return await _read_insights_from_self_chat()


# --- Batch: Browser / Instagram --------------------------------------------

async def _tool_search(args: dict) -> str:
    result = await browser_tools.search_and_read(args["query"])
    if "error" not in result:
        return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')}"
    return f"Suche fehlgeschlagen: {result.get('error', '')}"


async def _tool_browse(args: dict) -> str:
    result = await browser_tools.visit(args["url"])
    if "error" not in result:
        return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')}"
    return f"Seite nicht erreichbar: {result.get('error', '')}"


async def _tool_search_compare(args: dict) -> str:
    results = await browser_tools.search_multiple(args["query"], args.get("count", 5))
    if results and "error" in results[0]:
        return f"Suche fehlgeschlagen: {results[0]['error']}"
    if not results:
        return "Keine Ergebnisse gefunden."
    lines = [f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)]
    return "\n".join(lines)


def _format_tab(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    return f"[{result['tab_id']}] {result.get('title', '')}\n{result.get('url', '')}\n\n{result.get('content', '')}"


async def _tool_browser_open_tab(args: dict) -> str:
    return _format_tab(await browser_tools.open_tab(args["url"], args.get("tab_id")))


async def _tool_browser_switch_tab(args: dict) -> str:
    return _format_tab(await browser_tools.switch_tab(args["tab_id"]))


async def _tool_browser_list_tabs(args: dict) -> str:
    tabs = await browser_tools.list_tabs()
    if not tabs:
        return "Keine Tabs offen."
    return "\n".join(f"[{t['tab_id']}] {t['title']} — {t['url']}" for t in tabs)


async def _tool_browser_close_tab(args: dict) -> str:
    return await browser_tools.close_tab(args["tab_id"])


async def _tool_browser_read_tab(args: dict) -> str:
    return _format_tab(await browser_tools.read_tab(args["tab_id"]))


async def _tool_browser_click(args: dict) -> str:
    return await browser_tools.click_element(args["tab_id"], args["description"])


async def _tool_browser_fill_field(args: dict) -> str:
    return await browser_tools.fill_field(args["tab_id"], args["field_description"], args["value"])


async def _tool_browser_extract(args: dict) -> str:
    result = await browser_tools.extract_structured(ai, args["tab_id"], args["what"])
    if "error" in result:
        return f"ERROR: {result['error']}"
    items = result.get("items", [])
    if not items:
        return "Keine passenden Eintraege auf der Seite gefunden."
    lines = []
    for i, item in enumerate(items, 1):
        fields = " | ".join(f"{k}: {v}" for k, v in item.items())
        lines.append(f"{i}. {fields}")
    return "\n".join(lines)


async def _tool_self_improve_log(args: dict) -> str:
    """Roadmap Punkt 22 — on-demand Gegenstueck zum stillen Morgen-Briefing-
    Hinweis (background_brain.py's morning_briefing_pass): explizite
    Nachfrage nach dem, was self_improve_pass zuletzt an sich selbst
    veraendert hat."""
    hours = args.get("hours", 24)
    entries = memory.get_recent_self_improve_entries(hours)
    if not entries:
        return f"In den letzten {hours} Stunden hat sich Jarvis nicht selbst an Fehlern versucht."
    lines = []
    for e in entries:
        commit = f" (Commit {e['commit_hash']})" if e.get("commit_hash") else ""
        lines.append(f"[{e['timestamp']}]{commit}: {e['result']}")
    return "\n".join(lines)


# Sicherheitsgrenze: Subagenten laufen OHNE Rueckfrage-Moeglichkeit bei Ahmad
# (das ist der Zweck von delegate_subagents/simulate_decision), duerfen darum
# NICHTS tun das eine andere echte Person erreicht, Ahmads echtes Geraet/
# Kalender/Bildschirm veraendert, beliebigen Code ausfuehrt, oder im
# Playwright-Browser final klickt/abschickt (koennte einen echten Kauf
# abschliessen) — genau die Sorte Aktion, fuer die der Hauptloop laut
# System-Prompt vorher Ahmads Bestaetigung einholen soll. Ein Subagent, der
# 'nur simuliert', darf so eine Aktion niemals versehentlich WIRKLICH
# ausloesen. Rein lesende/interne Werkzeuge (Recherche, eigenes Gedaechtnis,
# Wissensgraph, Browser OEFFNEN/LESEN) bleiben erlaubt. Jedes kuenftige neue
# Tool mit echtem Seiteneffekt muss hier bewusst ergaenzt werden.
_SUBAGENT_EXCLUDED_TOOLS = {
    "delegate_subagents", "simulate_decision",  # keine unbegrenzte Rekursion
    "calendar_add", "calendar_delete",
    "whatsapp_send", "jerome_msg",
    "open_url", "open_app",
    "screen_click", "screen_type",
    "browser_click", "browser_fill_field",
    "claude_code_exec",
}


async def _run_subagent_turn(task: str, max_rounds: int = 4) -> str:
    """Roadmap Punkt 23 — eigenstaendige, session-lose Variante von
    process_message_native fuer EINE Teilaufgabe: kein session_id/ws/_speak,
    keine gemeinsame conversations[]-Historie, kein Dialog (der Subagent kann
    nicht nachfragen, muss selbst entscheiden). Nutzt dieselben TOOL_REGISTRY/
    _run_tool wie der Hauptloop, darf aber selbst NICHT delegate_subagents/
    simulate_decision aufrufen (keine unbegrenzte Rekursion). Gibt am Ende
    eine kompakte Text-Zusammenfassung zurueck statt sie vorzulesen.

    _SUBAGENT_TOOLS wird bewusst HIER drin aus dem globalen TOOLS gefiltert
    (nicht als Modul-Konstante direkt neben TOOL_REGISTRY) — TOOLS selbst
    entsteht erst NACH TOOL_REGISTRY (siehe unten), und diese Funktion muss
    schon VOR TOOL_REGISTRY existieren, weil delegate_subagents/simulate_decision
    dort als Handler direkt referenziert werden."""
    subagent_tools = [t for t in TOOLS if t["name"] not in _SUBAGENT_EXCLUDED_TOOLS]
    messages = [{"role": "user", "content": task}]
    system = (
        "Du bist Jarvis' interner Subagent fuer genau eine Teilaufgabe, kein Dialog moeglich "
        "(keine Rueckfrage, entscheide selbst wenn etwas unklar ist). Nutze die verfuegbaren "
        "Werkzeuge um die Aufgabe zu bearbeiten. Antworte am Ende NUR mit einer kompakten, "
        "auf Deutsch geschriebenen Zusammenfassung deiner Ergebnisse — keine Anrede, keine "
        "Hoeflichkeitsfloskeln, nur die Fakten."
    )

    last_text = ""
    for _ in range(max_rounds):
        try:
            response = await ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=system,
                tools=subagent_tools,
                messages=messages,
            )
        except Exception as e:
            return f"ERROR: Subagent-Aufruf fehlgeschlagen: {e}"

        text_blocks = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if text_blocks:
            last_text = " ".join(text_blocks)

        messages.append({"role": "assistant", "content": response.content})

        if not tool_use_blocks:
            return last_text or "Subagent hat keine Zusammenfassung geliefert."

        results = await asyncio.gather(*(_run_tool(b) for b in tool_use_blocks))
        tool_result_content = [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"], "is_error": r["is_error"]}
            for r in results
        ]
        messages.append({"role": "user", "content": tool_result_content})

    return last_text or f"Subagent hat die Aufgabe nicht innerhalb von {max_rounds} Runden abgeschlossen."


async def _tool_delegate_subagents(args: dict) -> str:
    """Roadmap Punkt 23 — mehrere wirklich unabhaengige Teilaufgaben parallel
    statt seriell Modul fuer Modul. Jede Teilaufgabe laeuft als eigener
    _run_subagent_turn mit eigenem Tool-Zugriff, nicht nur ein einzelner
    Haiku-Call ohne Werkzeuge."""
    subtasks = args.get("subtasks") or []
    if not subtasks:
        return "ERROR: keine Teilaufgaben angegeben."
    results = await asyncio.gather(*(_run_subagent_turn(t) for t in subtasks))
    lines = [f"{i}. {t}\n   -> {r}" for i, (t, r) in enumerate(zip(subtasks, results), 1)]
    return "\n".join(lines)


async def _tool_simulate_decision(args: dict) -> str:
    """Roadmap Punkt 24 — nutzt _run_subagent_turn (Punkt 23) direkt: erst
    plausible Optionen generieren (falls nicht mitgegeben), dann jede parallel
    simulieren (darf dabei Werkzeuge nutzen, z.B. den Wissensgraphen fuer
    aehnliche vergangene Entscheidungen), zuletzt eine Empfehlung synthetisieren."""
    decision = args.get("decision", "").strip()
    if not decision:
        return "ERROR: keine Entscheidung angegeben."
    context = args.get("context", "").strip()
    options = args.get("options") or []

    if not options:
        options_prompt = (
            f"Entscheidung: {decision}\nKontext: {context or 'keiner angegeben'}\n\n"
            "Nenne 2 bis 4 plausible, klar unterscheidbare Handlungsoptionen fuer diese Entscheidung. "
            "Antworte NUR als JSON-Array von kurzen Strings, z.B. [\"Option A\", \"Option B\"]."
        )
        try:
            resp = await ai.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=300,
                messages=[{"role": "user", "content": options_prompt}],
            )
            raw = resp.content[0].text.strip()
            start, end = raw.index("["), raw.rindex("]") + 1
            options = json.loads(raw[start:end])
        except Exception as e:
            return f"ERROR: Konnte keine Optionen generieren: {e}"

    if not options:
        return "ERROR: keine Optionen zum Simulieren gefunden."

    sim_tasks = [
        f"Simuliere den wahrscheinlichsten Ausgang, wenn bei folgender Entscheidung diese Option "
        f"gewaehlt wird: '{opt}'. Entscheidung: {decision}. Kontext: {context or 'keiner angegeben'}. "
        "Nenne konkrete Chancen und Risiken, mehrere Schritte vorausgedacht. Nutze verfuegbare "
        "Werkzeuge (z.B. den Wissensgraphen) falls das bei aehnlichen frueheren Entscheidungen hilft."
        for opt in options
    ]
    simulations = await asyncio.gather(*(_run_subagent_turn(t) for t in sim_tasks))

    synthesis_prompt = (
        f"Entscheidung: {decision}\nKontext: {context or 'keiner angegeben'}\n\n"
        "Simulierte Ausgaenge je Option:\n"
        + "\n".join(f"- {opt}: {sim}" for opt, sim in zip(options, simulations))
        + "\n\nVergleiche die Optionen und gib eine begruendete Empfehlung auf Deutsch, in 3-5 Saetzen."
    )
    try:
        resp = await ai.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role": "user", "content": synthesis_prompt}],
        )
        recommendation = resp.content[0].text.strip()
    except Exception as e:
        recommendation = f"(Empfehlung konnte nicht synthetisiert werden: {e})"

    lines = [f"Simulierte Optionen fuer: {decision}"]
    for opt, sim in zip(options, simulations):
        lines.append(f"- {opt}: {sim}")
    lines.append(f"\nEmpfehlung: {recommendation}")
    return "\n".join(lines)


async def _tool_news(args: dict) -> str:
    return await browser_tools.fetch_news()


async def _tool_instagram_trend(args: dict) -> str:
    return instagram_tools.format_trend_summary()


async def _tool_instagram_link(args: dict) -> str:
    return instagram_tools.add_tracked_link(args["url"].strip(), args.get("label", "").strip())


async def _tool_research(args: dict) -> str:
    summary = await research.research_topic(ai, args["topic"])
    if "NICHTS_NEUES" in summary:
        return "Zu diesem Thema konnte ich gerade nichts Neues oder Relevantes finden."
    memory.add_knowledge(summary, category="on-demand")
    return summary


async def _tool_video_analysis(args: dict) -> str:
    handle = args["handle"].strip().lstrip("@")
    await instagram_tools.analyze_recent_videos(handle, ai, count=6)
    return instagram_tools.format_video_analysis(handle)


async def _tool_add_competitor(args: dict) -> str:
    handle = args["handle"].strip().lstrip("@")
    added = instagram_tools.add_competitor_account(handle)
    if not added:
        return f"@{handle} wird bereits beobachtet."
    with open(CONFIG_PATH, "r") as f:  # re-read: add_competitor_account just wrote to it on disk
        config_now = json.load(f)
    sync_result = await sheets_tools.sync_competitor_accounts(
        config_now.get("competitor_accounts", []), instagram_tools.get_competitor_niches()
    )
    return f"@{handle} zur Konkurrenz-Beobachtung hinzugefuegt. Sheet-Sync: {sync_result}"


async def _tool_screen_click(args: dict) -> str:
    return await screen_control.click_on(args["description"], ai)


async def _tool_screen_type(args: dict) -> str:
    return screen_control.type_text(args["text"])


# --- Batch: Calendar / Gmail / WhatsApp -------------------------------------

async def _tool_calendar_list(args: dict) -> str:
    days = args.get("days", 7)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    return await calendar_tools.list_upcoming_events(days)


async def _tool_calendar_delete(args: dict) -> str:
    return await calendar_tools.delete_event(args["title_query"].strip(), args["date"].strip())


async def _tool_gmail_check(args: dict) -> str:
    return await gmail_tools.get_recent_emails_summary()


async def _tool_whatsapp_check(args: dict) -> str:
    return await whatsapp_tools.check_new_messages(ai)


async def _tool_read_chat(args: dict) -> str:
    contact = args["contact"].strip()
    phone = await app_control.resolve_contact_phone(contact)
    if not phone:
        reason = app_control.last_contacts_error() or "kein Treffer in Contacts.app"
        return f"ERROR: Konnte '{contact}' nicht in den Kontakten finden ({reason})."
    png_bytes = await whatsapp_tools.open_chat_and_screenshot(phone)
    return await whatsapp_tools.summarize_chat(ai, png_bytes, contact)


# --- Batch: Sheets / Tracking / Jerome --------------------------------------

async def _tool_winner_track(args: dict) -> str:
    entry = {"account": args["account"], "video_link": args["video_link"]}
    try:
        entry["views"] = int(str(args["views"]).replace(".", "").replace(",", ""))
    except (ValueError, TypeError):
        return f"ERROR: '{args.get('views')}' ist keine gueltige Views-Zahl."
    if args.get("comments_total") is not None:
        try:
            entry["comments_total"] = int(args["comments_total"])
        except (ValueError, TypeError):
            pass
    create_result = await sheets_tools.add_winner_tracking_entry(entry)
    baseline_result = await sheets_tools.compute_baseline_avg(entry["account"], entry["video_link"])
    return f"{create_result}\n{baseline_result}"


async def _tool_winner_status(args: dict) -> str:
    account = (args.get("account") or "").strip() or None
    rows = await sheets_tools.read_winner_tracking(account=account)
    if not rows:
        return f"Keine Winner-Tracking-Eintraege fuer '{account or ''}' gefunden."
    if rows[0].get("error"):
        return f"ERROR: {rows[0]['error']}"
    lines = [
        f"{r['video_link']}: {r['views']} Views, Outlier={r['outlier']}, Decision={r['decision']}, "
        f"US-Audience={r['us_audience_pct']}"
        for r in rows
    ]
    return "\n".join(lines)


async def _tool_scaling_log(args: dict) -> str:
    next_step = args.get("next_step") or "Trial Reel mit dieser Variable testen."

    async def _notify_ahmad(text: str):
        await app_control.send_whatsapp(config.get("alert_phone", ""), text)

    return await jerome_comm.handle_trial_reel_candidate(
        args["account"], args["video_link"], args["virality_factor"], next_step,
        notify_ahmad_fn=_notify_ahmad,
    )


async def _tool_jerome_check(args: dict) -> str:
    reply = await jerome_comm.check_jerome_replies(ai)
    return f"Jerome hat geantwortet: {reply}" if reply else "Nichts Neues von Jerome."


async def _tool_jerome_brief(args: dict) -> str:
    return await content_strategy.send_daily_content_brief(ai)


async def _tool_jerome_msg(args: dict) -> str:
    return await jerome_comm.compose_and_send_message(ai, args["description"].strip(), config)


# --- Batch: Rest (OpenApp / Claude Code / Fanplace / SLT.bio / Remember) ---

async def _tool_open_app(args: dict) -> str:
    return await app_control.open_app(args["app_name"])


async def _tool_claude_code_context(args: dict) -> str:
    return claude_code_context.get_recent_context(args["question"])


async def _tool_claude_code_exec(args: dict) -> str:
    task = args.get("task", "").strip()
    if not task:
        return "ERROR: Kein Auftrag angegeben, bitte die Aufgabe fuer Claude Code konkret formulieren."
    return await claude_code_tool.run_claude_code(task)


async def _tool_fanplace_snapshot(args: dict) -> str:
    return await fanplace.get_snapshot()


async def _tool_sltbio_snapshot(args: dict) -> str:
    return await slt_bio_tools.format_for_jarvis()


async def _tool_remember(args: dict) -> str:
    return memory.remember(args["fact"].strip(), args["category"].strip())


# --- Batch: Ziel-/Projekt-Tracking (Tier 1, Punkt 8) ------------------------

async def _tool_track_goal(args: dict) -> str:
    return goal_tracker.add_goal(args["description"].strip(), args.get("check_in_days", 3))


async def _tool_update_goal(args: dict) -> str:
    return goal_tracker.update_goal(
        args["title_query"].strip(), args.get("note", "").strip(), args.get("status", "").strip()
    )


async def _tool_list_goals(args: dict) -> str:
    return goal_tracker.format_active_goals()


# --- Batch: Wissensgraph (Tier 2, Punkt 10) ---------------------------------

async def _tool_remember_relation(args: dict) -> str:
    return knowledge_graph.add_relation(
        args["from_name"].strip(),
        args.get("from_type", "").strip(),
        args["relation"].strip(),
        args["to_name"].strip(),
        args.get("to_type", "").strip(),
        args.get("description", "").strip(),
    )


async def _tool_query_relations(args: dict) -> str:
    return knowledge_graph.query_entity(args["entity_name"].strip())


async def _tool_record_decision_outcome(args: dict) -> str:
    return knowledge_graph.record_outcome(
        args["decision_query"].strip(), args["outcome"].strip(), args.get("status", "mixed").strip()
    )


async def _tool_list_decisions(args: dict) -> str:
    return knowledge_graph.list_decisions(args.get("status", "").strip())


TOOL_REGISTRY: dict = {
    "screen": ToolSpec(
        schema={
            "name": "screen",
            "description": "Bildschirm ansehen und beschreiben. Sag vorher kurz einen Satz, das dauert ein paar Sekunden (interner Vision-Call).",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_screen,
        speak_result=True,
        slow=True,
    ),
    "screen_history": ToolSpec(
        schema={
            "name": "screen_history",
            "description": "Durchsucht die im Hintergrund alle 20-30 Minuten aufgezeichneten Bildschirm-Beobachtungen (Roadmap Punkt 19, Text-Zusammenfassungen, keine Bilder, 30 Tage aufbewahrt). Nutze das wenn Ahmad fragt woran er zuletzt/heute/an einem bestimmten Tag gearbeitet hat. query ist ein Stichwort oder ein Datum (JJJJ-MM-TT), leer = die letzten Eintraege insgesamt.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Stichwort oder Datum, optional"}},
                "required": [],
            },
        },
        handler=_tool_screen_history,
        speak_result=True,
    ),
    "open_url": ToolSpec(
        schema={
            "name": "open_url",
            "description": "URL im echten, sichtbaren Chrome-Browser oeffnen. Nutzen um Sir direkt etwas zu zeigen statt nur zu beschreiben.",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Die zu oeffnende URL."}},
                "required": ["url"],
            },
        },
        handler=_tool_open_url,
        speak_result=False,
    ),
    "calendar_add": ToolSpec(
        schema={
            "name": "calendar_add",
            "description": "Termin anlegen und sofort speichern, keine Bestaetigung noetig. Rechne relative Datumsangaben (morgen, naechsten Montag) selbst in ein konkretes Datum um, bevor du das Tool aufrufst.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string", "description": "Format JJJJ-MM-TT."},
                    "start_time": {"type": "string", "description": "Format HH:MM (24h). Weglassen fuer einen ganztaegigen Termin."},
                    "end_time": {"type": "string", "description": "Format HH:MM (24h). Weglassen fuer einen ganztaegigen Termin."},
                    "description": {"type": "string", "description": "Optional."},
                },
                "required": ["title", "date"],
            },
        },
        handler=_tool_calendar_add,
        speak_result=True,
    ),
    "whatsapp_send": ToolSpec(
        schema={
            "name": "whatsapp_send",
            "description": "WhatsApp-Nachricht schreiben und SOFORT absenden, keine Bestaetigung noetig. recipient ist ein Name aus den Kontakten oder eine Telefonnummer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["recipient", "message"],
            },
        },
        handler=_tool_whatsapp_send,
        speak_result=False,
    ),
    "read_insights": ToolSpec(
        schema={
            "name": "read_insights",
            "description": "Liest Instagram-Insights-Screenshots aus dem Chat mit sich selbst aus und traegt sie strukturiert ins Winner Tracking bzw. eine offene Trial-Reel-Welle ein. Kein Parameter noetig. Kann laenger dauern.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_read_insights,
        speak_result=True,
        slow=True,
    ),

    # --- Batch: Browser / Instagram ---
    "search": ToolSpec(
        schema={
            "name": "search",
            "description": "Internet durchsuchen und Ergebnisse zusammenfassen.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        handler=_tool_search,
        speak_result=True,
    ),
    "browse": ToolSpec(
        schema={
            "name": "browse",
            "description": "Eine bestimmte, bereits bekannte URL direkt aufrufen und ihren Inhalt lesen (anders als search: keine Suche, die Seite ist schon klar).",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
        handler=_tool_browse,
        speak_result=True,
    ),
    "search_compare": ToolSpec(
        schema={
            "name": "search_compare",
            "description": (
                "Liefert mehrere Suchergebnisse (Titel, URL, Snippet) statt wie 'search' blind auf den "
                "ersten Treffer zu klicken. Nutze das bei echten Vergleichsaufgaben ('was ist die beste "
                "Option fuer...', 'vergleiche X und Y') — danach gezielt 'browse' auf die 1-2 "
                "vielversprechendsten URLs bevor du eine begruendete Empfehlung gibst. Fuer einfache "
                "Faktenfragen bleibt 'search' die richtige, schnellere Wahl."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer", "description": "Wie viele Ergebnisse, Default 5."},
                },
                "required": ["query"],
            },
        },
        handler=_tool_search_compare,
        speak_result=True,
    ),
    "browser_open_tab": ToolSpec(
        schema={
            "name": "browser_open_tab",
            "description": (
                "Oeffnet eine URL in einem ECHTEN, bestehen bleibenden Browser-Tab (anders als 'browse', "
                "das nur kurz liest) — fuer Multi-Tab-Aufgaben (mehrere Seiten parallel offen halten und "
                "vergleichen). Gibt sofort den Inhalt zurueck. tab_id optional, sonst automatisch vergeben "
                "(z.B. 'tab1') — merke dir die zurueckgegebene tab_id fuer spaeteres browser_switch_tab."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "tab_id": {"type": "string"}},
                "required": ["url"],
            },
        },
        handler=_tool_browser_open_tab,
        speak_result=True,  # KEIN Fire-and-Forget — der Inhalt muss im naechsten
        # Tool-Runden-Schritt synthetisiert werden koennen, sonst bricht der
        # native Loop nach dem Oeffnen still ab (echter Bug, live gefunden 2026-08-08).
    ),
    "browser_switch_tab": ToolSpec(
        schema={
            "name": "browser_switch_tab",
            "description": "Wechselt zu einem bereits mit browser_open_tab geoeffneten Tab und liest seinen aktuellen Inhalt (kann sich seitdem veraendert haben).",
            "input_schema": {
                "type": "object",
                "properties": {"tab_id": {"type": "string"}},
                "required": ["tab_id"],
            },
        },
        handler=_tool_browser_switch_tab,
        speak_result=True,  # gleicher Grund wie browser_open_tab
    ),
    "browser_list_tabs": ToolSpec(
        schema={
            "name": "browser_list_tabs",
            "description": "Listet alle aktuell offenen Browser-Tabs (Handle, Titel, URL) — fuer Ueberblick bei mehreren parallel offenen Seiten.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_browser_list_tabs,
        speak_result=True,
    ),
    "browser_close_tab": ToolSpec(
        schema={
            "name": "browser_close_tab",
            "description": "Schliesst einen mit browser_open_tab geoeffneten Tab wieder.",
            "input_schema": {
                "type": "object",
                "properties": {"tab_id": {"type": "string"}},
                "required": ["tab_id"],
            },
        },
        handler=_tool_browser_close_tab,
        speak_result=False,
    ),
    "browser_read_tab": ToolSpec(
        schema={
            "name": "browser_read_tab",
            "description": "Liest einen bereits offenen Tab neu, OHNE ihn nach vorne zu holen (z.B. um zu pruefen was sich nach einer Aktion in einem anderen Tab geaendert hat).",
            "input_schema": {
                "type": "object",
                "properties": {"tab_id": {"type": "string"}},
                "required": ["tab_id"],
            },
        },
        handler=_tool_browser_read_tab,
        speak_result=True,  # gleicher Grund wie browser_open_tab
    ),
    "browser_click": ToolSpec(
        schema={
            "name": "browser_click",
            "description": (
                "Klickt ein Element in einem offenen Browser-Tab, gefunden ueber Text/Rolle im DOM "
                "(zuverlaessiger als SCREENCLICK, aber NUR innerhalb dieses Playwright-Browsers nutzbar). "
                "Bei REVERSIBLEN Zielen (navigieren, ausklappen, in den Warenkorb legen, ein Suchfeld "
                "abschicken) selbststaendig nutzbar. Bei ERKENNBAR IRREVERSIBLEN Zielen (Kaufen, Bestellen, "
                "Bezahlen, Abschicken/Senden, Loeschen, Bestaetigen — alles was eine Zahlung/Bestellung/"
                "Nachricht abschliesst oder Daten zerstoert) NUR wenn Ahmad es JETZT GERADE explizit sagt "
                "oder auf deine Rueckfrage zustimmt — exakt dieselbe Regel wie bei SCREENCLICK, hier "
                "genauso bindend. Findet nichts eindeutig Passendes: klickt NICHTS, meldet das ehrlich."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string"},
                    "description": {"type": "string", "description": "Wonach gesucht wird, z.B. 'In den Warenkorb' oder 'Jetzt kaufen'."},
                },
                "required": ["tab_id", "description"],
            },
        },
        handler=_tool_browser_click,
        speak_result=True,
    ),
    "browser_fill_field": ToolSpec(
        schema={
            "name": "browser_fill_field",
            "description": "Fuellt ein Formularfeld in einem offenen Browser-Tab aus (gefunden ueber Label/Placeholder). Reversibel (nichts wird abgeschickt) — selbststaendig nutzbar, das eigentliche Absenden laeuft separat ueber browser_click mit derselben Kaufen/Bestaetigen-Regel.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string"},
                    "field_description": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["tab_id", "field_description", "value"],
            },
        },
        handler=_tool_browser_fill_field,
        speak_result=True,  # gleicher Grund wie browser_open_tab — nach dem
        # Ausfuellen folgt meist noch ein Klick oder eine Pruefung
    ),
    "browser_extract": ToolSpec(
        schema={
            "name": "browser_extract",
            "description": (
                "Liest einen bereits offenen Tab (browser_open_tab/browser_read_tab) und extrahiert "
                "ALLE passenden Eintraege als saubere Felder statt Fliesstext — nutze das bei "
                "Vergleichs-/Listenseiten mit vielen aehnlichen Eintraegen (Fluege, Preise, Angebote), "
                "statt selbst aus dem rohen Text zu raten. Fuer einzelne Fakten oder kurze Seiten "
                "reicht browser_read_tab weiterhin."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string"},
                    "what": {
                        "type": "string",
                        "description": "Was extrahiert werden soll, mit Feldern, z.B. 'Flugangebote mit Preis, Airline, Abflugzeit'.",
                    },
                },
                "required": ["tab_id", "what"],
            },
        },
        handler=_tool_browser_extract,
        speak_result=True,
        slow=True,
    ),
    "self_improve_log": ToolSpec(
        schema={
            "name": "self_improve_log",
            "description": (
                "Zeigt was Jarvis zuletzt selbststaendig im Hintergrund an sich selbst repariert hat "
                "(Roadmap Punkt 22). Nutze das bei Nachfragen wie 'was hast du zuletzt an dir selbst "
                "geaendert' oder 'gab es Selbstreparaturen'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Zeitraum in Stunden, Default 24."},
                },
                "required": [],
            },
        },
        handler=_tool_self_improve_log,
        speak_result=True,
    ),
    "delegate_subagents": ToolSpec(
        schema={
            "name": "delegate_subagents",
            "description": (
                "Roadmap Punkt 23 — mehrere WIRKLICH unabhaengige Teilaufgaben parallel bearbeiten "
                "lassen (z.B. Instagram-Zahlen UND Finanzstatus UND aktuelle Trends in einer Anfrage), "
                "statt sie seriell nacheinander abzufragen. Jede Teilaufgabe bekommt einen eigenen "
                "Subagenten mit Lese-/Recherche-Werkzeugen (kein WhatsApp/Jerome/Kalender/Kauf/Code-"
                "Ausfuehrung — dafuer bleibt der normale Weg mit Ahmads Bestaetigung noetig). NUR "
                "nutzen wenn die Teilaufgaben wirklich unabhaengig voneinander sind, nicht fuer "
                "einfache sequenzielle Anfragen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "subtasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 bis 5 klar formulierte, unabhaengige Teilaufgaben.",
                    },
                },
                "required": ["subtasks"],
            },
        },
        handler=_tool_delegate_subagents,
        speak_result=True,
        slow=True,
    ),
    "simulate_decision": ToolSpec(
        schema={
            "name": "simulate_decision",
            "description": (
                "Roadmap Punkt 24 — vor einer wichtigen Entscheidung (z.B. Jerome-bezogen: 'sollen wir "
                "dieses Reel-Format skalieren?') mehrere moegliche Ausgaenge durchspielen lassen, bevor "
                "eine Empfehlung kommt. Reine Simulation/Denkarbeit — die dabei genutzten Subagenten "
                "koennen NICHTS wirklich auslösen (kein Senden/Kalender/Kauf), das bleibt bewusst "
                "getrennt von einer echten Umsetzung. Kein Hellsehen, aber strukturiertes Vorausdenken "
                "statt Bauchgefuehl. Sag vorher einen kurzen Satz wie 'Ich denke das kurz durch.'"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "description": "Die zu treffende Entscheidung."},
                    "context": {"type": "string", "description": "Relevanter Hintergrund, optional."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Konkrete Optionen, falls schon bekannt. Sonst generiert Jarvis selbst welche.",
                    },
                },
                "required": ["decision"],
            },
        },
        handler=_tool_simulate_decision,
        speak_result=True,
        slow=True,
    ),
    "news": ToolSpec(
        schema={
            "name": "news",
            "description": "Aktuelle Weltnachrichten abrufen. Nutzen bei Fragen zu News, Nachrichten, Weltgeschehen. Sag vorher einen kurzen Satz wie 'Ich schaue nach den aktuellen Nachrichten.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_news,
        speak_result=True,
        slow=True,
    ),
    "instagram_trend": ToolSpec(
        schema={
            "name": "instagram_trend",
            "description": (
                "Instagram-Zahlen (Follower, Beitraege, Trend) von Luna Vale's eigenen Accounts und "
                "der hinterlegten Konkurrenz pruefen (letzter Hintergrund-Stand, nicht live re-gescraped). "
                "Ergebnis ist getrennt in EIGENE ACCOUNTS (lunaxvale/cowgirllunavale/lunavalethegoth) und "
                "KONKURRENZ. Bei 'meinen'/'unseren' Accounts NUR den EIGENE-ACCOUNTS-Block nutzen. "
                "Sag vorher einen kurzen Satz wie 'Ich schaue kurz bei Instagram nach.'"
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_instagram_trend,
        speak_result=True,
        slow=True,
    ),
    "instagram_link": ToolSpec(
        schema={
            "name": "instagram_link",
            "description": "Einen Instagram-Post/Reel-Link dauerhaft im Blick behalten, um seine Performance ueber Zeit zu vergleichen.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "label": {"type": "string", "description": "Optional, kurze Beschreibung."},
                },
                "required": ["url"],
            },
        },
        handler=_tool_instagram_link,
        speak_result=False,
    ),
    "research": ToolSpec(
        schema={
            "name": "research",
            "description": "Live im Internet zu einem konkreten Thema recherchieren (z.B. Algorithmus-Aenderung, Nischen-Trend). Sag vorher einen kurzen Satz wie 'Ich recherchiere das kurz.'",
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        },
        handler=_tool_research,
        speak_result=True,
        slow=True,
    ),
    "video_analysis": ToolSpec(
        schema={
            "name": "video_analysis",
            "description": "Letzte Videos EINES Instagram-Accounts pruefen: Views, Likes, Kommentare, Publikum eher englischsprachig/Premium oder nicht. Sag vorher 'Ich schaue mir die letzten Videos an.'",
            "input_schema": {
                "type": "object",
                "properties": {"handle": {"type": "string", "description": "Instagram-Name ohne @."}},
                "required": ["handle"],
            },
        },
        handler=_tool_video_analysis,
        speak_result=True,
        slow=True,
    ),
    "add_competitor": ToolSpec(
        schema={
            "name": "add_competitor",
            "description": "Neuen Konkurrenz-Account manuell zur Beobachtung hinzufuegen — wird sofort in die Target Creator List aufgenommen.",
            "input_schema": {
                "type": "object",
                "properties": {"handle": {"type": "string", "description": "Ohne @."}},
                "required": ["handle"],
            },
        },
        handler=_tool_add_competitor,
        speak_result=True,
    ),
    "screen_click": ToolSpec(
        schema={
            "name": "screen_click",
            "description": (
                "Echter Mausklick auf dem Bildschirm an der beschriebenen Stelle. Bei REVERSIBLEN Klicks "
                "(navigieren, oeffnen, ausklappen) waehrend eines laufenden Gespraechs selbststaendig "
                "nutzbar. Bei IRREVERSIBLEN Klicks (Senden, Loeschen, Bestaetigen, Kaufen) NUR wenn Ahmad "
                "es JETZT explizit sagt oder zustimmt. Sag vorher kurz was geklickt wird."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
        handler=_tool_screen_click,
        speak_result=False,
    ),
    "screen_type": ToolSpec(
        schema={
            "name": "screen_type",
            "description": "Tippt Text an der aktuellen Cursor-Position ein. Reversibles Ausfuellen waehrend eines Gespraechs ok, vor dem tatsaechlichen Absenden/Bestaetigen immer erst nachfragen.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        handler=_tool_screen_type,
        speak_result=False,
    ),

    # --- Batch: Calendar / Gmail / WhatsApp ---
    "calendar_list": ToolSpec(
        schema={
            "name": "calendar_list",
            "description": "Bevorstehende Termine im Google Kalender abrufen. Nutzen bei Fragen was ansteht, ob Zeit ist, oder nach dem Terminplan.",
            "input_schema": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Anzahl Tage voraus, Default sieben."}},
                "required": [],
            },
        },
        handler=_tool_calendar_list,
        speak_result=True,
    ),
    "calendar_delete": ToolSpec(
        schema={
            "name": "calendar_delete",
            "description": (
                "Einen Termin absagen/loeschen. Findet den Termin ueber Titel an dem Tag — wenn mehrere "
                "passen oder keiner gefunden wird, wird NICHTS geloescht (nie raten bei etwas Unumkehrbarem). "
                "Nur nutzen wenn Ahmad ausdruecklich sagt einen Termin abzusagen/zu loeschen/zu streichen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title_query": {"type": "string"},
                    "date": {"type": "string", "description": "Format JJJJ-MM-TT."},
                },
                "required": ["title_query", "date"],
            },
        },
        handler=_tool_calendar_delete,
        speak_result=True,
    ),
    "gmail_check": ToolSpec(
        schema={
            "name": "gmail_check",
            "description": "Letzte E-Mails im Postfach abrufen (Absender, Betreff, Kurzausschnitt, gelesen/ungelesen). Sag vorher 'Ich schaue kurz ins Postfach.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_gmail_check,
        speak_result=True,
        slow=True,
    ),
    "whatsapp_check": ToolSpec(
        schema={
            "name": "whatsapp_check",
            "description": "Prueft WhatsApp auf ungelesene Nachrichten (wer hat geschrieben, worum es geht). Sag vorher 'Ich schaue kurz bei WhatsApp nach.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_whatsapp_check,
        speak_result=True,
        slow=True,
    ),
    "read_chat": ToolSpec(
        schema={
            "name": "read_chat",
            "description": (
                "Oeffnet den WhatsApp-Chat mit einer bestimmten Person und fasst zusammen worum es in den "
                "letzten Nachrichten geht. NUR auf konkrete Nachfrage nutzen, niemals von dir aus oder im "
                "Hintergrund private Chats durchsuchen. Sag vorher 'Ich schaue kurz im Chat mit X nach.'"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"contact": {"type": "string", "description": "Name aus Contacts.app oder Telefonnummer."}},
                "required": ["contact"],
            },
        },
        handler=_tool_read_chat,
        speak_result=True,
        slow=True,
    ),

    # --- Batch: Sheets / Tracking / Jerome ---
    "winner_track": ToolSpec(
        schema={
            "name": "winner_track",
            "description": (
                "Traegt ein Video ins Winner-Tracking-Tab ein (natives Google Sheet, live). Die Formeln "
                "(Multiplier/Outlier/Decision) berechnet das Sheet selbst — NUR Rohdaten eintragen, "
                "NIEMALS selbst eine Bewertung ausrechnen. US-Audience% fehlt meistens noch (kommt nur "
                "aus einem Insights-Screenshot) — das dazusagen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "video_link": {"type": "string"},
                    "views": {"type": "integer"},
                    "comments_total": {"type": "integer", "description": "Optional."},
                },
                "required": ["account", "video_link", "views"],
            },
        },
        handler=_tool_winner_track,
        speak_result=True,
    ),
    "winner_status": ToolSpec(
        schema={
            "name": "winner_status",
            "description": (
                "Liest die vom Sheet bereits berechneten Decisions (KEEP/CAUTION/DELETE) und Outlier-Status "
                "der letzten Eintraege eines Accounts. Nutzen um zu wissen was das Sheet zu einem Video schon "
                "sagt, BEVOR eine Empfehlung gegeben wird — nie selbst die 20/40-Regel nachrechnen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"account": {"type": "string", "description": "Optional, leer = alle Accounts."}},
                "required": [],
            },
        },
        handler=_tool_winner_status,
        speak_result=True,
    ),
    "scaling_log": ToolSpec(
        schema={
            "name": "scaling_log",
            "description": (
                "Legt einen Trial-Reel-Kandidaten im Scaling-Log-Tab an UND benachrichtigt Jerome direkt "
                "per WhatsApp mit einer klaren Anweisung. Selbststaendig nutzen sobald winner_status fuer "
                "ein Video Decision=KEEP UND (Outlier=YES ODER gute US-Audience) zeigt. virality_factor und "
                "next_step werden UNVERAENDERT an Jerome geschickt — deshalb IMMER auf ENGLISCH schreiben, "
                "auch wenn sonst Deutsch gesprochen wird. Ein bereits gemeldetes Video bekommt keine zweite Nachricht."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "video_link": {"type": "string"},
                    "virality_factor": {"type": "string", "description": "Auf Englisch: was am Video funktioniert hat (Hook/Outfit/Sound/Caption)."},
                    "next_step": {"type": "string", "description": "Auf Englisch: klare kurze Anweisung, z.B. 'same hook, different outfit'."},
                },
                "required": ["account", "video_link", "virality_factor"],
            },
        },
        handler=_tool_scaling_log,
        speak_result=True,
    ),
    "jerome_check": ToolSpec(
        schema={
            "name": "jerome_check",
            "description": "Prueft ob Jerome auf WhatsApp geantwortet hat, gibt seine neueste Nachricht zurueck. Sag vorher 'Ich schaue kurz ob Jerome geantwortet hat.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_jerome_check,
        speak_result=True,
        slow=True,
    ),
    "jerome_brief": ToolSpec(
        schema={
            "name": "jerome_brief",
            "description": (
                "Recherchiert JEDEN Luna-Vale-Account gruendlich und schickt Jerome danach GENAU EINE "
                "vollstaendige Nachricht mit konkreten Video-Links. Dauert mehrere Minuten. Sag VORHER "
                "'Ich recherchiere das jetzt gruendlich fuer Jerome, das dauert ein paar Minuten.' NIEMALS "
                "selbst per whatsapp_send eine eigene Content-Strategie zusammenschreiben, IMMER dieses "
                "Tool nutzen (Ad-hoc-Nachrichten sind schon mehrfach kaputt/halbfertig bei Jerome angekommen)."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_jerome_brief,
        speak_result=True,
        slow=True,
    ),
    "jerome_msg": ToolSpec(
        schema={
            "name": "jerome_msg",
            "description": (
                "Fuer JEDE andere freie/laengere Nachricht an Jerome (Tipps geben, etwas erklaeren, eine "
                "Ankuendigung). description ist NUR eine KURZE Beschreibung was gesagt werden soll, NICHT "
                "der fertige Nachrichtentext — der wird danach separat mit vollem Budget geschrieben und in "
                "einem Rutsch geschickt. Fuer Jerome IMMER dieses Tool oder jerome_brief nutzen, "
                "whatsapp_send nur fuer andere Kontakte. Sag vorher 'Ich schreibe Jerome das jetzt.'"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
        handler=_tool_jerome_msg,
        speak_result=True,
        slow=True,
    ),

    # --- Batch: Rest ---
    "open_app": ToolSpec(
        schema={
            "name": "open_app",
            "description": "Eine App auf dem Mac oeffnen (z.B. 'Spotify', 'Notizen', 'Calendar'). Nutzen wenn Sir bittet eine App zu oeffnen.",
            "input_schema": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
        },
        handler=_tool_open_app,
        speak_result=False,
    ),
    "claude_code_context": ToolSpec(
        schema={
            "name": "claude_code_context",
            "description": "Nutzen wenn Ahmad wissen will was zuletzt mit Claude Code oder in der Claude Desktop App besprochen/gebaut wurde, woran gerade gearbeitet wird, oder Kontext aus laufenden Projekten braucht.",
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "Die konkrete Frage/das Thema in eigenen Worten."}},
                "required": ["question"],
            },
        },
        handler=_tool_claude_code_context,
        speak_result=True,
    ),
    "claude_code_exec": ToolSpec(
        schema={
            "name": "claude_code_exec",
            "description": (
                "Eine Aufgabe an Claude Code auslagern: programmieren, ein Skript schreiben, tief in Code/"
                "Daten recherchieren. NIEMALS fuer Dinge nutzen die schon ein eigenes Tool haben "
                "(winner_track/winner_status/scaling_log/read_insights; instagram_trend/video_analysis; "
                "whatsapp_send/whatsapp_check) — Claude Code kennt diese sorgfaeltig getesteten Funktionen "
                "nicht und baut auf eigene Faust etwas Neues, Ungetestetes. Claude Code hat vollen, "
                "unbeaufsichtigten Zugriff auf dieses Projektverzeichnis (Dateien bearbeiten, Befehle "
                "ausfuehren). Sag vorher 'Ich lasse das kurz von Claude Code erledigen.' Kann bis zu "
                "zehn Minuten dauern, das ist normal."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
        },
        handler=_tool_claude_code_exec,
        speak_result=True,
        slow=True,
    ),
    "fanplace_snapshot": ToolSpec(
        schema={
            "name": "fanplace_snapshot",
            "description": "Aktuelle Fanplace-Zahlen abrufen: Umsatz (heute/woche/monat/all-time) und neue Abonnenten. Nutzen bei JEDER Frage zu Abonnenten, Umsatz, Einnahmen oder wie das Geschaeft laeuft. Sag vorher 'Ich schaue kurz auf Fanplace nach.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_fanplace_snapshot,
        speak_result=True,
        slow=True,
    ),
    "sltbio_snapshot": ToolSpec(
        schema={
            "name": "sltbio_snapshot",
            "description": "Aktuelle Klick-/Aufruf-Zahlen der Link-in-Bio-Seiten (SLT.bio) abrufen — echte API, keine geschaetzten Zahlen. Sag vorher 'Ich schaue kurz bei SLT.bio nach.'",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_sltbio_snapshot,
        speak_result=True,
        slow=True,
    ),
    "remember": ToolSpec(
        schema={
            "name": "remember",
            "description": (
                "Einen wichtigen Fakt dauerhaft im Langzeitgedaechtnis speichern, damit du dich auch nach "
                "einem Neustart daran erinnerst. SELBSTSTAENDIG nutzen bei allem was spaeter relevant sein "
                "koennte, ohne dass Ahmad 'merk dir das' sagen muss — auch Persoenliches, nicht nur Business."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["preferences", "people", "decisions", "business", "self", "general"],
                        "description": "preferences=Vorlieben/Arbeitsstil, people=wichtige Menschen, decisions=Entscheidungen+Gruende, business=Geschaeftsfakten, self=Selbstreflexion, general=Rest.",
                    },
                    "fact": {"type": "string", "description": "Ein klarer, kurzer Satz."},
                },
                "required": ["category", "fact"],
            },
        },
        handler=_tool_remember,
        speak_result=False,
    ),

    # --- Batch: Ziel-/Projekt-Tracking ---
    "track_goal": ToolSpec(
        schema={
            "name": "track_goal",
            "description": (
                "Merkt sich ein Ziel/Projekt das Ahmad gerade erwaehnt, dauerhaft. Jarvis fragt "
                "spaeter von selbst proaktiv nach, wenn laenger nichts Neues dazu kam. Nutzen wenn "
                "Ahmad eine Absicht/ein Vorhaben aeussert (z.B. 'ich will X bis Y schaffen')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "check_in_days": {"type": "integer", "description": "Alle wie viele Tage nachfragen falls nichts Neues kommt. Default drei."},
                },
                "required": ["description"],
            },
        },
        handler=_tool_track_goal,
        speak_result=False,
    ),
    "update_goal": ToolSpec(
        schema={
            "name": "update_goal",
            "description": (
                "Traegt ein Update zu einem bestehenden Ziel ein und/oder schliesst es ab. "
                "title_query muss nur ein Teil der urspruenglichen Zielbeschreibung treffen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title_query": {"type": "string"},
                    "note": {"type": "string", "description": "Optional, neuer Fortschritts-Stand."},
                    "status": {"type": "string", "enum": ["active", "done", "abandoned"], "description": "Optional, nur setzen wenn sich der Status aendert."},
                },
                "required": ["title_query"],
            },
        },
        handler=_tool_update_goal,
        speak_result=True,
    ),
    "list_goals": ToolSpec(
        schema={
            "name": "list_goals",
            "description": "Listet alle aktuell offenen Ziele/Projekte mit ihrem letzten Stand auf.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        handler=_tool_list_goals,
        speak_result=True,
    ),

    # --- Batch: Wissensgraph ---
    "remember_relation": ToolSpec(
        schema={
            "name": "remember_relation",
            "description": (
                "Haelt einen Zusammenhang zwischen zwei benannten Dingen (Person/Projekt/Entscheidung) "
                "strukturiert fest, z.B. 'Jerome arbeitet an Luna Vale' oder 'Trial-Reel-Format-Wechsel "
                "wurde entschieden wegen besserer US-Audience-Quote (betrifft Jerome)'. Nutze das "
                "SELBSTSTAENDIG, sobald so ein Zusammenhang im Gespraech erkennbar wird, genau wie beim "
                "REMEMBER-Tool fuer einzelne Fakten — nicht erst auf Nachfrage warten."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "from_name": {"type": "string"},
                    "from_type": {"type": "string", "enum": ["person", "project", "decision", "other"]},
                    "relation": {"type": "string", "description": "Kurzer, natuerlicher Text der die Verbindung beschreibt, z.B. 'arbeitet an' oder 'wurde entschieden wegen'."},
                    "to_name": {"type": "string"},
                    "to_type": {"type": "string", "enum": ["person", "project", "decision", "other"]},
                    "description": {"type": "string", "description": "Optional, zusaetzlicher Kontext/Grund."},
                },
                "required": ["from_name", "from_type", "relation", "to_name", "to_type"],
            },
        },
        handler=_tool_remember_relation,
        speak_result=False,
    ),
    "query_relations": ToolSpec(
        schema={
            "name": "query_relations",
            "description": (
                "Fragt den Wissensgraph zu einem benannten Ding ab (Person/Projekt/Entscheidung), z.B. "
                "'was haben wir bei Jerome zuletzt entschieden'. Gibt alle bekannten Zusammenhaenge dazu "
                "zurueck, neueste zuerst."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"entity_name": {"type": "string"}},
                "required": ["entity_name"],
            },
        },
        handler=_tool_query_relations,
        speak_result=True,
    ),
    "record_decision_outcome": ToolSpec(
        schema={
            "name": "record_decision_outcome",
            "description": (
                "Verknuepft eine bereits bekannte Entscheidung (vorher per remember_relation angelegt) "
                "mit ihrem spaeteren Ergebnis, z.B. 'das Ergebnis vom Trial-Reel-Format-Wechsel war eine "
                "bessere US-Audience-Quote'. Nutze das SELBSTSTAENDIG sobald Ahmad erzaehlt wie eine "
                "fruehere Entscheidung ausgegangen ist. decision_query muss nur einen Teil des "
                "urspruenglichen Entscheidungsnamens treffen."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "decision_query": {"type": "string"},
                    "outcome": {"type": "string", "description": "Was am Ende dabei rauskam."},
                    "status": {"type": "string", "enum": ["success", "failure", "mixed", "open"], "description": "Grobe Einordnung, Default mixed."},
                },
                "required": ["decision_query", "outcome"],
            },
        },
        handler=_tool_record_decision_outcome,
        speak_result=True,
    ),
    "list_decisions": ToolSpec(
        schema={
            "name": "list_decisions",
            "description": "Listet bekannte Entscheidungen mit Status und Ergebnis auf, optional gefiltert nach Status (success/failure/mixed/open). Nutze das bei Fragen wie 'was haben wir bisher entschieden' oder 'was hat sich bewaehrt'.",
            "input_schema": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["success", "failure", "mixed", "open"]}},
                "required": [],
            },
        },
        handler=_tool_list_decisions,
        speak_result=True,
    ),
}

TOOLS = [spec.schema for spec in TOOL_REGISTRY.values()]

SLOW_TOOL_FILLERS = {
    "screen": "Lassen Sie mich einen Blick auf Ihren Bildschirm werfen.",
    "read_insights": "Ich schaue in den Insights-Chat, einen Moment.",
    "news": "Ich schaue nach den aktuellen Nachrichten.",
    "instagram_trend": "Ich schaue kurz bei Instagram nach.",
    "research": "Ich recherchiere das kurz.",
    "browser_extract": "Ich werte die Seite strukturiert aus.",
    "video_analysis": "Ich schaue mir die letzten Videos an.",
    "gmail_check": "Ich schaue kurz ins Postfach.",
    "whatsapp_check": "Ich schaue kurz bei WhatsApp nach.",
    "read_chat": "Ich schaue kurz im Chat nach.",
    "jerome_check": "Ich schaue kurz ob Jerome geantwortet hat.",
    "jerome_brief": "Ich recherchiere das jetzt gruendlich fuer Jerome, das dauert ein paar Minuten.",
    "jerome_msg": "Ich schreibe Jerome das jetzt.",
    "claude_code_exec": "Ich lasse das kurz von Claude Code erledigen.",
    "fanplace_snapshot": "Ich schaue kurz auf Fanplace nach.",
    "sltbio_snapshot": "Ich schaue kurz bei SLT.bio nach.",
    "delegate_subagents": "Ich teile das auf und kuemmere mich parallel darum.",
    "simulate_decision": "Ich denke das kurz durch.",
}

# Legacy-Aktionsnamen, die durch ein natives Tool in TOOL_REGISTRY ersetzt
# wurden — ihre [ACTION:X]-Bullets in STATIC_INSTRUCTIONS werden fuer den
# native-Modus rausgefiltert (siehe NATIVE_STATIC_INSTRUCTIONS unten), sonst
# befolgt das Modell beide Konventionen gleichzeitig. Waechst mit jeder
# weiteren Rollout-Welle.
NATIVE_PORTED_ACTIONS = {
    "SCREEN", "OPEN", "CALENDARADD", "WHATSAPP", "READINSIGHTS",
    # Batch: Browser / Instagram
    "SEARCH", "BROWSE", "NEWS", "INSTAGRAM", "INSTAGRAMLINK", "RESEARCH",
    "VIDEOANALYSIS", "ADDCOMPETITOR", "SCREENCLICK", "SCREENTYPE",
    # Batch: Calendar / Gmail / WhatsApp
    "CALENDAR", "CALENDARDELETE", "GMAIL", "WHATSAPPCHECK", "READCHAT",
    # Batch: Sheets / Tracking / Jerome
    "WINNERTRACK", "WINNERSTATUS", "SCALINGLOG", "JEROMECHECK", "JEROMEBRIEF", "JEROMEMSG",
    # Batch: Rest
    "OPENAPP", "CLAUDECODE", "CLAUDECODE_EXEC", "FANPLACE", "SLTBIO", "REMEMBER", "SCREENHISTORY",
}


def _strip_ported_action_bullets(text: str, ported: set) -> str:
    for name in ported:
        text = re.sub(rf'^\[ACTION:{name}\].*\n?', '', text, flags=re.MULTILINE)
    return text


# Einmal beim Modul-Laden berechnet (nicht pro Request) — bleibt dadurch
# Byte-fuer-Byte stabil ueber Requests hinweg, Voraussetzung dafuer dass
# Anthropics Prompt-Caching darauf ueberhaupt greift.
NATIVE_STATIC_INSTRUCTIONS = _strip_ported_action_bullets(STATIC_INSTRUCTIONS, NATIVE_PORTED_ACTIONS)


CREDIT_ERROR_PATTERNS = ("credit balance", "insufficient_quota", "billing", "quota exceeded")


async def _handle_llm_error(session_id: str, ws: WebSocket, e: Exception):
    """Any Claude API error (billing, rate limit, network blip) must never
    crash the WebSocket — spoken as a normal Jarvis response instead, so the
    frontend just hears 'something's wrong' rather than silently dying with
    'Verbindung verloren' and forcing a reconnect.

    Distinguishes a credit/billing issue specifically (2026-08-06) — this
    happened for real earlier in the project and the generic 'connection
    problem, try again' message just had Ahmad retrying a dead key over and
    over instead of knowing to check the account."""
    print(f"  LLM ERROR: {e}", flush=True)
    if any(p in str(e).lower() for p in CREDIT_ERROR_PATTERNS):
        text = f"{USER_ADDRESS}, es sieht nach einem Guthaben-/Abrechnungsproblem bei Anthropic aus — bitte kurz das Konto pruefen, ein erneuter Versuch wird sonst wieder fehlschlagen."
    else:
        text = f"Entschuldigung, {USER_ADDRESS}, ich habe gerade ein Problem mit meiner Verbindung zu Claude. Versuchen Sie es gleich noch einmal."
    audio = await synthesize_speech(text)
    conversations[session_id].append({"role": "assistant", "content": text})
    memory.append_turn("assistant", text)
    await ws.send_json({
        "type": "response",
        "text": text,
        "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
    })


async def handle_activate(session_id: str, user_text: str, ws: WebSocket):
    """Full briefing in a single flow: weather/tasks (already refreshed) plus
    Fanplace and Gmail — gathered in parallel and spoken as one continuous
    update instead of separate action-by-action round trips.

    WhatsApp deliberately excluded (2026-08-05): bringing it to the front to
    screenshot it fought Chrome for focus on every single "Jarvis, lets go" —
    exactly the kind of unwanted foreground-stealing Ahmad asked to eliminate.
    Sending messages still opens WhatsApp on demand (that's unavoidable and
    brief); Ahmad can still explicitly ask "was ist neu bei WhatsApp" any time
    (ACTION:WHATSAPPCHECK) — just not proactively on every activate anymore.

    Also 2026-08-05: the full Fanplace/Mail/Instagram rundown now happens
    ONCE per day, not on every single activate — Ahmad's explicit call,
    repeating the same numbers every time he double-claps added noise
    without adding information. Every activate after the first one that day
    gets a short, natural greeting instead; the underlying data is still one
    ACTION away (FANPLACE/GMAIL/INSTAGRAM) whenever he actually asks."""
    conversations[session_id].append({"role": "user", "content": user_text})
    memory.append_turn("user", user_text)

    open_questions = memory.get_open_questions()
    pending_block = ""
    if open_questions:
        # format_open_questions() statt einer eigenen Kopie der Bullet-Liste
        # — dieselbe Quelle wie die passive Pro-Turn-Anzeige in
        # build_dynamic_block(), damit die [WICHTIG]-Markierung (siehe
        # memory.py) auch dort ankommt wo die Frage tatsaechlich
        # ausgesprochen wird, nicht nur im Hintergrund-Kontext.
        q_text = memory.format_open_questions()
        pending_block = f"\n\nOffene Fragen an Ahmad (stelle sie jetzt kurz, einmalig):\n{q_text}"
        # Nur die TATSAECHLICH gezeigten Fragen als gestellt markieren — bei
        # mehr als limit=5 offenen Fragen wuerde ein Aufruf ohne Argumente
        # (frueheres Verhalten) auch die nicht gezeigten als "asked"
        # markieren und sie faelschlich verschwinden lassen.
        memory.mark_questions_asked([q["question"] for q in open_questions])

    if memory.has_full_briefing_today():
        prompt = (
            "Ahmad hat heute bereits das volle Update bekommen (Fanplace/Mails/Instagram), das "
            "hier ist ein weiteres \"Jarvis, lets go\" am selben Tag — kein neues volles Briefing "
            "noetig, aber auch NICHT einfach nur trocken 'was brauchen Sie' fragen, das wirkt wie "
            "ein Systemfehler statt eine bewusste Entscheidung. Begruesse ihn kurz und ECHT lebendig: "
            "orientiere dich an der Uhrzeit oben (siehe AKTUELLE DATEN, Morgen/Mittag/Abend/Nacht "
            "klingen unterschiedlich, spaet nachts darf das auch mal angemerkt werden), passend zu "
            "deinem Charakter gerne mit einer trockenen, unterspielten Bemerkung — aber nutze NIEMALS "
            "zweimal hintereinander dieselbe Formulierung oder denselben Satzbau wie im bisherigen "
            "Gespraechsverlauf oben, das faellt sofort auf und wirkt mechanisch. Mach klar dass er "
            "direkt sagen kann was er braucht, aber jedes Mal mit anderen Worten. Wenn gerade etwas "
            "Konkretes ansteht (offene Frage, spaete/fruehe Uhrzeit, laengere Pause seit dem letzten "
            "Update) darfst du das statt einer generischen Floskel aufgreifen. Ein bis zwei Saetze, "
            "kein Frage-Antwort-Automatismus." + pending_block
        )
    else:
        results = await asyncio.gather(
            fanplace.get_snapshot(),
            gmail_tools.get_recent_emails_summary(),
            calendar_tools.get_today_events_brief(),
            slt_bio_tools.format_for_jarvis(),
            return_exceptions=True,
        )
        blocks = []
        for label, result in zip(("Fanplace", "Gmail", "Kalender heute", "SLT.bio (Link-in-Bio)"), results):
            text = f"ERROR: {result}" if isinstance(result, Exception) else result
            blocks.append(f"{label}:\n{text}")

        # Instagram: read the latest background-brain snapshot instead of a live
        # check here — live checks already run on their own cadence in the
        # background, no need to slow down every "activate" with a fresh browser pass.
        blocks.append(f"Instagram (letzter Stand):\n{instagram_tools.format_trend_summary()}")

        combined = "\n\n".join(blocks)
        prompt = (
            "Gib mir jetzt dein volles Update: Begruessung, Wetter, Aufgaben, "
            "Fanplace-Zahlen, neue Mails, heutige Termine, Instagram-Stand, SLT.bio-Klicks — alles in "
            "einem natuerlichen Fluss, aber KOMPAKT (jedes Wort kostet TTS-Geld) — Kernzahlen nennen, "
            "nicht jedes Detail ausformulieren.\n\n" + combined + pending_block
        )
        memory.mark_full_briefing_done()

    try:
        response = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=build_system_blocks(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        await _handle_llm_error(session_id, ws, e)
        return
    text = response.content[0].text
    text, _ = extract_action(text)  # strip a stray action tag if the model adds one
    print(f"  LLM raw (activate): {text[:200]}", flush=True)

    audio = await synthesize_speech(text)
    print(f"  Jarvis: {text[:80]}", flush=True)
    print(f"  Audio bytes: {len(audio)}", flush=True)
    conversations[session_id].append({"role": "assistant", "content": text})
    memory.append_turn("assistant", text)
    await ws.send_json({
        "type": "response",
        "text": text,
        "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
    })


MAX_TOOL_ROUNDS = 6  # Sicherheitsdeckel — heutige Ketten wie READINSIGHTS gehen bis ~4 Ebenen tief,
# auf 6 angehoben (Tier 3, Punkt 16, 2026-08-08) fuer echte Multi-Tab-Recherchen (mehrere Tabs
# oeffnen, vergleichen, dann handeln braucht in der Praxis mehr als 4 Runden)


def _is_dangling_tool_result(msg: dict) -> bool:
    """True wenn msg ein user-Turn ist, der NUR tool_result-Bloecke enthaelt
    (also die Antwort auf ein tool_use aus einer vorherigen assistant-
    Message ist)."""
    content = msg.get("content")
    return (
        msg.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    )


def _safe_recent_history(session_id: str, max_messages: int = 16) -> list:
    """Wie conversations[session_id][-max_messages:], aber schneidet niemals
    mitten in ein tool_use/tool_result-Paar hinein. Ein rohes [-16:] kann
    genau zwischen der assistant-Message mit dem tool_use und der
    folgenden user-Message mit dem tool_result landen — das verwaiste
    tool_result als erste Message im Fenster lehnt die Anthropic-API dann
    hart ab ('unexpected tool_use_id ... no corresponding tool_use block'),
    reproduzierbar beobachtet nach ein paar Turns mit Tool-Aufrufen. Statt
    das Fenster zu vergroessern (verschiebt das Problem nur), wird ein
    fuehrendes verwaistes tool_result einfach mit verworfen — sein
    zugehoeriges tool_use ist ohnehin schon ausserhalb des Fensters."""
    history = conversations[session_id][-max_messages:]
    while history and _is_dangling_tool_result(history[0]):
        history = history[1:]
    return history


async def _push_live_event(text: str) -> bool:
    """Broadcast ein unaufgefordertes background_brain-Finding an ALLE
    aktuell verbundenen, GERADE NICHT belegten Sessions (SESSION_BUSY) —
    nie in einen laufenden Turn hineinreden. Nutzt denselben Wire-Vertrag
    wie _speak() (nie ein leeres Audio-Feld, das liest das Frontend als
    'Gespraech vorbei'), plus ein zusaetzliches proactive:true, damit das
    Frontend das optisch/akustisch von einer normalen Antwort unterscheiden
    kann. Synthetisiert TTS EINMAL, verteilt dasselbe Audio an alle
    passenden Verbindungen statt pro Verbindung neu zu synthetisieren."""
    eligible = [sid for sid in CONNECTIONS if sid not in SESSION_BUSY]
    if not text.strip() or not eligible:
        return False
    audio = await synthesize_speech(text)
    audio_b64 = base64.b64encode(audio).decode("utf-8") if audio else ""
    if not audio_b64:
        return False
    frame = {"type": "response", "text": text, "audio": audio_b64, "proactive": True}
    delivered = False
    for session_id in eligible:
        ws = CONNECTIONS.get(session_id)
        if ws is None:
            continue
        try:
            conversations.setdefault(session_id, []).append({"role": "assistant", "content": text})
            await ws.send_json(frame)
            delivered = True
        except Exception as e:
            print(f"[live_event] Push an {session_id} fehlgeschlagen: {e}", flush=True)
    if delivered:
        memory.append_turn("assistant", text)
    return delivered


async def _poll_live_events_once():
    pending = memory.get_pending_live_events()
    if not pending:
        return
    now = time.time()
    delivered_ids, stale_ids = [], []
    for entry in pending:
        age = now - entry.get("created_epoch", now)
        if age > memory.LIVE_EVENT_MAX_AGE_SECONDS:
            stale_ids.append(entry["id"])
            continue
        if not CONNECTIONS:
            continue  # bleibt pending, naechster Poll versucht es erneut
        if await _push_live_event(entry["text"]):
            delivered_ids.append(entry["id"])
        # Zustellung fehlgeschlagen: bewusst NICHT resolven, naechster Poll
        # versucht es erneut — begrenzt durch dieselbe Staleness-Grenze,
        # kein Retry-Sturm moeglich (nur ein kleiner lokaler Datei-Read alle
        # LIVE_EVENT_POLL_SECONDS).
    if delivered_ids:
        memory.resolve_live_events(delivered_ids, "delivered")
    if stale_ids:
        memory.resolve_live_events(stale_ids, "skipped_stale")


async def _live_events_poll_loop():
    while True:
        try:
            await _poll_live_events_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[live_events] Poll-Fehler (ignoriert): {e}", flush=True)
        await asyncio.sleep(LIVE_EVENT_POLL_SECONDS)


async def _speak(session_id: str, ws: WebSocket, text: str):
    """Ein Textblock -> TTS -> ein WS-Response-Frame. Wird sowohl fuer Text
    VOR einem Tool-Call als auch fuer die finale Antwort NACH einem
    Tool-Ergebnis genutzt — ersetzt damit sowohl das heutige spoken_text
    als auch den separaten Summarizer-Call mit einem Mechanismus."""
    if not text.strip():
        return
    audio = await synthesize_speech(text)
    print(f"  Jarvis: {text[:80]}", flush=True)
    print(f"  Audio bytes: {len(audio)}", flush=True)
    conversations[session_id].append({"role": "assistant", "content": text})
    memory.append_turn("assistant", text)
    await ws.send_json({
        "type": "response",
        "text": text,
        "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
    })


async def _run_tool(block) -> dict:
    spec = TOOL_REGISTRY.get(block.name)
    if spec is None:
        return {"tool_use_id": block.id, "content": f"ERROR: unbekanntes Tool '{block.name}'", "is_error": True}
    try:
        result = await spec.handler(block.input)
    except Exception as e:
        print(f"  Tool error ({block.name}): {e}", flush=True)
        result = f"ERROR: {e}"
    return {"tool_use_id": block.id, "content": str(result), "is_error": str(result).startswith("ERROR:")}


async def process_message_native(session_id: str, user_text: str, ws: WebSocket):
    """Nativer tool_use/tool_result-Loop — Ersatz fuer den [ACTION:X]-Regex-
    Trick, siehe Plan 'Nativer Tool-Use-Loop fuer server.py'. Nur hinter
    USE_NATIVE_TOOLS erreichbar; der Legacy-Pfad bleibt unveraendert."""
    if session_id not in conversations:
        conversations[session_id] = []

    trigger_text = user_text.lower()
    if "lets go" in trigger_text or "let's go" in trigger_text:
        refresh_data()
        await handle_activate(session_id, user_text, ws)
        return

    conversations[session_id].append({"role": "user", "content": user_text})
    memory.append_turn("user", user_text)

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            response = await ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                system=build_system_blocks(native=True, query=user_text),
                tools=TOOLS,
                messages=_safe_recent_history(session_id),
            )
        except Exception as e:
            await _handle_llm_error(session_id, ws, e)
            return

        text_blocks = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        print(
            f"  LLM raw (native, round {round_num}): text={text_blocks} "
            f"tools={[b.name for b in tool_use_blocks]}",
            flush=True,
        )

        for t in text_blocks:
            await _speak(session_id, ws, t)

        # response.content selbst (nicht die Textstrings) muss unveraendert
        # zurueck in die history — die tool_use-Bloecke tragen die IDs, die
        # das folgende tool_result per tool_use_id referenzieren muss.
        conversations[session_id].append({"role": "assistant", "content": response.content})

        if not tool_use_blocks:
            return  # (a) einfache Antwort ohne Tool-Aufruf — fertig

        if not text_blocks:
            for b in tool_use_blocks:
                spec = TOOL_REGISTRY.get(b.name)
                if spec and spec.slow:
                    filler = SLOW_TOOL_FILLERS.get(b.name)
                    if filler:
                        await _speak(session_id, ws, filler)
                    break  # ein Fuellsatz reicht auch bei mehreren langsamen Calls

        results = await asyncio.gather(*(_run_tool(b) for b in tool_use_blocks))
        for r in results:
            print(f"  Tool result: {str(r['content'])[:200]}", flush=True)

        tool_result_content = [
            {
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": r["content"],
                "is_error": r["is_error"],
            }
            for r in results
        ]
        conversations[session_id].append({"role": "user", "content": tool_result_content})

        any_speak = any(
            (TOOL_REGISTRY.get(b.name) and TOOL_REGISTRY[b.name].speak_result) for b in tool_use_blocks
        )
        any_failed = any(r["is_error"] for r in results)
        if not any_speak and not any_failed:
            return  # fire-and-forget, alles gut gelaufen — still bleiben, wie heute

    # MAX_TOOL_ROUNDS erreicht, ohne dass Claude sauber ohne weiteren
    # tool_use geantwortet hat — sichtbar loggen (Runaway-Warnsignal) und dem
    # Nutzer kurz Bescheid geben statt endlos weiterzulaufen.
    print(f"  [WARN] MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}) erreicht, Turn abgebrochen.", flush=True)
    await _speak(session_id, ws, f"Das dauert laenger als erwartet, {USER_ADDRESS} — ich melde mich, sobald ich fertig bin.")


async def process_message(session_id: str, user_text: str, ws: WebSocket):
    # SESSION_BUSY guards against _push_live_event() talking over an
    # in-progress turn — see the live-events poll loop.
    SESSION_BUSY.add(session_id)
    try:
        if USE_NATIVE_TOOLS:
            await process_message_native(session_id, user_text, ws)
        else:
            await process_message_legacy(session_id, user_text, ws)
    finally:
        SESSION_BUSY.discard(session_id)


async def process_message_legacy(session_id: str, user_text: str, ws: WebSocket):
    """Process message and send responses via WebSocket."""
    if session_id not in conversations:
        conversations[session_id] = []

    trigger_text = user_text.lower()
    if "lets go" in trigger_text or "let's go" in trigger_text:
        refresh_data()
        await handle_activate(session_id, user_text, ws)
        return

    conversations[session_id].append({"role": "user", "content": user_text})
    memory.append_turn("user", user_text)
    history = conversations[session_id][-16:]

    # LLM call
    try:
        response = await ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=build_system_blocks(query=user_text),
            messages=history,
        )
    except Exception as e:
        await _handle_llm_error(session_id, ws, e)
        return
    reply = response.content[0].text
    print(f"  LLM raw: {reply[:200]}", flush=True)
    spoken_text, action = extract_action(reply)

    # Speak the main response immediately
    if spoken_text:
        audio = await synthesize_speech(spoken_text)
        print(f"  Jarvis: {spoken_text[:80]}", flush=True)
        print(f"  Audio bytes: {len(audio)}", flush=True)
        conversations[session_id].append({"role": "assistant", "content": spoken_text})
        memory.append_turn("assistant", spoken_text)
        await ws.send_json({
            "type": "response",
            "text": spoken_text,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        })

    # Execute action if any
    if action:
        print(f"  Action: {action['type']} -> {action['payload'][:100]}", flush=True)

        # Quick voice feedback for SCREEN so user knows Jarvis is working
        if action["type"] == "SCREEN":
            hint = "Lassen Sie mich einen Blick auf Ihren Bildschirm werfen."
            hint_audio = await synthesize_speech(hint)
            await ws.send_json({
                "type": "response",
                "text": hint,
                "audio": base64.b64encode(hint_audio).decode("utf-8") if hint_audio else "",
            })

        try:
            action_result = await execute_action(action)
            print(f"  Result: {action_result}", flush=True)
        except Exception as e:
            print(f"  Action error: {e}", flush=True)
            action_result = f"ERROR: {e}"

        failed = action_result.startswith("ERROR:")

        if action["type"] in ("OPEN", "OPENAPP", "WHATSAPP", "REMEMBER", "INSTAGRAMLINK", "SCREENCLICK", "SCREENTYPE"):
            # Jarvis already spoke a confirmation before the action tag — on success
            # nothing more to say. On failure, say so instead of staying silent.
            if failed:
                summary = f"Das hat leider nicht funktioniert, {USER_ADDRESS}: {action_result[len('ERROR:'):].strip()}"
                audio2 = await synthesize_speech(summary)
                conversations[session_id].append({"role": "assistant", "content": summary})
                memory.append_turn("assistant", summary)
                await ws.send_json({
                    "type": "response",
                    "text": summary,
                    "audio": base64.b64encode(audio2).decode("utf-8") if audio2 else "",
                })
            return

        # SEARCH, BROWSE, SCREEN, NEWS, CLAUDECODE, FANPLACE — summarize results
        if action_result and not failed:
            try:
                summary_resp = await ai.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=130,
                    system=f"Du bist Jarvis. Fasse die folgenden Informationen auf Deutsch zusammen — EIN Satz, maximal ZWEI, nur das Wichtigste (jedes Wort kostet echtes TTS-Geld). Im Jarvis-Stil. Sprich den Nutzer als {USER_ADDRESS} an. KEINE Tags in eckigen Klammern. KEINE ACTION-Tags. Schreibe alle Zahlen, Geldbetraege und Prozentangaben komplett in Worten ausgeschrieben (z.B. \"ein Dollar achtzig\" statt \"$1.80\"), niemals als Ziffern oder Symbole — der Text wird per Text-zu-Sprache vorgelesen.",
                    messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
                )
                summary = summary_resp.content[0].text
                summary, _ = extract_action(summary)
            except Exception as e:
                print(f"  LLM ERROR (summarize): {e}", flush=True)
                summary = f"Ich habe die Daten, konnte sie gerade aber nicht zusammenfassen, {USER_ADDRESS} — Verbindungsproblem mit Claude."
        else:
            summary = f"Das hat leider nicht funktioniert, {USER_ADDRESS}."

        audio2 = await synthesize_speech(summary)
        conversations[session_id].append({"role": "assistant", "content": summary})
        memory.append_turn("assistant", summary)
        await ws.send_json({
            "type": "response",
            "text": summary,
            "audio": base64.b64encode(audio2).decode("utf-8") if audio2 else "",
        })


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = str(id(ws))
    CONNECTIONS[session_id] = ws
    print(f"[jarvis] Client connected", flush=True)

    try:
        while True:
            data = await ws.receive_json()
            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            print(f"  You:    {user_text}", flush=True)
            await process_message(session_id, user_text, ws)

    except WebSocketDisconnect:
        pass
    finally:
        # finally (not just the except above) so CONNECTIONS never leaks a
        # dead entry on any other kind of disconnect/exception either —
        # bundled fix, conversations.pop had the same gap before this.
        conversations.pop(session_id, None)
        CONNECTIONS.pop(session_id, None)


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


@app.get("/mobile")
async def serve_mobile():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "mobile.html"))


@app.post("/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """Roadmap Punkt 17 — Push-to-Talk vom Handy: ElevenLabs Speech-to-Text
    statt der Web-SpeechRecognition-API (auf iOS Safari nicht zuverlaessig).
    Gleicher API-Key wie fuer TTS, kein neuer Anbieter noetig."""
    audio_bytes = await audio.read()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                data={"model_id": "scribe_v1"},
                files={"file": (audio.filename or "recording.webm", audio_bytes, audio.content_type or "audio/webm")},
            )
            r.raise_for_status()
            data = r.json()
        return {"text": data.get("text", "")}
    except Exception as e:
        print(f"[transcribe] Fehler: {e}", flush=True)
        return {"text": "", "error": str(e)}


@app.get("/push/vapid-public-key")
async def push_vapid_public_key():
    return {"key": push_notifications.get_vapid_public_key()}


@app.post("/push/subscribe")
async def push_subscribe(subscription: dict):
    push_notifications.add_subscription(subscription)
    return {"ok": True}


class _FakeWebSocket:
    """Minimaler WebSocket-Ersatz fuer /shortcut — process_message() braucht
    ein ws-artiges Objekt, faengt hier nur den finalen Antworttext ab statt
    ihn wirklich ueber einen Socket zu senden. Jede unerwartete Methode
    (z.B. close()) wird als No-Op abgefangen statt einen Fehler zu werfen."""
    def __init__(self):
        self.captured_text = []

    async def send_json(self, data):
        if isinstance(data, dict) and data.get("type") == "response" and data.get("text"):
            self.captured_text.append(data["text"])

    async def _noop(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return self._noop


@app.post("/shortcut")
async def shortcut_endpoint(payload: dict):
    """Roadmap Punkt 17 — Webhook fuer eine iOS-Shortcuts-Automation
    ("Hey Siri, Jarvis"). Laeuft durch dieselbe process_message()-Pipeline
    wie WebSocket-Nachrichten (gleiches Gehirn, gleiches Gedaechtnis),
    liefert nur Text zurueck statt Audio (Shortcuts kann das selbst per
    Siri vorlesen lassen)."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"error": "text fehlt"}
    session_id = "shortcut"
    conversations.setdefault(session_id, [])
    fake_ws = _FakeWebSocket()
    await process_message(session_id, text, fake_ws)
    return {"text": " ".join(fake_ws.captured_text).strip()}


@app.post("/internal/push_event")
async def internal_push_event(payload: dict, request: Request):
    """Roadmap Punkt 3 — Event-Bus: background_brain.py (ein SEPARATER
    Prozess) ruft das direkt auf statt nur auf den 5-Sekunden-Datei-Poll
    (_live_events_poll_loop unten) zu warten, senkt die Zustellzeit im
    Normalfall auf nahezu sofort. Der Datei-Weg bleibt zusaetzlich bestehen
    als Fallback (z.B. wenn server.py gerade neu startet). Nur von localhost
    erreichbar (anders als mac_actuator.py, das an die Tailscale-IP bindet,
    bindet server.py an 0.0.0.0 — dieser eine sensible interne Endpunkt
    braucht daher einen eigenen Schutz statt sich auf die Firewall zu
    verlassen, sonst koennte theoretisch jeder von aussen Ahmad beliebigen
    Text vorsprechen lassen)."""
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        return JSONResponse(status_code=403, content={"error": "nur von localhost erreichbar"})
    text = (payload.get("text") or "").strip()
    if not text:
        return {"delivered": False, "reason": "text fehlt"}
    delivered = await _push_live_event(text)
    return {"delivered": delivered}


if __name__ == "__main__":
    import uvicorn
    print("=" * 50, flush=True)
    print("  J.A.R.V.I.S. V2 Server", flush=True)
    print(f"  http://localhost:8340", flush=True)
    print("=" * 50, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8340, ws_max_size=16 * 1024 * 1024)
