"""
Jarvis V2 — SLT.bio (Link-in-Bio)
Reads click/page-view analytics for Luna Vale's link-in-bio pages via
slt.bio's real REST API (https://slt.bio/api-reference) — exact numbers
from a real API, not screenshots or scraping. Ahmad's own words (2026-08-07):
"hier ist es wichtig, dass wir unsere Zahlen kennen, klicks und co" — slt.bio
is the link-in-bio ("lander") that funnels traffic to Fanplace.
"""

import json
import os
import time
from collections import defaultdict

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-sltbio.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-sltbio.log")
)
BASE_URL = "https://api.slt.bio"

# Documented: 60 requests/hour per key. A snapshot pass makes at most 2 calls
# (summary + links), so anything down to a ~5min cadence would stay well
# under budget — kept slower anyway since click totals don't need second-
# by-second freshness.


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


async def _request(path: str, params: dict = None) -> dict:
    config = _load_config()
    api_key = config.get("slt_bio_api_key", "")
    if not api_key:
        return {"error": "kein slt_bio_api_key in config.json hinterlegt"}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BASE_URL}{path}", headers=headers, params=params or {})
    except Exception as e:
        _log(f"FEHLER bei {path}: {e}")
        return {"error": str(e)}

    if response.status_code == 429:
        _log(f"Rate-Limit erreicht bei {path}")
        return {"error": "Rate-Limit erreicht (60 Anfragen/Stunde) — spaeter erneut versuchen"}
    if response.status_code != 200:
        _log(f"FEHLER {response.status_code} bei {path}: {response.text[:200]}")
        return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}
    return response.json()


async def get_me() -> dict:
    return await _request("/v1/me")


async def get_links() -> dict:
    return await _request("/v1/links")


async def get_pages() -> dict:
    return await _request("/v1/pages")


async def get_summary(from_date: str = None, to_date: str = None) -> dict:
    """Daily aggregates by event_type/country/referrer/page."""
    params = {}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    return await _request("/v1/summary", params)


async def get_recent_events(since_iso: str = None) -> dict:
    """Raw events, last 7 days (API's own retention window)."""
    params = {"since": since_iso} if since_iso else {}
    return await _request("/v1/events", params)


async def _find_fanplace_link_ids() -> set:
    """Which /v1/links entries actually point at Fanplace — the specific
    funnel step Ahmad cares about, not just generic page traffic."""
    data = await get_links()
    if "error" in data:
        return set()
    return {
        link["id"] for link in data.get("links", [])
        if "fanplace" in (link.get("url") or "").lower()
    }


def _aggregate(entries: list, page_key: str, type_key: str, count_key) -> dict:
    per_page = defaultdict(lambda: {"views": 0, "link_clicks": 0, "blocked": 0})
    totals = {"views": 0, "link_clicks": 0, "blocked": 0}
    for entry in entries:
        page = entry.get(page_key) or "unbekannt"
        count = count_key(entry)
        etype = entry.get(type_key)
        if etype == "page_viewed":
            per_page[page]["views"] += count
            totals["views"] += count
        elif etype in ("link_clicked", "poplink_click"):
            per_page[page]["link_clicks"] += count
            totals["link_clicks"] += count
        elif etype == "country_blocked":
            per_page[page]["blocked"] += count
            totals["blocked"] += count
    return {"totals": totals, "per_page": dict(per_page)}


async def get_daily_snapshot(day: str = None) -> dict:
    """Aggregated page views / link clicks / blocked-bot count for one day,
    broken down by page (persona). Returns {"error": ...} on failure — never
    a guessed number.

    /v1/summary lags behind (empirically confirmed 2026-08-07: shows 0 for
    the current day, presumably finalized only after the day ends), so
    "today" is counted from /v1/events instead — and even there, a bare
    call without '?since=' silently caps at 1000 events starting from the
    OLDEST end of the 7-day window, meaning on any account with real volume
    it never actually reaches today's events at all. Always pass an
    explicit 'since' at that day's start to get real, current data."""
    today = time.strftime("%Y-%m-%d")
    day = day or today

    if day == today:
        events = await get_recent_events(since_iso=f"{day}T00:00:00Z")
        if "error" in events:
            return events
        agg = _aggregate(events.get("events", []), "page_slug", "event_type", lambda e: 1)
    else:
        summary = await get_summary(from_date=day, to_date=day)
        if "error" in summary:
            return summary
        agg = _aggregate(summary.get("summary", []), "page_slug", "event_type", lambda e: e.get("count", 0) or 0)

    return {"day": day, "totals": agg["totals"], "per_page": agg["per_page"]}


async def format_for_jarvis(day: str = None) -> str:
    """Human-readable snapshot for a live check or the daily briefing."""
    snapshot = await get_daily_snapshot(day)
    if "error" in snapshot:
        return f"ERROR: SLT.bio Daten nicht abrufbar: {snapshot['error']}"

    totals = snapshot["totals"]
    lines = [
        f"SLT.bio ({snapshot['day']}): {totals['views']} Seitenaufrufe, "
        f"{totals['link_clicks']} Link-Klicks insgesamt."
    ]
    for page, stats in sorted(snapshot["per_page"].items(), key=lambda kv: -kv[1]["views"]):
        lines.append(f"  @{page}: {stats['views']} Views, {stats['link_clicks']} Klicks")
    if totals["blocked"]:
        lines.append(f"  {totals['blocked']} verdaechtige/geblockte Aufrufe (Bot-Schutz).")
    return "\n".join(lines)
