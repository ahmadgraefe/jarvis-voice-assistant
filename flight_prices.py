"""
Jarvis V2 — Live-Flugpreise (Amadeus Self-Service API)

Hintergrund (echter Vorfall): Ahmad wollte Flugpreise von Skyscanner/Kayak
verglichen haben. Beide Seiten sind hart gegen Bots geschuetzt (CAPTCHA,
Cloudflare) — der Playwright-Weg (browser_tools/browser_extract) scheitert dort
zuverlaessig und liefert im besten Fall eine CAPTCHA-Seite, im schlimmsten Fall
halb geladene Preise, die falsch wirken aber echt aussehen. Deshalb hier
bewusst KEIN Scraping, sondern die offizielle Amadeus-Flight-Offers-Search-API:
dieselbe GDS-Datenquelle, aus der auch Vergleichsportale ihre Preise ziehen.

Kostenloser Key: https://developers.amadeus.com — App anlegen, dann
"amadeus_api_key" und "amadeus_api_secret" in config.json eintragen. Optional
"amadeus_environment": "test" (Default, gratis, eingeschraenkter Datensatz)
oder "production" (echte Live-Preise, Freikontingent 2000 Calls/Monat).

Rein lesend — hier wird nichts gebucht und nichts bezahlt.
"""

import json
import os
import re
import time
from datetime import date, datetime

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-flights.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-flights.log")
)

BASE_URLS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}

# Access-Token gilt laut Amadeus ~30 Minuten. Einmal holen und wiederverwenden,
# sonst kostet jede Preisabfrage zwei Calls aus dem Monatskontingent.
_token_cache: dict = {"token": "", "expires_at": 0.0, "env": ""}

# Kleiner Cache fuer Ortsnamen -> IATA. Rein statische Zuordnung ("Berlin" ist
# morgen auch noch BER), spart pro Anfrage bis zu zwei Reference-Data-Calls.
_iata_cache: dict = {}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _environment(config: dict) -> str:
    env = str(config.get("amadeus_environment", "test")).strip().lower()
    return env if env in BASE_URLS else "test"


async def _get_token(config: dict) -> dict:
    """Holt (oder recycelt) einen OAuth2-Token. Gibt {'token': ...} oder {'error': ...}."""
    key = config.get("amadeus_api_key", "")
    secret = config.get("amadeus_api_secret", "")
    if not key or not secret:
        return {"error": (
            "kein Amadeus-Zugang hinterlegt — 'amadeus_api_key' und 'amadeus_api_secret' "
            "fehlen in config.json. Kostenlos anlegbar auf https://developers.amadeus.com"
        )}

    env = _environment(config)
    if _token_cache["token"] and _token_cache["env"] == env and time.time() < _token_cache["expires_at"]:
        return {"token": _token_cache["token"]}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{BASE_URLS[env]}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": key,
                    "client_secret": secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as e:
        _log(f"Token-Fehler: {e}")
        return {"error": f"Amadeus nicht erreichbar ({e})"}

    if response.status_code != 200:
        _log(f"Token-Fehler HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code in (400, 401):
            return {"error": (
                "Amadeus lehnt die Zugangsdaten ab (HTTP 401) — amadeus_api_key/"
                "amadeus_api_secret in config.json pruefen, und ob der Key zur "
                f"eingestellten Umgebung '{env}' gehoert"
            )}
        return {"error": f"Amadeus-Login fehlgeschlagen: HTTP {response.status_code}"}

    data = response.json()
    token = data.get("access_token", "")
    if not token:
        return {"error": "Amadeus lieferte keinen access_token zurueck"}
    # 60s Sicherheitsabstand, damit kein Token mitten im Call ablaeuft.
    _token_cache.update({
        "token": token,
        "expires_at": time.time() + max(60, int(data.get("expires_in", 1799))) - 60,
        "env": env,
    })
    return {"token": token}


async def _api_get(config: dict, path: str, params: dict) -> dict:
    token = await _get_token(config)
    if "error" in token:
        return token
    env = _environment(config)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{BASE_URLS[env]}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token['token']}"},
            )
    except Exception as e:
        _log(f"FEHLER bei {path}: {e}")
        return {"error": f"Amadeus nicht erreichbar ({e})"}

    if response.status_code == 429:
        return {"error": "Amadeus-Rate-Limit erreicht — in ein paar Minuten erneut versuchen"}
    if response.status_code != 200:
        detail = ""
        try:
            errors = response.json().get("errors", [])
            if errors:
                first = errors[0]
                detail = f" — {first.get('title', '')}: {first.get('detail', '')}".rstrip(": ")
        except ValueError:
            detail = f" — {response.text[:200]}"
        _log(f"FEHLER {response.status_code} bei {path}: {response.text[:300]}")
        return {"error": f"HTTP {response.status_code}{detail}"}

    try:
        return response.json()
    except ValueError:
        return {"error": "Amadeus lieferte keine lesbare Antwort"}


