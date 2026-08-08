"""
Jarvis — Web-Push-Benachrichtigungen (Roadmap Punkt 17, Handy-Companion)
Echte iPhone-Benachrichtigungen ueber Web Push (VAPID), dritter Kanal neben
WhatsApp und Live-Event (siehe background_brain.py _alert()). Laeuft nur auf
dem Server (Hetzner) — pywebpush ist bewusst nur in requirements-server.txt.
"""

import json
import os

from pywebpush import webpush, WebPushException

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
VAPID_PRIVATE_KEY_PATH = os.path.join(MEMORY_DIR, "vapid_private.pem")
VAPID_PUBLIC_KEY_PATH = os.path.join(MEMORY_DIR, "vapid_public_b64.txt")
SUBSCRIPTIONS_PATH = os.path.join(MEMORY_DIR, "push_subscriptions.json")
VAPID_CLAIMS = {"sub": "mailto:ahmad.chahrour833@gmail.com"}


def get_vapid_public_key() -> str:
    with open(VAPID_PUBLIC_KEY_PATH) as f:
        return f.read().strip()


def _load_subscriptions() -> list:
    if not os.path.exists(SUBSCRIPTIONS_PATH):
        return []
    try:
        with open(SUBSCRIPTIONS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_subscriptions(subs: list):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(SUBSCRIPTIONS_PATH, "w") as f:
        json.dump(subs, f, indent=2)


def add_subscription(subscription_info: dict):
    """Dedupliziert ueber den 'endpoint' (eindeutig pro Geraet/Browser-Installation)."""
    subs = _load_subscriptions()
    endpoint = subscription_info.get("endpoint")
    if not any(s.get("endpoint") == endpoint for s in subs):
        subs.append(subscription_info)
        _save_subscriptions(subs)


def send_push_to_all(title: str, body: str):
    """An alle bekannten Geraete schicken. Ein Abo das dauerhaft ungueltig
    ist (Geraet abgemeldet/App deinstalliert, HTTP 404/410) wird entfernt,
    andere Fehler werden nur geloggt statt das Abo zu verlieren."""
    if not os.path.exists(VAPID_PRIVATE_KEY_PATH):
        return
    subs = _load_subscriptions()
    if not subs:
        return
    still_valid = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims=dict(VAPID_CLAIMS),
            )
            still_valid.append(sub)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                print(f"[push] Abo entfernt (ungueltig, HTTP {status}): {sub.get('endpoint', '')[:60]}", flush=True)
                continue
            print(f"[push] Fehler beim Senden: {e}", flush=True)
            still_valid.append(sub)
    if len(still_valid) != len(subs):
        _save_subscriptions(still_valid)
