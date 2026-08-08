"""
Headless Test fuer den Live-Event-Kanal (background_brain.py -> server.py,
mitten in ein bereits offenes Gespraech hinein).

Verbindet sich mit dem WebSocket wie das echte Frontend, sendet aber
BEWUSST NICHTS. Stattdessen wird direkt ein Test-Eintrag in
memory/live_events.jsonl geschrieben (simuliert background_brain._alert()),
und geprueft ob innerhalb eines Poll-Zyklus ein unaufgeforderter
response-Frame mit echtem Audio ankommt.

WICHTIG: Laeuft nur gegen eine lokale Test-Instanz, niemals gegen die
Instanz die Ahmad im Alltag nutzt. Loest einen echten ElevenLabs-TTS-Call aus.

Nutzung:
    python3 server.py &   # lokale Test-Instanz
    python3 scripts/test_live_events_ws.py
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

import memory

WS_URL = "ws://localhost:8340/ws"


async def wait_for_push(ws, timeout_s: float, expect_text: str = None):
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None
    frame = json.loads(raw)
    if expect_text is not None and frame.get("text") != expect_text:
        print(f"  WARNUNG: Text weicht ab. Erwartet {expect_text!r}, bekommen {frame.get('text')!r}")
    return frame


async def test_delivery():
    print("=== 1) Normale Zustellung (verbundene Session, frischer Eintrag) ===")
    text = f"TEST-LIVE-EVENT {int(time.time())}"
    async with websockets.connect(WS_URL) as ws:
        memory.add_live_event(text)
        frame = await wait_for_push(ws, timeout_s=15, expect_text=text)
        if frame is None:
            print("  FAIL: kein Frame innerhalb von 15s angekommen.")
            return False
        ok = frame.get("type") == "response" and bool(frame.get("audio"))
        print(f"  {'OK' if ok else 'FAIL'}: type={frame.get('type')} audio_len={len(frame.get('audio',''))}")
        return ok


async def test_no_connection_then_connect():
    print("=== 2) Kein Client verbunden -> spaeter verbinden, sollte trotzdem ankommen ===")
    text = f"TEST-LIVE-EVENT-DELAYED {int(time.time())}"
    memory.add_live_event(text)
    print("  Eintrag geschrieben, warte 7s OHNE Verbindung...")
    await asyncio.sleep(7)
    async with websockets.connect(WS_URL) as ws:
        frame = await wait_for_push(ws, timeout_s=15, expect_text=text)
        if frame is None:
            print("  FAIL: kein Frame angekommen nach Verbindungsaufbau.")
            return False
        ok = frame.get("type") == "response" and bool(frame.get("audio"))
        print(f"  {'OK' if ok else 'FAIL'}: nachtraeglich zugestellt.")
        return ok


async def test_staleness():
    print("=== 3) Veralteter Eintrag (>30min) wird NICHT gesprochen ===")
    text = f"TEST-LIVE-EVENT-STALE {int(time.time())}"
    memory.add_live_event(text)
    # direkt zurueckdatieren
    entries = memory._read_all_live_events()
    for e in entries:
        if e["text"] == text:
            e["created_epoch"] = time.time() - memory.LIVE_EVENT_MAX_AGE_SECONDS - 60
    memory._write_all_live_events(entries)

    async with websockets.connect(WS_URL) as ws:
        frame = await wait_for_push(ws, timeout_s=10, expect_text=text)
        if frame is not None:
            print(f"  FAIL: veralteter Eintrag wurde trotzdem gesprochen: {frame}")
            return False
        print("  OK: kein Frame (wie erwartet).")

    await asyncio.sleep(1)
    entries = memory._read_all_live_events()
    match = next((e for e in entries if e["text"] == text), None)
    if match is None:
        print("  FAIL: Eintrag nicht mehr auffindbar.")
        return False
    ok = match.get("status") == "skipped_stale"
    print(f"  {'OK' if ok else 'FAIL'}: status={match.get('status')} (erwartet skipped_stale)")
    return ok


async def test_busy_session_deferred():
    """Ein Live-Event, das WAEHREND ein normaler Turn noch laeuft geschrieben
    wird, darf erst NACH dem Turn ankommen (SESSION_BUSY), nicht dazwischen —
    und dann mit proactive:true markiert."""
    print("=== 4) Busy-Session: Live-Event kommt erst NACH dem laufenden Turn ===")
    text = f"TEST-LIVE-EVENT-BUSY {int(time.time())}"
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"text": "Wie ist das Wetter?"}))
        # Sofort danach schreiben, waehrend der Turn mit hoher
        # Wahrscheinlichkeit noch laeuft (LLM+TTS-Latenz) — SESSION_BUSY
        # sollte das strukturell garantieren, nicht nur zeitlich zufaellig.
        memory.add_live_event(text)

        frames = []
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            except asyncio.TimeoutError:
                break
            frames.append(json.loads(raw))

        texts = [f.get("text", "") for f in frames]
        print(f"  Frames empfangen: {len(frames)}")
        for f in frames:
            print(f"    proactive={f.get('proactive', False)} text={f.get('text','')[:80]!r}")

        live_idx = next((i for i, t in enumerate(texts) if t == text), None)
        if live_idx is None:
            print("  FAIL: Live-Event nie angekommen.")
            return False
        if live_idx == 0:
            print("  FAIL: Live-Event kam als ALLERERSTES an — SESSION_BUSY hat nicht gegriffen.")
            return False
        if not frames[live_idx].get("proactive"):
            print("  FAIL: Live-Event-Frame hatte kein proactive:true.")
            return False
        print("  OK: Live-Event kam erst nach der normalen Antwort, korrekt markiert.")
        return True


async def main():
    results = []
    results.append(("delivery", await test_delivery()))
    results.append(("delayed_connection", await test_no_connection_then_connect()))
    results.append(("staleness", await test_staleness()))
    results.append(("busy_session_deferred", await test_busy_session_deferred()))

    print("\n" + "=" * 50)
    failed = [name for name, ok in results if not ok]
    if failed:
        print(f"FEHLGESCHLAGEN: {', '.join(failed)}")
        sys.exit(1)
    print("ALLE TESTS OK")


if __name__ == "__main__":
    asyncio.run(main())
