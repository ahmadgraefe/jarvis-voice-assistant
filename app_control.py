"""
Jarvis V2 — App Control
Opens native macOS apps and sends WhatsApp messages on voice command.
"""

import asyncio
import difflib
import glob
import os
import re
import urllib.parse
from typing import Optional

APP_DIRS = ["/Applications", "/System/Applications", os.path.expanduser("~/Applications")]

# Why the most recent Contacts lookup came back empty. "Contact not found" and
# "Contacts.app never answered" both surface as no phone number, but only the
# first one is Ahmad's problem to fix — callers append this to their error text
# so the logs say which of the two actually happened.
_last_contacts_error = ""


def _list_installed_apps() -> list:
    names = []
    for d in APP_DIRS:
        # Most .app bundles sit directly in the dir, but some (e.g. WhatsApp)
        # ship inside a wrapper folder like "WhatsApp.localized/WhatsApp.app" —
        # check one level deeper too, not just the top level.
        for pattern in ("*.app", "*/*.app"):
            for path in glob.glob(os.path.join(d, pattern)):
                names.append(os.path.splitext(os.path.basename(path))[0])
    return names


def _resolve_app_name(app_name: str) -> str:
    """Speech recognition mangles app names ("Gilark" for "GeeLark") — match
    against what's actually installed instead of failing outright."""
    installed = _list_installed_apps()
    query = app_name.lower()
    lower_map = {a.lower(): a for a in installed}

    if query in lower_map:
        return lower_map[query]

    # Substring containment handles multi-word names like "Google Chrome" —
    # only trust it when exactly one installed app matches, to avoid guessing.
    substring_hits = [a for a in installed if query in a.lower() or a.lower() in query]
    if len(substring_hits) == 1:
        return substring_hits[0]

    # Fuzzy match against each installed app's full name AND its individual
    # words, so a short query like "krom" can match "Chrome" inside "Google
    # Chrome" instead of being penalized for the full name's extra length.
    best_name, best_score = None, 0.0
    for a in installed:
        candidates = [a.lower()] + a.lower().split()
        score = max(difflib.SequenceMatcher(None, query, c).ratio() for c in candidates)
        if score > best_score:
            best_score, best_name = score, a

    return best_name if best_score >= 0.6 else app_name


async def open_app(app_name: str) -> str:
    """Open a named macOS app, fuzzy-matched against what's actually installed."""
    resolved = _resolve_app_name(app_name)
    try:
        proc = await asyncio.create_subprocess_exec("open", "-a", resolved)
        await asyncio.wait_for(proc.wait(), timeout=5)
        if proc.returncode != 0:
            return f"ERROR: Konnte {app_name} nicht finden."
        if resolved.lower() != app_name.lower():
            return f"{resolved} geoeffnet (als naechstliegende App zu '{app_name}' erkannt)."
        return f"{resolved} geoeffnet."
    except Exception as e:
        return f"ERROR: Konnte {app_name} nicht oeffnen: {e}"


async def _run_contacts_script(script: str) -> Optional[str]:
    """Run an AppleScript and return its stdout, or None if it never ran to
    completion. Checking the exit code matters: a script that errors out also
    leaves stdout EMPTY, so stdout alone can't tell "this contact has no
    number" (rc 0) apart from "Contacts.app didn't answer at all" (rc 1)."""
    global _last_contacts_error
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        # Contacts.app can hang indefinitely (cold start, or an open TCC
        # permission dialog waiting on a click). Kill the orphaned osascript
        # and report "not found" — every caller already handles None, whereas
        # a raised TimeoutError has an EMPTY str() and reaches the logs as a
        # blank "FEHLER: " line with nothing to debug.
        proc.kill()
        await proc.wait()
        _last_contacts_error = "Contacts.app hat nach 8s nicht geantwortet"
        return None
    if proc.returncode != 0:
        _last_contacts_error = f"Contacts.app-Fehler: {stderr.decode().strip()}"
        return None
    # Cleared on success so a retry that genuinely finds nothing isn't reported
    # with the first attempt's error still attached to it.
    _last_contacts_error = ""
    return stdout.decode().strip()