async def resolve_iata(config: dict, place: str) -> dict:
    """'Berlin' -> {'code': 'BER', 'label': 'Berlin'}. Ein bereits gueltiger
    3-Buchstaben-Code wird unveraendert uebernommen."""
    place = (place or "").strip()
    if not place:
        return {"error": "kein Ort angegeben"}
    if re.fullmatch(r"[A-Za-z]{3}", place):
        return {"code": place.upper(), "label": place.upper()}

    cached = _iata_cache.get(place.lower())
    if cached:
        return cached

    data = await _api_get(config, "/v1/reference-data/locations", {
        "subType": "CITY,AIRPORT",
        "keyword": place,
        "page[limit]": 5,
    })
    if "error" in data:
        return {"error": f"Konnte '{place}' keinem Flughafen zuordnen ({data['error']})"}

    entries = data.get("data", [])
    if not entries:
        return {"error": (
            f"Kein Flughafen/keine Stadt zu '{place}' gefunden — bitte den IATA-Code "
            "nennen (z.B. BER, MUC, IST)"
        )}
    # Staedte vor einzelnen Flughaefen: "Berlin" soll BER (Stadtcode) treffen,
    # nicht zufaellig einen kleinen Regionalflughafen in der Naehe.
    entries.sort(key=lambda e: 0 if e.get("subType") == "CITY" else 1)
    best = entries[0]
    result = {
        "code": best.get("iataCode", ""),
        "label": f"{best.get('name', place).title()} ({best.get('iataCode', '')})",
    }
    if not result["code"]:
        return {"error": f"Amadeus lieferte fuer '{place}' keinen IATA-Code"}
    _iata_cache[place.lower()] = result
    return result


def _parse_date(value: str, label: str) -> dict:
    value = (value or "").strip()
    if not value:
        return {"error": f"{label} fehlt"}
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"{label} '{value}' ist kein gueltiges Datum im Format JJJJ-MM-TT"}
    if parsed < date.today():
        return {"error": f"{label} '{value}' liegt in der Vergangenheit"}
    return {"date": parsed.isoformat()}


def _format_duration(iso: str) -> str:
    """'PT7H35M' -> '7h 35m'."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso or "")
    if not match:
        return iso or "?"
    hours, minutes = match.group(1), match.group(2)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"


def _format_time(iso: str) -> str:
    """'2026-09-01T07:20:00' -> '01.09. 07:20'."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or "?"
    return dt.strftime("%d.%m. %H:%M")


def _describe_itinerary(itinerary: dict, carriers: dict) -> str:
    segments = itinerary.get("segments", [])
    if not segments:
        return "keine Streckendaten"
    first, last = segments[0], segments[-1]
    stops = len(segments) - 1
    stop_text = "direkt" if stops == 0 else (
        f"1 Stopp ({segments[0].get('arrival', {}).get('iataCode', '?')})" if stops == 1
        else f"{stops} Stopps ({', '.join(s.get('arrival', {}).get('iataCode', '?') for s in segments[:-1])})"
    )
    airlines = []
    for segment in segments:
        code = segment.get("carrierCode", "")
        name = carriers.get(code, code)
        if name and name not in airlines:
            airlines.append(name)
    return (
        f"{first.get('departure', {}).get('iataCode', '?')} "
        f"{_format_time(first.get('departure', {}).get('at', ''))} "
        f"→ {last.get('arrival', {}).get('iataCode', '?')} "
        f"{_format_time(last.get('arrival', {}).get('at', ''))} "
        f"| {_format_duration(itinerary.get('duration', ''))} | {stop_text} "
        f"| {', '.join(airlines) if airlines else 'Airline unbekannt'}"
    )


def booking_link(origin: str, destination: str, departure: str, return_date: str = "") -> str:
    """Google-Flights-Suchlink zum selben Flug — Amadeus liefert Preise, aber
    keine buchbare Oberflaeche. Nur ein Link, es wird nichts ausgeloest."""
    query = f"Fluege von {origin} nach {destination} am {departure}"
    if return_date:
        query += f" zurueck am {return_date}"
    return "https://www.google.com/travel/flights?q=" + query.replace(" ", "%20")


