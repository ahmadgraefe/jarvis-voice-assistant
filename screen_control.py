"""
Jarvis V2 — Screen Control
Real mouse/keyboard control of Ahmad's actual screen, on his explicit
request (never autonomously — see server.py's system prompt: this is
scoped to "wenn ich es dir sage", his own words, given the blast radius of
a wrong click on a real, unsandboxed desktop).

Coordinate handling matters here: on a Retina display, PIL screenshots come
back in PHYSICAL pixels (e.g. 2880x1800) but pyautogui/Quartz mouse control
operates in LOGICAL points (e.g. 1440x900) — a raw Vision-identified pixel
coordinate fed straight into pyautogui would land in the wrong place. Every
click_on() call rescales by the measured ratio instead of assuming 2x, so
it still works correctly on a non-Retina display or an external monitor.
"""

import base64
import io
import json
import os
import re
import time

import pyautogui
from PIL import ImageGrab

LOG_PATH = os.path.expanduser("~/Library/Logs/jarvis-screen-control.log")

# Never move faster than this, never move instantly across the whole screen —
# small, real, deliberate — and a hard top-left "abort corner" pyautogui
# itself enforces (FAILSAFE) by throwing if the mouse is yanked to (0,0).
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _capture_screen():
    """Screenshot + the physical->logical coordinate scale factor for this display."""
    img = ImageGrab.grab()
    logical_w, _ = pyautogui.size()
    scale = logical_w / img.width if img.width else 1.0
    return img, scale


def move_mouse(x: int, y: int) -> str:
    """Move to LOGICAL screen coordinates (instant — eased moves measured
    imprecise in testing, landing off-target)."""
    pyautogui.moveTo(x, y)
    _log(f"move_mouse({x}, {y})")
    return f"Maus bei ({x}, {y})."


def click(x: int, y: int, button: str = "left") -> str:
    pyautogui.moveTo(x, y)
    pyautogui.click(button=button)
    _log(f"click({x}, {y}, button={button})")
    return f"Geklickt bei ({x}, {y})."


def double_click(x: int, y: int) -> str:
    pyautogui.moveTo(x, y)
    pyautogui.doubleClick()
    _log(f"double_click({x}, {y})")
    return f"Doppelklick bei ({x}, {y})."


def type_text(text: str) -> str:
    """Via clipboard + Cmd+V, not keystroke-by-keystroke — reliable for
    German umlauts/special characters that pyautogui.write() mishandles,
    and far faster for anything longer than a couple words."""
    import subprocess
    proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"))
    if proc.returncode != 0:
        return "ERROR: Konnte Text nicht in die Zwischenablage kopieren."
    pyautogui.hotkey("command", "v")
    _log(f"type_text({len(text)} Zeichen)")
    return "Text eingegeben."


def press_key(key: str) -> str:
    """key: pyautogui key name — 'enter', 'tab', 'escape', 'backspace', etc."""
    pyautogui.press(key)
    _log(f"press_key({key})")
    return f"Taste '{key}' gedrueckt."


async def click_on(description: str, anthropic_client, action: str = "click") -> str:
    """The interface Jarvis actually uses — it doesn't know pixel
    coordinates, it knows WHAT to click. Screenshots, asks Claude Vision for
    that element's location, rescales to logical points, then clicks there.
    action: 'click' | 'move' (move-only, for a dry-run/verification step)."""
    img, scale = _capture_screen()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": (
                    f"Finde auf diesem Screenshot: \"{description}\".\n"
                    f"Das Bild ist {img.width}x{img.height} Pixel gross. Antworte NUR mit dem Mittelpunkt "
                    "dieses Elements im exakten Format 'X,Y' (Pixel-Koordinaten, kommagetrennt, keine "
                    "Leerzeichen, keine Erklaerung). Wenn du das Element nicht sicher findest, antworte "
                    "exakt mit 'NICHT_GEFUNDEN'."
                )},
            ],
        }],
    )
    raw = response.content[0].text.strip()
    _log(f"click_on('{description}') Vision-Antwort: {raw}")

    match = re.match(r'^(\d+)\s*,\s*(\d+)$', raw)
    if not match:
        return f"ERROR: Konnte '{description}' nicht sicher auf dem Bildschirm finden."

    px, py = int(match.group(1)), int(match.group(2))
    logical_x, logical_y = round(px * scale), round(py * scale)

    if action == "move":
        return move_mouse(logical_x, logical_y) + f" (Ziel: {description})"
    return click(logical_x, logical_y) + f" (Ziel: {description})"