async def _launch_contacts():
    """Start Contacts.app in the background. `tell application "Contacts"`
    does NOT auto-launch it when Jarvis runs headless out of the background
    brain — osascript dies with AppleScript error -600 ("Das Programm laeuft
    nicht") instead. `open -g` brings it up without pulling focus away from
    whatever Ahmad is doing at the time."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "open", "-g", "-a", "Contacts",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, OSError):
        pass  # the retry below still works if the app came up anyway


async def _resolve_contact_phone(name: str) -> Optional[str]:
    """Look up a contact's phone number in Contacts.app by name."""
    global _last_contacts_error
    _last_contacts_error = ""
    script = f'''
    tell application "Contacts"
        set matches to (every person whose name contains "{name}")
        if (count of matches) is 0 then return ""
        set thePhones to phones of item 1 of matches
        if (count of thePhones) is 0 then return ""
        return value of item 1 of thePhones
    end tell
    '''
    result = await _run_contacts_script(script)
    if result is None:
        # Nearly always error -600, i.e. Contacts.app just wasn't running, so
        # the lookup never happened. That's what killed the daily content brief
        # on 2026-08-06 with "keine Telefonnummer fuer 'Jerome'" while Jerome
        # WAS in Contacts the whole time. Launch it and retry — a cold start
        # needs a moment before it answers AppleEvents.
        await _launch_contacts()
        for _ in range(2):
            await asyncio.sleep(1.5)
            result = await _run_contacts_script(script)
            if result is not None:
                break
    return result if result else None


def last_contacts_error() -> str:
    """Reason the most recent Contacts lookup failed, "" if it was a plain
    no-such-contact. For callers that log their own failure message."""
    return _last_contacts_error


async def resolve_contact_phone(name_or_phone: str) -> Optional[str]:
    """Public wrapper: `name_or_phone` can already be a phone number (passed
    through as-is) or a Contacts name (looked up). Used anywhere a chat needs
    to be opened without necessarily sending a message (e.g. reading replies)."""
    if re.match(r'^\+?[\d\s()-]{6,}$', name_or_phone):
        return name_or_phone
    return await _resolve_contact_phone(name_or_phone)


async def _is_whatsapp_running() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-x", "WhatsApp", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def _wait_for_whatsapp_window(timeout_s: float = 15) -> bool:
    """Poll until WhatsApp actually has a window — a COLD launch (process
    wasn't running at all) needs real, variable time before it's ready,
    unlike an already-running instance where a short fixed wait is fine."""
    script = (
        'tell application "System Events" to return (exists process "WhatsApp") '
        'and (count of windows of process "WhatsApp") > 0'
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if stdout.decode().strip() == "true":
            return True
        await asyncio.sleep(0.5)
    return False


async def send_whatsapp(recipient: str, message: str) -> str:
    """Send a WhatsApp message. `recipient` can be a phone number or a Contacts name."""
    phone = recipient if re.match(r'^\+?[\d\s()-]{6,}$', recipient) else await _resolve_contact_phone(recipient)
    if not phone:
        detail = f" ({_last_contacts_error})" if _last_contacts_error else ""
        return f"ERROR: Konnte keine Telefonnummer fuer '{recipient}' finden{detail}. Bitte Nummer angeben."

    clean_phone = re.sub(r'[^\d+]', '', phone)
    if clean_phone.startswith("0"):
        clean_phone = "+49" + clean_phone[1:]  # German local format -> international

    # Confirmed live (2026-08-06): the old fixed 3s wait assumed WhatsApp was
    # already warm. On a genuine cold start (process not running at all — the
    # Jerome-chat pass now runs every 15min so this comes up far more often
    # than before) the app can take longer than 3s to show its window, and
    # the blind keystroke below then lands in WHATEVER window happens to be
    # frontmost at that moment — not just "message not sent", a real risk of
    # an unintended Enter keystroke somewhere else entirely.
    was_running = await _is_whatsapp_running()
    url = f"whatsapp://send?phone={clean_phone}&text={urllib.parse.quote(message)}"
    try:
        proc = await asyncio.create_subprocess_exec("open", url)
        await asyncio.wait_for(proc.wait(), timeout=5)

        if was_running:
            await asyncio.sleep(3)  # known-good timing for an already-warm instance
        else:
            got_window = await _wait_for_whatsapp_window(timeout_s=15)
            await asyncio.sleep(2 if got_window else 3)  # let the compose box populate

        # Explicit safety check, not just timing: confirm WhatsApp is
        # actually frontmost right before the blind keystroke. If focus
        # drifted elsewhere, re-activate rather than risk the Enter landing
        # in the wrong place.
        frontmost_proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        frontmost_out, _ = await frontmost_proc.communicate()
        if frontmost_out.decode().strip() != "WhatsApp":
            await asyncio.create_subprocess_exec("osascript", "-e", 'tell application "WhatsApp" to activate')
            await asyncio.sleep(1)

        send_proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to tell process "WhatsApp" to keystroke return',
        )
        await asyncio.wait_for(send_proc.wait(), timeout=5)
        return f"Nachricht an {recipient} gesendet."
    except Exception as e:
        return f"ERROR: WhatsApp-Nachricht fehlgeschlagen: {e}"
