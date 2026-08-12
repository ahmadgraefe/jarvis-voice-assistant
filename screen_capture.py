"""
Jarvis V2 — Screen Capture
Takes screenshots and describes them via Claude Vision. Zwei Modi:
- describe_screen(): auf Zuruf ("was siehst du grad", SCREEN-Aktion).
- describe_screen_for_awareness(): periodischer Hintergrund-Pass (Roadmap
  Punkt 19, kontinuierliches Screen-Bewusstsein) — Ahmads bewusste
  Entscheidung nach Rueckfrage: nur die TEXT-Beschreibung wird je
  gespeichert, nie das Bild selbst; bestimmte Apps (Passwort-Manager/
  Banking) werden uebersprungen; Beschreibung bewusst privacy-zurueckhaltend
  formuliert (siehe Prompt unten).
"""

import asyncio
import base64
import io
import os
import tempfile

from PIL import ImageGrab


def capture_screen() -> bytes:
    """Capture the entire screen, return PNG bytes."""
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Grace-Fenster NACH dem osascript-Ende, in dem noch auf die Datei gewartet
# wird (siehe _wait_for_complete_file). Bewusst kurz: im Normalfall ist die
# Datei sofort da, das Fenster wird nur im Fehlerfall ueberhaupt ausgereizt.
# 2 Versuche * (10s osascript + 5s Grace) = 30s, bleibt unter dem read=60s
# des HTTP-Clients (mac_actuator_client._TIMEOUT), der diesen Aufruf umgibt.
_SETTLE_TIMEOUT = 5.0


async def _wait_for_complete_file(path: str, timeout: float) -> bytes:
    """Wartet bis die Datei existiert UND ihre Groesse zwei Messungen lang
    gleich bleibt, dann liest sie.

    Warum ueberhaupt warten, obwohl das AppleScript unten schon
    'repeat while busy of jarvisTab' macht: Terminal setzt 'busy' erst
    verzoegert, nachdem 'do script' zurueckkam — die Schleife kann darum
    sofort durchlaufen, waehrend 'screencapture' noch gar nicht angefangen
    hat. Vorher wurde direkt danach EINMAL geprueft, ob die Datei existiert,
    und bei Fehlen sofort abgebrochen; jedes bisschen Langsamkeit von
    screencapture (z.B. bei schlafendem/aufwachendem Display, genau die
    Tageszeiten der beobachteten Ausfaelle) wurde so zum harten Fehler,
    obwohl der Screenshot Millisekunden spaeter da war.

    Die Groessen-Stabilitaet ist der zweite Teil: eine bereits existierende,
    aber noch nicht fertig geschriebene PNG-Datei war nach der alten Pruefung
    ('existiert und > 0 Bytes') sofort gut genug und wurde abgeschnitten
    eingelesen — ein solches Bild kommt bei Claude Vision als teilweise/ganz
    schwarz an, statt als erkennbarer Fehler."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_size = -1
    while True:
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size > 0 and size == last_size:
            with open(path, "rb") as f:
                return f.read()
        last_size = size
        if loop.time() >= deadline:
            raise RuntimeError(
                "Screenshot ueber Terminal.app kam nicht rechtzeitig an"
                + ("" if size == 0 else f" (Datei blieb unvollstaendig bei {size} Bytes)")
            )
        await asyncio.sleep(0.2)


async def _run_terminal_screencapture(timeout: float, reuse_window: bool) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    os.remove(path)  # screencapture legt die Datei frisch an, sonst nicht von "noch nicht fertig" unterscheidbar

    if reuse_window:
        # EIN wiederverwendetes, per Custom-Title identifizierbares Terminal-
        # Fenster statt bei jeder Aufnahme ein neues zu oeffnen (gleiches
        # "Fenster wiederverwenden statt stapeln"-Prinzip wie launch-session.sh
        # schon bei Chrome nutzt). Ausprobiert und verworfen: das Fenster nach
        # jeder Aufnahme per 'close' zu schliessen — Terminal meldet Erfolg,
        # das Fenster bleibt aber live bestaetigt trotzdem offen.
        script = f'''
        tell application "Terminal"
            set foundWindow to missing value
            repeat with w in windows
                try
                    if (custom title of tab 1 of w) is "jarvis-capture" then
                        set foundWindow to w
                        exit repeat
                    end if
                end try
            end repeat
            if foundWindow is missing value then
                set jarvisTab to do script "screencapture -x -T 0 {path}"
                set custom title of jarvisTab to "jarvis-capture"
            else
                set jarvisTab to do script "screencapture -x -T 0 {path}" in foundWindow
            end if
            repeat while busy of jarvisTab
                delay 0.2
            end repeat
        end tell
        '''
    else:
        # Fallback-Zweig (siehe Aufrufer unten): kein Reuse-Lookup, immer ein
        # brandneues Fenster ohne Custom-Title. Faengt den Fall auf, dass das
        # sonst wiederverwendete "jarvis-capture"-Fenster in einen kaputten
        # Zustand geraten ist (z.B. von Ahmad manuell geschlossen, Terminal
        # neu gestartet) -- ein 'do script in foundWindow' auf eine ungueltige
        # Fensterreferenz haengt sonst bis zum Timeout, ohne je etwas zu tun.
        script = f'''
        tell application "Terminal"
            set jarvisTab to do script "screencapture -x -T 0 {path}"
            repeat while busy of jarvisTab
                delay 0.2
            end repeat
        end tell
        '''

    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        return await _wait_for_complete_file(path, _SETTLE_TIMEOUT)
    finally:
        # Auch im Fehlerfall aufraeumen: bisher blieb bei jedem Fehlschlag eine
        # (leere oder halbe) PNG-Datei in /var/folders liegen, weil os.remove
        # erst nach der Pruefung kam.
        try:
            os.remove(path)
        except OSError:
            pass


async def _capture_screen_via_terminal(timeout: float = 10.0) -> bytes:
    """macOS' 'Bildschirmaufnahme'-Berechtigung ist strenger als Bedienungs-
    hilfen/Automation: ein von launchd gestarteter Hintergrundprozess (wie
    mac_actuator.py) kann sie weder automatisch bekommen noch ueberhaupt
    einen Eintrag in den Systemeinstellungen erzeugen, den man nachtraeglich
    freischalten koennte (live getestet, bestaetigt) — ImageGrab.grab()
    schlaegt darum in diesem Kontext IMMER fehl. Terminal.app hat die
    Berechtigung bei Ahmad bereits (bestaetigt). Fuehrt darum 'screencapture'
    ueber EINEN einmaligen, kurzen Terminal-Befehl aus (kein Dauerprozess,
    keine KeepAlive-Neustart-Schleife wie beim frueheren com.jarvis.brain-
    Problem — nur dieser eine schnelle Screenshot, dann ist Terminal fertig).

    Live beobachtet (2026-08-11, Ahmad meldete wiederholt "kann den
    Bildschirm nicht sehen"): das wiederverwendete Fenster kann in einen
    haengenden Zustand geraten und den Timeout reissen lassen. EIN
    automatischer Fallback-Versuch mit einem komplett neuen Fenster faengt
    das auf, statt dass Jarvis dauerhaft blind bleibt bis jemand manuell
    eingreift."""
    try:
        return await _run_terminal_screencapture(timeout, reuse_window=True)
    except RuntimeError:
        return await _run_terminal_screencapture(timeout, reuse_window=False)


async def _get_frontmost_app_name() -> str:
    """Gleiches AppleScript-Muster wie app_control.py's Frontmost-Check bei
    send_whatsapp. Leerer String wenn nicht ermittelbar (dann wird NICHT
    ausgeschlossen — im Zweifel lieber erfassen als durch einen stillen
    Fehler dauerhaft blind fuer alles zu werden)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return out.decode().strip()
    except Exception:
        return ""


async def describe_screen(anthropic_client) -> str:
    """Capture screen and describe it using Claude Vision."""
    png_bytes = await _capture_screen_via_terminal()
    b64 = base64.b64encode(png_bytes).decode("utf-8")

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": "Beschreibe kurz auf Deutsch was auf diesem Bildschirm zu sehen ist. Maximal 2-3 Saetze. Nenne die wichtigsten offenen Programme und Inhalte.",
                },
            ],
        }],
    )
    return response.content[0].text


