"""
Jarvis V2 — Instagram Graph API (offizielle Insights)

Ersetzt fuer Ahmads EIGENE Luna-Vale-Accounts das bisherige, fragile
Screenshot-Verfahren (Vision-Modell rät Zahlen von Instagram-Screenshots ab,
Views sind dabei oft gar nicht sichtbar) durch echte, offiziell von Meta
gelieferte Zahlen -- direkt vom Server abgerufen, mit den offiziellen
Access-Tokens aus Ahmads eigener Meta-App. Konkurrenz-Accounts bleiben beim
Screenshot-Verfahren (dafuer gibt es keine API-Zugangsdaten).

BEWUSST OHNE Proxy-Routing: jeder Call kommt von der stabilen Server-IP.
Legitime, autorisierte Business-API-Nutzung braucht keine IP-Tarnung -- ein
Account-eigener Proxy pro Token waere der Kernmechanismus von Multi-Account-
Ban-Evasion (claude_app_status.md Regel 4, explizit ausgeschlossen). Diese
Grenze wurde 2026-08-12 mit Ahmad geklaert.

Metriken/Endpunkte 2026-08-12 direkt bei Meta nachgeschlagen (nicht geraten):
https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights/
Gueltige Reels-Metriken: views, likes, comments, reach, saved, shares.
'impressions' ist fuer Medien nach 2024-07-02 deprecated, wird nicht genutzt.
Ein Laender-/US-Audience-Breakdown pro einzelnem Reel ist ueber die
oeffentliche API NICHT verfuegbar -- diese eine Zahl bleibt weiter Sache des
alten Screenshot-Verfahrens.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-igapi.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-igapi.log")
)
BASE_URL = "https://graph.instagram.com"
# 2026-08-12, echter Fehler live gefunden: "IGAA..."-Tokens ("Instagram API
# with Instagram Login") laufen ueber DIESEN Host, nicht graph.facebook.com/vXX
# (klassische Facebook-Graph-API via verknuepfter Seite). Erste Version
# dieses Moduls nutzte faelschlich graph.facebook.com -- alle vier Tokens
# waren die ganze Zeit gueltig, das war der tatsaechliche Fehler, nicht die
# Tokens. Ahmad hat das selbst gefunden und korrigiert.
INSIGHTS_METRICS = "views,likes,comments,reach,saved,shares"

# Meta-Fehlercodes, die nur Ahmad selbst loesen kann (Token nicht
# parsebar/ungueltig, Berechtigung entzogen/abgelaufen) -- alles andere
# (Rate-Limit, 5xx, Netzwerk) ist transientes Rauschen, das der naechste
# taegliche Lauf von selbst erneut versucht, ohne Ahmad zu stoeren.
_FATAL_ERROR_CODES = {190, 10, 200, 803}

MAX_PAGES = 10  # harte Obergrenze gegen unnoetig langes Zurueckblaettern


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def get_configured_accounts() -> dict:
    """{"handle": {"user_id": ..., "access_token": ...}, ...} aus config.json."""
    return _load_config().get("instagram_graph_accounts", {})


async def _fetch(url_or_path: str, access_token: str, params: dict = None, absolute: bool = False) -> dict:
    """`absolute=True` fuer Metas eigene paging.next-URLs (die haben Query-
    String inkl. access_token bereits komplett -- kein erneutes Anhaengen)."""
    full_url = url_or_path if absolute else f"{BASE_URL}{url_or_path}"
    call_params = dict(params or {})
    if not absolute:
        call_params["access_token"] = access_token
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(full_url, params=call_params)
    except Exception as e:
        _log(f"FEHLER bei {url_or_path}: {e}")
        return {"error": {"message": str(e), "code": None, "fatal": False}}

    data = response.json()
    if "error" in data:
        code = data["error"].get("code")
        fatal = code in _FATAL_ERROR_CODES
        _log(f"Meta-Fehler bei {url_or_path} (Code {code}, fatal={fatal}): {data['error'].get('message')}")
        return {"error": {"message": data["error"].get("message", "unbekannt"), "code": code, "fatal": fatal}}
    return data


async def verify_token(user_id: str, access_token: str) -> dict:
    """Leichter Preflight VOR jedem /media-Aufruf -- faengt einen kaputten
    Token (live beobachtet 2026-08-12: 'Cannot parse access token') frueh
    und sauber ab, statt in verwirrenden Pagination-Fehlern zu enden."""
    result = await _fetch(f"/{user_id}", access_token, {"fields": "id,username"})
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    return {"ok": True, "username": result.get("username")}


async def get_recent_reels(user_id: str, access_token: str, days: int = 30) -> dict:
    """Paginiert /media (neueste zuerst), bricht ab sobald der aelteste
    Eintrag der aktuellen Seite aelter als `days` ist -- kein unnoetiges
    Zurueckblaettern in die Vergangenheit. Trennt danach in zwei Listen:
    letzte 48h (Prioritaet) und Rest, damit bei einem Abbruch (Timeout,
    Fehler mitten in der Pass) immer zuerst die frischesten Reels verarbeitet
    sind, nicht irgendwelche je nach Rueckgabe-Reihenfolge."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    priority_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    priority, rest = [], []
    path = f"/{user_id}/media"
    params = {"fields": "id,media_type,media_product_type,permalink,timestamp,caption", "limit": 50}
    absolute = False
    for _ in range(MAX_PAGES):
        result = await _fetch(path, access_token, params, absolute=absolute)
        if "error" in result:
            return result

        stop = False
        for item in result.get("data", []):
            if item.get("media_product_type") != "REELS":
                continue
            ts_raw = item.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw else None
            except ValueError:
                ts = None
            if ts and ts < cutoff:
                stop = True
                continue
            (priority if ts and ts >= priority_cutoff else rest).append(item)
        if stop:
            break

        next_url = (result.get("paging") or {}).get("next")
        if not next_url:
            break
        await asyncio.sleep(1)  # kurzer Abstand vor der naechsten Seite, siehe background_brain.py-Kommentar
        path, params, absolute = next_url, None, True

    return {"priority": priority, "rest": rest}


async def get_reel_insights(media_id: str, access_token: str) -> dict:
    """EIN Call mit allen sechs Metriken statt sechs einzelnen Calls."""
    result = await _fetch(f"/{media_id}/insights", access_token, {"metric": INSIGHTS_METRICS})
    if "error" in result:
        return result
    values = {}
    for entry in result.get("data", []):
        name = entry.get("name")
        vals = entry.get("values") or []
        if name and vals:
            values[name] = vals[0].get("value")
    return {"insights": values}
