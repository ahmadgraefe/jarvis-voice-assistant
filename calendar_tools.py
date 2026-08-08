"""
Jarvis V2 — Google Calendar
Reads upcoming events and creates/cancels events via the Calendar API, so
Jarvis can actually manage Ahmad's schedule, not just report on it. Same
OAuth pattern as gmail_tools.py/sheets_tools.py — first run opens a browser
once for consent, after that a saved token is reused and auto-refreshed.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "calendar_token.json")
TIMEZONE = "Europe/Berlin"

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        client_config = {
            "installed": {
                "client_id": config["google_client_id"],
                "client_secret": config["google_client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    return creds


def _get_service():
    return build("calendar", "v3", credentials=_get_credentials())


def _format_event_time(event: dict) -> str:
    start = event.get("start", {})
    if "date" in start:  # all-day event
        return "ganztaegig"
    dt = datetime.fromisoformat(start["dateTime"])
    end = event.get("end", {})
    end_dt = datetime.fromisoformat(end["dateTime"]) if "dateTime" in end else None
    if end_dt:
        return f"{dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
    return dt.strftime("%H:%M")


def _list_events_sync(days: int) -> list:
    service = _get_service()
    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=days)).isoformat() + "Z"
    result = service.events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime", maxResults=25,
    ).execute()
    return result.get("items", [])


async def list_upcoming_events(days: int = 7) -> str:
    """Formatted, speakable list of events in the next `days` days."""
    loop = asyncio.get_event_loop()
    try:
        events = await loop.run_in_executor(None, _list_events_sync, days)
    except Exception as e:
        return f"ERROR: Kalender konnte nicht abgerufen werden: {e}"

    if not events:
        return f"Keine Termine in den naechsten {days} Tagen."

    lines = []
    for e in events:
        start = e.get("start", {})
        date_str = start.get("date") or start.get("dateTime", "")[:10]
        date_obj = datetime.fromisoformat(date_str)
        weekday = WEEKDAYS_DE[date_obj.weekday()]
        title = e.get("summary", "(ohne Titel)")
        time_str = _format_event_time(e)
        location = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"{weekday}, {date_obj.strftime('%d.%m.')} {time_str}: {title}{location}")
    return "\n".join(lines)


async def list_upcoming_events_structured(days: int = 7) -> list:
    """Wie list_upcoming_events, aber mit erhaltener Zeitstruktur statt eines
    fertigen Sprech-Strings — fuer programmatische Konfliktpruefung
    (background_brain.py's calendar_conflict_pass), nicht fuer Sprachausgabe.
    description/location/attendees (Tier 3, Punkt 14) fuer Kontext-
    Zusammenstellung vor Terminen, calendar_conflict_pass ignoriert sie einfach."""
    loop = asyncio.get_event_loop()
    events = await loop.run_in_executor(None, _list_events_sync, days)
    result = []
    for e in events:
        start = e.get("start", {})
        end = e.get("end", {})
        attendees = [
            a["email"] for a in e.get("attendees", [])
            if a.get("email") and not a.get("self")
        ]
        result.append({
            "id": e.get("id"),
            "summary": e.get("summary", "(ohne Titel)"),
            "all_day": "date" in start,
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "description": e.get("description", ""),
            "location": e.get("location", ""),
            "attendees": attendees,
        })
    return result


async def get_today_events_brief() -> str:
    """Short one-liner for the daily briefing — just today, not the week."""
    loop = asyncio.get_event_loop()
    try:
        events = await loop.run_in_executor(None, _list_events_sync, 1)
    except Exception as e:
        return f"ERROR: {e}"

    today = datetime.now().date()
    todays = []
    for e in events:
        start = e.get("start", {})
        date_str = start.get("date") or start.get("dateTime", "")[:10]
        if datetime.fromisoformat(date_str).date() != today:
            continue
        todays.append(f"{_format_event_time(e)} {e.get('summary', '(ohne Titel)')}")

    if not todays:
        return "Keine Termine heute."
    return "; ".join(todays)


def _create_event_sync(title: str, date: str, start_time: str, end_time: str, description: str) -> dict:
    service = _get_service()
    if start_time.lower() in ("ganztags", "ganztaegig", ""):
        body = {
            "summary": title,
            "description": description,
            "start": {"date": date},
            "end": {"date": date},
        }
    else:
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": f"{date}T{start_time}:00", "timeZone": TIMEZONE},
            "end": {"dateTime": f"{date}T{end_time}:00", "timeZone": TIMEZONE},
        }
    return service.events().insert(calendarId="primary", body=body).execute()


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


async def create_event(title: str, date: str, start_time: str = "", end_time: str = "", description: str = "") -> str:
    """Creates a real calendar event. start_time == "" (or "ganztags") makes an all-day event."""
    if not title.strip():
        return "ERROR: Termin braucht einen Titel."
    if not _DATE_RE.match(date):
        return f"ERROR: Datum '{date}' hat nicht das Format JJJJ-MM-TT."
    try:
        datetime.fromisoformat(date)  # catches a well-formed-looking but impossible date, e.g. 2026-02-30
    except ValueError:
        return f"ERROR: Datum '{date}' ist kein gueltiges Datum."

    is_all_day = start_time.lower() in ("ganztags", "ganztaegig", "")
    if not is_all_day:
        if not _TIME_RE.match(start_time):
            return f"ERROR: Startzeit '{start_time}' hat nicht das Format HH:MM (24h)."
        if not end_time:
            # Forgetting the end time is a plausible slip in a voice-driven
            # flow — default to +1h instead of either erroring out or
            # sending a malformed "T:00" straight to the Google API.
            hour, minute = (int(x) for x in start_time.split(":"))
            end_time = f"{(hour + 1) % 24:02d}:{minute:02d}"
        elif not _TIME_RE.match(end_time):
            return f"ERROR: Endzeit '{end_time}' hat nicht das Format HH:MM (24h)."

    loop = asyncio.get_event_loop()
    try:
        event = await loop.run_in_executor(None, _create_event_sync, title, date, start_time, end_time, description)
    except Exception as e:
        return f"ERROR: Termin konnte nicht angelegt werden: {e}"

    when = "ganztaegig" if is_all_day else f"{start_time}-{end_time} Uhr"
    return f"Termin angelegt: '{title}' am {date} ({when})."


def _find_matching_events_sync(title_query: str, date: str) -> list:
    # Query a window a day wider on each side than needed, then filter to the
    # EXACT date client-side by comparing the event's own date string — an
    # all-day event's [timeMin, timeMax) boundary interacts with timezone
    # offsets in ways that made a same-day window silently miss it in
    # testing. Comparing actual dates sidesteps that boundary math entirely.
    service = _get_service()
    target = datetime.fromisoformat(date)
    day_start = (target - timedelta(days=1)).isoformat() + "Z"
    day_end = (target + timedelta(days=2)).isoformat() + "Z"
    result = service.events().list(
        calendarId="primary", timeMin=day_start, timeMax=day_end, singleEvents=True,
    ).execute()
    matches = []
    for e in result.get("items", []):
        start = e.get("start", {})
        event_date = start.get("date") or start.get("dateTime", "")[:10]
        if event_date == date and title_query.lower() in e.get("summary", "").lower():
            matches.append(e)
    return matches


async def delete_event(title_query: str, date: str) -> str:
    """Finds events matching title_query on that date and deletes ONLY if
    there's exactly one match — never guesses which event to cancel when
    there's ambiguity, a delete is not reversible from a voice command."""
    # An empty/near-empty query is a substring of EVERY event title, so it
    # would "confidently" match a single event that day and delete it with
    # no real match at all — the len(matches)>1 ambiguity guard below can't
    # catch this because there'd only be ONE match. Reject before searching.
    if len(title_query.strip()) < 2:
        return "ERROR: Titel fuer die Suche ist zu kurz/leer — bitte einen konkreteren Titel nennen, bevor ich loesche."
    if not _DATE_RE.match(date):
        return f"ERROR: Datum '{date}' hat nicht das Format JJJJ-MM-TT."

    loop = asyncio.get_event_loop()
    try:
        matches = await loop.run_in_executor(None, _find_matching_events_sync, title_query, date)
    except Exception as e:
        return f"ERROR: Kalender konnte nicht durchsucht werden: {e}"

    if not matches:
        return f"ERROR: Kein Termin mit '{title_query}' am {date} gefunden."
    if len(matches) > 1:
        titles = ", ".join(f"'{m.get('summary', '?')}'" for m in matches)
        return f"ERROR: Mehrere Termine passen ({titles}) — bitte genauer benennen, bevor ich loesche."

    event = matches[0]
    service = _get_service()
    try:
        await loop.run_in_executor(
            None, lambda: service.events().delete(calendarId="primary", eventId=event["id"]).execute()
        )
    except Exception as e:
        return f"ERROR: Termin konnte nicht geloescht werden: {e}"

    return f"Termin '{event.get('summary')}' am {date} abgesagt."