async def describe_screen_for_awareness(anthropic_client, excluded_apps: list) -> dict:
    """Periodischer Hintergrund-Pass. Prueft zuerst die Vordergrund-App gegen
    excluded_apps (Teilstring, gross-/kleinschreibungs-unabhaengig) — bei
    Treffer wird GAR KEIN Screenshot gemacht. Sonst: Screenshot -> Vision ->
    NUR der Text wird zurueckgegeben, das Bild existiert ab hier nirgends
    mehr (Ahmads ausdrueckliche Wahl). Der Prompt ist bewusst zurueckhaltend:
    thematische Zusammenfassung statt woertlicher Wiedergabe von allem was
    sichtbar ist, insbesondere niemals Zahlen/Codes die wie Konto-/Karten-
    nummern oder Passwoerter aussehen."""
    frontmost = await _get_frontmost_app_name()
    if frontmost:
        frontmost_lower = frontmost.lower()
        for excluded in excluded_apps or []:
            if excluded.lower() in frontmost_lower:
                return {"skipped": True, "reason": f"Vordergrund-App '{frontmost}' ist ausgeschlossen"}

    png_bytes = await _capture_screen_via_terminal()
    b64 = base64.b64encode(png_bytes).decode("utf-8")

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Fasse in EINEM kurzen Satz auf Deutsch THEMATISCH zusammen, womit Ahmad sich "
                        "gerade beschaeftigt (z.B. 'arbeitet am Jarvis-Code', 'liest E-Mails', 'schaut "
                        "Instagram-Analytics an', 'schreibt eine Nachricht'). NIEMALS sichtbare Zahlen, "
                        "Codes, Konto-/Kartennummern, Passwoerter, oder den woertlichen Inhalt privater "
                        "Nachrichten wiedergeben, auch wenn sie im Bild zu sehen sind — nur die Art der "
                        "Taetigkeit, keine Details. Wenn nichts Eindeutiges zu erkennen ist, sag das kurz."
                    ),
                },
            ],
        }],
    )
    return {"skipped": False, "text": response.content[0].text.strip()}


# Server-Migration (Hetzner): siehe app_control.py, gleiches Prinzip. Die
# echte Bildschirm-Erfassung existiert nur auf dem Mac.
if os.environ.get("JARVIS_ROLE") == "server":
    from mac_actuator_client import describe_screen, describe_screen_for_awareness  # noqa: E402,F811
