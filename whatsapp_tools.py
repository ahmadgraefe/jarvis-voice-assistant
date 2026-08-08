"""
Jarvis V2 — WhatsApp Inbox Check
No official API exists for reading WhatsApp Desktop messages, so this brings
the app to the front, screenshots it, and uses Claude Vision to read the
chat list for unread messages.
"""

import asyncio
import base64
import io
import os
import re

try:
    # Nicht installiert auf dem Server (requirements-server.txt laesst es
    # bewusst weg, siehe unten) — dort werden alle Funktionen dieses Moduls
    # ohnehin durch mac_actuator_client ersetzt, der Import muss nur auf dem
    # Mac selbst klappen (wo mac_actuator.py dieses Modul echt nutzt).
    import pyautogui
except ImportError:
    pyautogui = None
from PIL import Image, ImageGrab


async def _bring_to_front():
    proc = await asyncio.create_subprocess_exec("open", "-a", "WhatsApp")
    await asyncio.wait_for(proc.wait(), timeout=5)
    await asyncio.sleep(2)
    activate_proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", 'tell application "WhatsApp" to activate'
    )
    await asyncio.wait_for(activate_proc.wait(), timeout=5)
    await asyncio.sleep(1.5)


def _capture_screen() -> bytes:
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _get_whatsapp_window_bounds():
    """Position + size of WhatsApp's frontmost window, in LOGICAL points
    (same System Events coordinate space pyautogui/screen_control use).
    Returns None if it can't be read (e.g. window minimized) — caller then
    just falls back to the full, uncropped screenshot."""
    script = '''
    tell application "System Events" to tell process "WhatsApp"
        set p to position of window 1
        set s to size of window 1
        return ((item 1 of p) as string) & "," & ((item 2 of p) as string) & "," & ((item 1 of s) as string) & "," & ((item 2 of s) as string)
    end tell
    '''
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        x, y, w, h = [int(float(v.strip())) for v in stdout.decode().strip().split(",")]
        return x, y, w, h
    except Exception:
        return None


def _crop_to_window(png_bytes: bytes, bounds) -> bytes:
    """Screenshots are PHYSICAL pixels (Retina), window bounds from System
    Events are LOGICAL points — same scale mismatch screen_control.py has to
    correct for, just inverted (there we go physical->logical, here logical
    ->physical to build the crop box)."""
    x, y, w, h = bounds
    img = Image.open(io.BytesIO(png_bytes))
    logical_w, _ = pyautogui.size()
    scale = logical_w / img.width if img.width else 1.0
    box = (
        max(0, int(x / scale)), max(0, int(y / scale)),
        min(img.width, int((x + w) / scale)), min(img.height, int((y + h) / scale)),
    )
    cropped = img.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def _normalize_phone(phone: str) -> str:
    clean = re.sub(r'[^\d+]', '', phone)
    if clean.startswith("0"):
        clean = "+49" + clean[1:]  # German local format -> international
    return clean


async def open_chat_and_screenshot(phone: str) -> bytes:
    """Navigate to any chat by phone number — the same deep link
    app_control.send_whatsapp uses to SEND, here used purely to focus that
    chat (no &text= param, nothing gets sent) so it can be screenshotted."""
    clean_phone = _normalize_phone(phone)
    url = f"whatsapp://send?phone={clean_phone}"
    proc = await asyncio.create_subprocess_exec("open", url)
    await asyncio.wait_for(proc.wait(), timeout=5)
    await asyncio.sleep(2)
    activate_proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", 'tell application "WhatsApp" to activate'
    )
    await asyncio.wait_for(activate_proc.wait(), timeout=5)
    await asyncio.sleep(1.5)
    loop = asyncio.get_event_loop()
    png_bytes = await loop.run_in_executor(None, _capture_screen)
    bounds = await _get_whatsapp_window_bounds()
    if bounds:
        png_bytes = _crop_to_window(png_bytes, bounds)
    return png_bytes


async def open_self_chat_and_screenshot(own_phone: str) -> bytes:
    """Ahmad's own 'Message Yourself' chat — thin wrapper for callers that
    only ever meant the self-chat (kept so existing call sites don't change)."""
    return await open_chat_and_screenshot(own_phone)


async def capture_self_chat_history(own_phone: str, frames: int = 3, scroll_amount: int = 12) -> list:
    """Multiple screenshots of the self-chat, scrolling further back between
    each one — a single screenshot only shows whatever currently fits on
    screen, which isn't enough when Ahmad sends SEVERAL link+insights
    sequences (multiple videos) in one batch (2026-08-07: 'ich muss jarvis
    morgen vollstaendig updaten die insights und links'). Returns frames
    ordered OLDEST-first / NEWEST-last, matching natural top-to-bottom
    reading order for a Vision prompt that has to correlate a link with the
    screenshot(s) that follow it.

    scroll_amount is in pyautogui.scroll() 'clicks', applied at the chat
    window's vertical center — positive scrolls the mouse wheel up, which
    moves the visible window further UP the conversation (towards OLDER
    messages) in every tested macOS chat app including WhatsApp Desktop."""
    clean_phone = _normalize_phone(own_phone)
    url = f"whatsapp://send?phone={clean_phone}"
    proc = await asyncio.create_subprocess_exec("open", url)
    await asyncio.wait_for(proc.wait(), timeout=5)
    await asyncio.sleep(2)
    activate_proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", 'tell application "WhatsApp" to activate'
    )
    await asyncio.wait_for(activate_proc.wait(), timeout=5)
    await asyncio.sleep(1.5)

    loop = asyncio.get_event_loop()
    bounds = await _get_whatsapp_window_bounds()

    async def _shot():
        png_bytes = await loop.run_in_executor(None, _capture_screen)
        return _crop_to_window(png_bytes, bounds) if bounds else png_bytes

    captured = [await _shot()]

    if bounds:
        x, y, w, h = bounds
        center_x, center_y = x + w // 2 + w // 4, y + int(h * 0.5)  # right pane = the chat, not the chat list
        for _ in range(frames - 1):
            await loop.run_in_executor(None, lambda: (pyautogui.moveTo(center_x, center_y), pyautogui.scroll(scroll_amount)))
            await asyncio.sleep(1)
            captured.append(await _shot())

    captured.reverse()
    return captured


