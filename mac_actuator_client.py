"""
Jarvis — Mac-Actuator-Client (Server-Migration, Hetzner)
Wird NUR geladen wenn JARVIS_ROLE=server gesetzt ist (siehe app_control.py,
whatsapp_tools.py, screen_control.py, jeweils am Dateiende). Ersetzt dort die
echten, Mac-lokalen Funktionen durch duenne HTTP-Aufrufe an mac_actuator.py,
das auf dem Mac laeuft und nur ueber Tailscale erreichbar ist. Gleiche
Funktionsnamen/-signaturen wie die Originale, damit server.py an keiner
einzigen Aufrufstelle etwas davon merkt.
"""

import os
import base64
from typing import Optional
import httpx

MAC_ACTUATOR_URL = os.environ.get("MAC_ACTUATOR_URL", "http://100.113.144.103:8420")
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

_last_error = ""


async def _post(path: str, json_body: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(f"{MAC_ACTUATOR_URL}{path}", json=json_body or {})
        r.raise_for_status()
        return r.json()


# --- app_control.py Ersatz ---

async def open_app(app_name: str) -> str:
    try:
        data = await _post("/app/open", {"app_name": app_name})
        return data["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."


async def send_whatsapp(recipient: str, message: str) -> str:
    try:
        data = await _post("/whatsapp/send", {"recipient": recipient, "message": message})
        return data["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."


async def resolve_contact_phone(name_or_phone: str):
    global _last_error
    try:
        data = await _post("/whatsapp/resolve_contact", {"name_or_phone": name_or_phone})
        _last_error = data.get("error") or ""
        return data.get("phone")
    except Exception as e:
        _last_error = f"Mac-Actuator nicht erreichbar ({e})"
        return None


def last_contacts_error() -> str:
    return _last_error


# --- whatsapp_tools.py Ersatz ---

async def check_new_messages(anthropic_client=None) -> str:
    try:
        data = await _post("/whatsapp/check_new")
        return data["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."


async def open_chat_and_screenshot(phone: str) -> bytes:
    try:
        data = await _post("/whatsapp/open_chat_screenshot", {"phone": phone})
        return base64.b64decode(data["image_b64"])
    except Exception:
        return b""


async def summarize_chat(anthropic_client, png_bytes: bytes, contact_label: str) -> str:
    try:
        data = await _post("/whatsapp/summarize_chat", {
            "image_b64": base64.b64encode(png_bytes).decode(),
            "contact_label": contact_label,
        })
        return data["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."


async def capture_self_chat_history(own_phone: str, frames: int = 3, scroll_amount: int = 12) -> list:
    try:
        data = await _post("/whatsapp/self_chat_history", {"frames": frames, "scroll_amount": scroll_amount})
        return [base64.b64decode(x) for x in data["images_b64"]]
    except Exception:
        return []


# --- screen_control.py Ersatz ---

async def click_on(description: str, anthropic_client, action: str = "click") -> str:
    try:
        data = await _post("/screen/click", {"description": description, "action": action})
        return data["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."


def type_text(text: str) -> str:
    # Original ist bewusst synchron (server.py ruft es ohne await auf),
    # darum hier ein synchroner httpx-Client statt AsyncClient.
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.post(f"{MAC_ACTUATOR_URL}/screen/type", json={"text": text})
            r.raise_for_status()
            return r.json()["result"]
    except Exception as e:
        return f"ERROR: Mac-Actuator nicht erreichbar ({e})."
