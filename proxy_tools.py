"""
Jarvis V2 — Proxy-Cheap Uebersicht (read-only)

Ahmad hat einen Proxy-Cheap-API-Key gegeben (2026-08-12) fuer eine reine
Bestands-Uebersicht im Cockpit -- welche Proxies gibt es, Status, welchem
Account sie informell zugeordnet sind. BEWUSST NUR LESEND: kein Routing, keine
Zuweisung neuer Proxies, kein Verbinden mit der Account-Automatisierung.
Genau das war die explizite Grenze, die mit Ahmad geklaert wurde -- siehe
claude_app_status.md Regel 4 ("Nie bei Aufbau von Multi-Account-Ban-Evasion-
Infrastruktur helfen (Proxies, ...) -- nur Analyse und Content-Strategie").

Proxy-Cheap selbst hat kein Label/Tag-Feld pro Proxy -- die Konto-Zuordnung
kommt von Ahmad und wird lokal in memory/proxy_labels.json gepflegt.
"""

import json
import os
import time

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LABELS_PATH = os.path.join(os.path.dirname(__file__), "memory", "proxy_labels.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-proxytools.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-proxytools.log")
)
BASE_URL = "https://api.proxy-cheap.com"


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_labels() -> dict:
    if not os.path.exists(LABELS_PATH):
        return {}
    try:
        with open(LABELS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


async def _request(path: str) -> dict:
    config = _load_config()
    api_key = config.get("proxy_cheap_api_key", "")
    api_secret = config.get("proxy_cheap_api_secret", "")
    if not api_key or not api_secret:
        return {"error": "kein proxy_cheap_api_key/secret in config.json hinterlegt"}
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Secret": api_secret,
        "User-Agent": "jarvis-proxy-tools/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{BASE_URL}{path}", headers=headers)
    except Exception as e:
        _log(f"FEHLER bei {path}: {e}")
        return {"error": str(e)}

    if response.status_code != 200:
        _log(f"FEHLER {response.status_code} bei {path}: {response.text[:200]}")
        return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}
    return response.json()


async def get_balance() -> dict:
    return await _request("/account/balance")


async def get_proxies() -> dict:
    return await _request("/proxies")


async def get_proxies_for_cockpit() -> dict:
    """Reine Anzeige-Aufbereitung fuers Cockpit -- IP/Status/ISP/Ablauf/
    Bandbreite plus lokal gepflegtes Konto-Label. Absichtlich OHNE
    Zugangsdaten (Username/Passwort): das Cockpit hat ausser Tailscale-
    Netzwerkzugehoerigkeit keine eigene Auth, und fuer eine reine Uebersicht
    besteht kein funktionaler Grund, wiederverwendbare Proxy-Zugangsdaten
    dort zu zeigen -- Ahmad hat sie ohnehin im Proxy-Cheap-Dashboard."""
    balance = await get_balance()
    proxies_resp = await get_proxies()
    if "error" in proxies_resp:
        return {"error": proxies_resp["error"]}

    labels = _load_labels()
    proxies = []
    for p in proxies_resp.get("proxies", []):
        ip = (p.get("connection") or {}).get("publicIp", "")
        proxies.append({
            "id": p.get("id"),
            "public_ip": ip,
            "status": p.get("status"),
            "network_type": p.get("networkType"),
            "country_code": p.get("countryCode"),
            "isp_name": (p.get("metadata") or {}).get("ispName"),
            "expires_at": p.get("expiresAt"),
            "bandwidth_used_gb": (p.get("bandwidth") or {}).get("used"),
            "auto_extend": p.get("autoExtendEnabled"),
            "account_label": labels.get(ip),
        })

    return {
        "balance": balance.get("balance") if "error" not in balance else None,
        "proxies": proxies,
    }
