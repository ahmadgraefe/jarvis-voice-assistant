"""
Headless WS-Test fuer den nativen Tool-Use-Loop (process_message_native).

Verbindet sich direkt mit dem WebSocket, exakt wie frontend/main.js es tut
({"text": "..."} senden, {"type": "response"/"status", ...} empfangen), und
prueft den Vertrag automatisch — OHNE Browser/Mikro/Lautsprecher noetig.

WICHTIG: Laeuft NUR gegen eine lokale Test-Instanz mit gesetztem
JARVIS_NATIVE_TOOLS=1, niemals gegen die Instanz, die Ahmad im Alltag nutzt.
Standard-Turns sind rein lesend (SCREEN, READINSIGHTS ueber "lets go" wird
hier NICHT getriggert). OPEN/CALENDARADD/WHATSAPP haben echte Nebenwirkungen
(oeffnet einen Browser-Tab / schreibt einen echten Kalendereintrag / sendet
eine echte WhatsApp-Nachricht) und laufen nur mit --include-mutating, gegen
einen expliziten Test-Kontakt/ein Test-Datum.

Nutzung:
    JARVIS_NATIVE_TOOLS=1 python3 server.py &   # lokale Test-Instanz
    python3 scripts/test_native_tools_ws.py
    python3 scripts/test_native_tools_ws.py --include-mutating --contact "Test Kontakt"
"""

import argparse
import asyncio
import base64
import json
import sys

import websockets

WS_URL = "ws://localhost:8340/ws"
IDLE_SECONDS = 2.5  # keine neue Nachricht mehr -> Turn gilt als abgeschlossen


async def _drain_turn(ws) -> list:
    """Sammelt alle Frames eines Turns, bis IDLE_SECONDS lang nichts mehr kommt."""
    frames = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=IDLE_SECONDS)
        except asyncio.TimeoutError:
            break
        frame = json.loads(raw)
        frames.append(frame)
    return frames


def _check_protocol(frames: list, label: str) -> list:
    """Grundlegende Vertragspruefung — gibt eine Liste von Problemen zurueck (leer = ok)."""
    problems = []
    response_frames = [f for f in frames if f.get("type") == "response"]

    if not frames:
        problems.append("keine einzige Server-Antwort erhalten (Timeout)")

    for i, f in enumerate(frames):
        if f.get("type") not in ("response", "status"):
            problems.append(f"Frame {i} hat unbekannten/fehlenden 'type': {f.get('type')!r}")

    for i, f in enumerate(response_frames):
        audio = f.get("audio", "")
        is_last = f is response_frames[-1]
        if not audio and not is_last:
            problems.append(
                f"response-Frame {i} hat leeres audio, ist aber NICHT das letzte response-Frame "
                f"des Turns — das wuerde das echte Frontend faelschlich als 'Turn vorbei' werten."
            )
        if audio:
            try:
                decoded = base64.b64decode(audio)
                if len(decoded) < 100:
                    problems.append(f"response-Frame {i}: audio decoded auf verdaechtig wenige Bytes ({len(decoded)})")
            except Exception as e:
                problems.append(f"response-Frame {i}: audio ist kein gueltiges base64 ({e})")

    return problems


async def run_turn(ws, text: str, label: str) -> bool:
    await ws.send(json.dumps({"text": text}))
    frames = await _drain_turn(ws)
    problems = _check_protocol(frames, label)

    n_response = sum(1 for f in frames if f.get("type") == "response")
    n_status = sum(1 for f in frames if f.get("type") == "status")
    print(f"\n[{label}] '{text}'")
    print(f"  -> {n_response} response-Frame(s), {n_status} status-Frame(s)")
    for f in frames:
        if f.get("type") == "response":
            has_audio = "audio" if f.get("audio") else "KEIN AUDIO"
            print(f"     response: {f.get('text', '')[:100]!r} [{has_audio}]")

    if problems:
        print("  PROBLEME:")
        for p in problems:
            print(f"    - {p}")
        return False
    print("  OK")
    return True


READ_ONLY_TURNS = [
    ("Wie ist das Wetter?", "text_only (kein Tool)"),
    ("Wie sieht mein Bildschirm gerade aus?", "single_tool (screen, slow+filler)"),
    ("Lies die Insights aus dem Chat mit mir selbst.", "single_tool (read_insights, slow, moeglicherweise mehrstufig)"),
]


def mutating_turns(contact: str, test_date: str) -> list:
    return [
        (f"Oeffne https://example.com im Browser.", "fire_and_forget (open_url)"),
        (f"Schreib {contact} per WhatsApp: Test-Nachricht vom automatisierten Skript, bitte ignorieren.", "fire_and_forget (whatsapp_send)"),
        (f"Leg einen Termin an: Testtermin am {test_date} von 09:00 bis 09:15.", "summarized (calendar_add)"),
    ]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-mutating", action="store_true",
                         help="Zusaetzlich OPEN/WHATSAPP/CALENDARADD testen (echte Nebenwirkungen)")
    parser.add_argument("--contact", default="", help="Test-Kontakt fuer die WhatsApp-Mutating-Probe")
    parser.add_argument("--date", default="2099-01-01", help="Test-Datum (JJJJ-MM-TT) fuer die Kalender-Mutating-Probe")
    args = parser.parse_args()

    if args.include_mutating and not args.contact:
        print("FEHLER: --include-mutating braucht --contact <Testkontakt>", file=sys.stderr)
        sys.exit(1)

    turns = list(READ_ONLY_TURNS)
    if args.include_mutating:
        turns += mutating_turns(args.contact, args.date)

    print(f"Verbinde mit {WS_URL} ...")
    async with websockets.connect(WS_URL) as ws:
        # Erster Frame nach Connect ist typischerweise die automatische
        # "lets go"-Begruessung, falls das Test-Script selbst wie ein
        # frisches Frontend auftritt — hier bewusst NICHT gesendet, wir
        # wollen gezielte Turns, kein Briefing im Test.
        results = []
        for text, label in turns:
            ok = await run_turn(ws, text, label)
            results.append((label, ok))

    print("\n" + "=" * 60)
    failed = [l for l, ok in results if not ok]
    if failed:
        print(f"FEHLGESCHLAGEN ({len(failed)}/{len(results)}): {', '.join(failed)}")
        sys.exit(1)
    print(f"ALLE {len(results)} TURNS OK")


if __name__ == "__main__":
    asyncio.run(main())