async def read_latest_incoming_message(anthropic_client, png_bytes: bytes, contact_label: str) -> dict:
    """Vision-read the currently open chat (already screenshotted+cropped to
    the WhatsApp window via open_chat_and_screenshot). Returns {"text": ...,
    "already_answered": bool} — text is the LAST message bubble that came
    FROM the other person (left-aligned), "" if none visible; already_answered
    is True when the chat's overall LAST bubble (bottom of the screen,
    regardless of direction) is OUTGOING, meaning someone — Ahmad himself,
    typing directly, or Jarvis on an earlier pass — already replied after
    that incoming message. Without this, an autonomous reply pass has no way
    to tell "Jerome asked something, still open" apart from "Jerome asked
    something, Ahmad already personally answered it" — confirmed live
    (2026-08-07): Ahmad replied to Jerome himself ("thank you bro"), and the
    background pass still fired its own redundant reply on top a couple
    minutes later, unaware the conversation was already closed.

    Deliberately asks the model to classify EVERY bubble top-to-bottom
    (L/R) instead of jumping straight to "the latest incoming one" — a
    single-shot pick got fooled twice in live testing by a deleted-message
    placeholder ('Du hast diese Nachricht geloescht') that's italic/grey
    TEXT but was actually sitting on the RIGHT (outgoing) side. Classifying
    per-bubble is a much easier task for the model, and picking the actual
    last 'L' line (and the true last line overall) is then done
    deterministically in Python, not by the model."""
    b64 = base64.b64encode(png_bytes).decode("utf-8")
    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
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
                        f"Das ist ein Screenshot vom WhatsApp-Chat mit '{contact_label}'. "
                        "Liste JEDE Sprechblase im Bild von OBEN nach UNTEN auf, eine Zeile pro Blase, "
                        "in exakt diesem Format: 'L: <text>' wenn die Blase naeher am LINKEN Rand der "
                        "Chat-Spalte ist, oder 'R: <text>' wenn sie naeher am RECHTEN Rand ist. "
                        "Entscheide NUR anhand der horizontalen Position, NIEMALS anhand von Farbe oder "
                        "Textstil. Lass Systemhinweise/geloeschte Nachrichten ('Du hast diese Nachricht "
                        "geloescht', 'This message was deleted') komplett weg — dafuer KEINE Zeile "
                        "ausgeben. Gib NUR die Zeilen aus, keine Ueberschrift, kein Kommentar. Wenn der "
                        "Chat komplett leer ist, gib nichts aus."
                    ),
                },
            ],
        }],
    )
    lines = response.content[0].text.strip().splitlines()
    last_incoming = ""
    last_direction = None
    for line in lines:
        line = line.strip()
        if line.startswith("L:"):
            last_incoming = line[2:].strip()
            last_direction = "L"
        elif line.startswith("R:"):
            last_direction = "R"
    return {"text": last_incoming, "already_answered": last_direction == "R"}


async def summarize_chat(anthropic_client, png_bytes: bytes, contact_label: str) -> str:
    """Vision-read the currently open chat and give a short natural-language
    summary of the recent messages (both directions) — for on-demand 'what
    did X say/what's going on in that chat' asks, unlike
    read_latest_incoming_message() which deliberately only ever returns the
    single newest INCOMING bubble for the Jerome-reply dedup use case."""
    b64 = base64.b64encode(png_bytes).decode("utf-8")
    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": (
                    f"Das ist ein Screenshot vom WhatsApp-Chat mit '{contact_label}'. Fasse auf Deutsch "
                    "kurz zusammen worum es in den sichtbaren Nachrichten geht (die letzten paar "
                    "Nachrichten, beide Richtungen) — 2-3 knappe Saetze, keine Wortliste, kein "
                    "Nachrichten-Zitat-Format. Wenn der Chat leer/nichts zu sehen ist, sag das kurz."
                )},
            ],
        }],
    )
    return response.content[0].text.strip()


async def check_new_messages(anthropic_client) -> str:
    """Bring WhatsApp to front, screenshot it, describe unread chats via Vision."""
    try:
        await _bring_to_front()
        loop = asyncio.get_event_loop()
        png_bytes = await loop.run_in_executor(None, _capture_screen)
        b64 = base64.b64encode(png_bytes).decode("utf-8")

        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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
                            "Das ist ein Screenshot von WhatsApp Desktop. Schau in der Chat-Liste "
                            "links nach ungelesenen Nachrichten (fett gedruckte Chat-Namen, gruene "
                            "Zahlen-Badges). Nenne wer geschrieben hat und worum es laut Vorschautext "
                            "kurz geht. Wenn nichts ungelesen ist, sag kurz 'keine neuen Nachrichten'."
                        ),
                    },
                ],
            }],
        )
        return response.content[0].text
    except Exception as e:
        return f"ERROR: WhatsApp konnte nicht geprueft werden: {e}"


# Server-Migration (Hetzner): siehe app_control.py, gleiches Prinzip.
if os.environ.get("JARVIS_ROLE") == "server":
    from mac_actuator_client import (  # noqa: E402,F811
        check_new_messages, open_chat_and_screenshot, summarize_chat, capture_self_chat_history,
    )