async def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str = "",
    adults: int = 1,
    travel_class: str = "",
    nonstop: bool = False,
    max_results: int = 5,
    currency: str = "EUR",
) -> dict:
    """Live-Flugangebote suchen. Gibt {'offers': [...], ...} oder {'error': ...}."""
    config = _load_config()
    if not config.get("amadeus_api_key") or not config.get("amadeus_api_secret"):
        # Frueh und direkt melden, sonst kommt derselbe Grund erst verpackt in
        # einem "Ort nicht gefunden" heraus und liest sich wie ein Tippfehler.
        return {"error": (
            "kein Amadeus-Zugang hinterlegt — 'amadeus_api_key' und 'amadeus_api_secret' "
            "fehlen in config.json. Kostenloser Key: https://developers.amadeus.com "
            "(App anlegen, Key+Secret eintragen). Ohne den kann ich keine echten "
            "Flugpreise nennen; Skyscanner/Kayak blocken das Auslesen per CAPTCHA."
        )}

    departure = _parse_date(departure_date, "Hinflugdatum")
    if "error" in departure:
        return departure
    return_iso = ""
    if return_date:
        parsed_return = _parse_date(return_date, "Rueckflugdatum")
        if "error" in parsed_return:
            return parsed_return
        if parsed_return["date"] < departure["date"]:
            return {"error": "Rueckflug liegt vor dem Hinflug"}
        return_iso = parsed_return["date"]

    try:
        adults = max(1, min(9, int(adults)))
    except (TypeError, ValueError):
        adults = 1
    try:
        max_results = max(1, min(10, int(max_results)))
    except (TypeError, ValueError):
        max_results = 5

    from_place = await resolve_iata(config, origin)
    if "error" in from_place:
        return from_place
    to_place = await resolve_iata(config, destination)
    if "error" in to_place:
        return to_place
    if from_place["code"] == to_place["code"]:
        return {"error": f"Start und Ziel sind identisch ({from_place['code']})"}

    params = {
        "originLocationCode": from_place["code"],
        "destinationLocationCode": to_place["code"],
        "departureDate": departure["date"],
        "adults": adults,
        "currencyCode": (currency or "EUR").upper(),
        "max": max_results,
    }
    if return_iso:
        params["returnDate"] = return_iso
    if nonstop:
        params["nonStop"] = "true"
    if travel_class:
        allowed = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}
        wanted = travel_class.strip().upper().replace(" ", "_")
        if wanted not in allowed:
            return {"error": f"Unbekannte Klasse '{travel_class}' (moeglich: {', '.join(sorted(allowed))})"}
        params["travelClass"] = wanted

    data = await _api_get(config, "/v2/shopping/flight-offers", params)
    if "error" in data:
        return data

    carriers = (data.get("dictionaries") or {}).get("carriers") or {}
    offers = []
    for offer in data.get("data", []):
        price = offer.get("price", {}) or {}
        total = price.get("grandTotal") or price.get("total")
        if total is None:
            continue  # ohne Preis ist das Angebot fuer einen Preisvergleich wertlos
        itineraries = offer.get("itineraries", []) or []
        offers.append({
            "price": float(total),
            "currency": price.get("currency", params["currencyCode"]),
            "seats_left": offer.get("numberOfBookableSeats"),
            "airline": ", ".join(
                carriers.get(code, code) for code in offer.get("validatingAirlineCodes", [])
            ),
            "outbound": _describe_itinerary(itineraries[0], carriers) if itineraries else "",
            "inbound": _describe_itinerary(itineraries[1], carriers) if len(itineraries) > 1 else "",
        })

    if not offers:
        return {"error": (
            f"Amadeus kennt fuer {from_place['code']} → {to_place['code']} am "
            f"{departure['date']} keine Angebote"
            + (" (Test-Umgebung: dort ist nur ein Teil der echten Strecken/Daten hinterlegt)"
               if _environment(config) == "test" else "")
        )}

    offers.sort(key=lambda o: o["price"])
    return {
        "offers": offers,
        "from": from_place["label"],
        "to": to_place["label"],
        "departure_date": departure["date"],
        "return_date": return_iso,
        "adults": adults,
        "environment": _environment(config),
        "link": booking_link(from_place["code"], to_place["code"], departure["date"], return_iso),
    }
