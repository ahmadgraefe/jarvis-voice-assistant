"""
Jarvis V2 — Gmail
Reads recent inbox mail via the Gmail API so Jarvis can summarize what's new,
and (Tier 1 Punkt 7, 2026-08-08) can draft replies to routine mail — DRAFTS
ONLY, never sends on its own, Ahmad's explicit choice. First run opens a
browser once for OAuth consent; after that a saved token is reused and
auto-refreshed. Widening SCOPES (below) after this feature was added requires
deleting the old gmail_token.json once so the OAuth flow re-prompts for the
new permission — an existing token narrower than the current SCOPES list is
not silently upgraded.
"""

import asyncio
import base64
import json
import os
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# gmail.compose (not gmail.send or gmail.modify): least privilege for "create
# a draft" — structurally this scope alone cannot send a message unless code
# explicitly calls the drafts.send endpoint, which nothing here does.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",  # Tier 3, Punkt 13 — nur fuer
    # messages().modify() (Label INBOX entfernen = archivieren). Erlaubt strukturell
    # auch Senden, wird aber nirgends im Code aufgerufen (gleiche Begruendung wie
    # beim gmail.compose-Kommentar oben) — Ahmads harte Grenze ist Verhalten, nicht Scope.
]
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "gmail_token.json")
MAX_RESULTS = 5


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


def _fetch_recent_emails_sync(max_results: int) -> list:
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)
    results = service.users().messages().list(
        userId="me", maxResults=max_results, labelIds=["INBOX"]
    ).execute()

    emails = []
    for m in results.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        is_unread = "UNREAD" in msg.get("labelIds", [])
        emails.append({
            "id": m["id"],
            "from": headers.get("From", "Unbekannt"),
            "subject": headers.get("Subject", "(kein Betreff)"),
            "snippet": msg.get("snippet", ""),
            "unread": is_unread,
        })
    return emails


def _search_emails_sync(query: str, max_results: int) -> list:
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)
    results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()

    emails = []
    for m in results.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        emails.append({
            "id": m["id"],
            "from": headers.get("From", "Unbekannt"),
            "subject": headers.get("Subject", "(kein Betreff)"),
            "snippet": msg.get("snippet", ""),
        })
    return emails


async def search_emails(query: str, max_results: int = 3) -> list:
    """Tier 3, Punkt 14 — Gmail-Suche (z.B. 'from:x@y.com' oder freier Text),
    fuer Kontext-Zusammenstellung vor Terminen. Reiner Lesezugriff, braucht
    keinen neuen Scope (gmail.readonly deckt Suche bereits ab)."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _search_emails_sync, query, max_results)
    except Exception:
        return []


async def list_recent_emails(max_results: int = MAX_RESULTS) -> list:
    """Wie get_recent_emails_summary, aber die strukturierte Liste statt
    eines fertigen Sprech-Strings — fuer background_brain.py's
    gmail_reply_pass, das pro Mail die id fuer fetch_email_detail braucht."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_recent_emails_sync, max_results)


async def get_recent_emails_summary() -> str:
    """Fetch the most recent inbox emails and format them for the Jarvis LLM."""
    loop = asyncio.get_event_loop()
    try:
        emails = await loop.run_in_executor(None, _fetch_recent_emails_sync, MAX_RESULTS)
    except Exception as e:
        return f"ERROR: Gmail konnte nicht abgerufen werden: {e}"

    if not emails:
        return "Keine E-Mails im Posteingang."

    unread_count = sum(1 for e in emails if e["unread"])
    lines = [f"Ungelesene Mails unter den letzten {len(emails)}: {unread_count}\n"]
    for e in emails:
        status = "UNGELESEN" if e["unread"] else "gelesen"
        lines.append(f"Von: {e['from']}\nBetreff: {e['subject']}\nStatus: {status}\nAusschnitt: {e['snippet']}")
    return "\n\n".join(lines)


def _extract_plain_text(payload: dict) -> str:
    """Walks a Gmail message payload (possibly multipart/nested) for the
    first text/plain part and base64url-decodes it. Falls back to an empty
    string rather than raising — a mail Jarvis can't extract a body from
    just gets classified/drafted with less context, not a hard failure."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


def _fetch_email_detail_sync(message_id: str) -> dict:
    """Voller Inhalt EINER Mail (Format 'full') — Body-Text plus die fuer
    korrektes Antwort-Threading noetigen Felder (Gmail threadId, RFC822
    Message-ID Header), die _fetch_recent_emails_sync's leichte
    Metadata-Ansicht nicht liefert."""
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From", "Unbekannt"),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", "(kein Betreff)"),
        "message_id_header": headers.get("Message-ID", ""),
        "body": _extract_plain_text(msg["payload"]),
    }


async def fetch_email_detail(message_id: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_email_detail_sync, message_id)


def _create_draft_reply_sync(to: str, subject: str, body: str, thread_id: str, in_reply_to: str = "") -> str:
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)

    message = MIMEText(body)
    message["To"] = to
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    draft_body = {"message": {"raw": raw, "threadId": thread_id}}
    draft = service.users().drafts().create(userId="me", body=draft_body).execute()
    return draft.get("id", "")


async def create_draft_reply(to: str, subject: str, body: str, thread_id: str, in_reply_to: str = "") -> str:
    """Legt einen Antwort-ENTWURF an, sendet NIEMALS selbst — Ahmads
    bewusste Entscheidung (2026-08-08). Gibt die Draft-ID zurueck, oder
    einen ERROR-String bei Fehlschlag."""
    loop = asyncio.get_event_loop()
    try:
        draft_id = await loop.run_in_executor(
            None, _create_draft_reply_sync, to, subject, body, thread_id, in_reply_to
        )
        return draft_id
    except Exception as e:
        return f"ERROR: Entwurf konnte nicht angelegt werden: {e}"


def _archive_message_sync(message_id: str) -> None:
    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds)
    service.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}).execute()


async def archive_message(message_id: str) -> str:
    """Entfernt eine Mail aus dem Posteingang (Gmail-Archiv, nicht geloescht,
    jederzeit ueber 'INBOX'-Label wiederherstellbar). Tier 3, Punkt 13 —
    einzige Aktion die bei hoeherer Vertrauensstufe automatisch passiert,
    bewusst NICHTS was einen Dritten erreicht (Ahmads harte Grenze,
    2026-08-08: kein Auto-Send bei irgendeiner Stufe)."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _archive_message_sync, message_id)
        return "archiviert"
    except Exception as e:
        return f"ERROR: Archivieren fehlgeschlagen: {e}"


async def classify_and_draft_reply(anthropic_client, email: dict, notify_ahmad_fn=None, trust_level: str = "conservative") -> dict:
    """Generalisiert vom Jerome-Klassifikator (jerome_comm.handle_jerome_message,
    gleiches Modell, gleiches JSON-Format, gleiche Brace-Slicing-Parse-Logik),
    aber mit einer vierten Kategorie 'ignore' — ein echtes Postfach enthaelt
    auch automatisierte Mails/Newsletter, die keine Reaktion brauchen.

    ignore: keine Reaktion noetig, nichts passiert.
    routine: Jarvis draftet direkt eine Antwort, Ahmad wird NICHT gestoert.
    notable: Jarvis draftet trotzdem, UND Ahmad bekommt eine Notiz.
    needs_ahmad: KEIN Entwurf, nur eine Notiz dass Ahmad selbst antworten muss.

    Anders als bei Jerome bedeutet 'routine' hier NICHT automatisch senden,
    sondern nur einen Entwurf anlegen — eine E-Mail kann an einen unbekannten/
    externen Empfaenger gehen, ein WhatsApp-Kontakt ist immer ein bekannter
    Mensch. Bewusste Abweichung vom Jerome-Risikoprofil, nicht eine Kopie davon.

    trust_level (Tier 3, Punkt 13): 'conservative' (Default) oder 'confident'.
    Aendert NICHTS am Sende-/Entwurf-Verhalten oben (Ahmads harte Grenze,
    2026-08-08) — einzige Wirkung: bei 'confident' UND category=='ignore'
    wird die Mail automatisch archiviert (archive_message), reversibel,
    erreicht niemanden. Bei 'conservative' passiert bei 'ignore' wie bisher
    nichts.

    Rechnungs-Erkennung (Tier 3, Punkt 15, 2026-08-08, Ahmads ausdruecklicher
    Wunsch): derselbe Haiku-Call erkennt zusaetzlich ob die Mail eine
    Rechnung/ein Beleg ist (is_invoice + Betrag/Waehrung/Datum/Vendor/
    Kategorie) — bewusst im SELBEN Call statt eines zweiten Gmail-Scans,
    keine doppelte Mail-Abfrage. Sehr konservativ: nur bei WIRKLICH klarem
    Betrag+Datum is_invoice=true, im Zweifel false — ein Fehleintrag in
    Ahmads Finanz-Historie waere schlimmer als eine verpasste, aber spaeter
    manuell nachtragbare Rechnung (gleiches Prinzip wie bei der Gedaechtnis-
    Konsolidierung, Tier 2 Punkt 11).

    Returns {"category", "draft_id", "ahmad_note", "archived", "invoice"}.
    "invoice" ist None wenn keine Rechnung erkannt wurde, sonst ein dict
    {"amount", "currency", "date", "vendor", "kategorie"}."""
    prompt = (
        f"Eine E-Mail im Postfach von Ahmad:\n\n"
        f"Von: {email['from']}\nBetreff: {email['subject']}\n\n{email['body'][:3000]}\n\n"
        "WICHTIG fuer die Perspektive, falls du einen draft_body schreibst: das ist Ahmads "
        "Postfach, die Mail kam AN Ahmad — er ist der EMPFAENGER, nicht der Absender. Lies aus dem "
        "Inhalt sorgfaeltig heraus, welche ROLLE Ahmad in diesem Austausch hat (z.B. Kunde eines "
        "Anbieters, nicht der Support-Mitarbeiter des Anbieters, auch wenn die Mail wie ein "
        "Support-Ticket aussieht). Schreibe den Entwurf IMMER aus Ahmads eigener Perspektive an den "
        "Absender — niemals versehentlich in der Rolle des Absenders selbst.\n\n"
        "Entscheide:\n"
        "- 'ignore': automatisierte Mail, Newsletter, Marketing, keine Antwort erwartet oder sinnvoll.\n"
        "- 'routine': einfache Anfrage, du kannst selbstsicher eine passende Antwort entwerfen "
        "(z.B. eine Terminbestaetigung, eine kurze Sachfrage mit klarer Antwort) — du legst einen "
        "Entwurf an, Ahmad wird NICHT informiert.\n"
        "- 'notable': du kannst eine Antwort entwerfen, aber Ahmad sollte es wissen (z.B. eine "
        "echte Chance, eine wichtige Anfrage) — Entwurf UND eine kurze Notiz an Ahmad.\n"
        "- 'needs_ahmad': zu heikel oder unklar fuer einen Entwurf (Zahlungen, Vertraege, "
        "Persoenliches, eine echte Entscheidung) — KEIN Entwurf, nur eine Notiz dass Ahmad selbst "
        "antworten muss.\n\n"
        "draft_body: nur bei 'routine' oder 'notable' — der volle Antworttext auf Deutsch oder "
        "Englisch (gleiche Sprache wie die Original-Mail), hoeflich, kurz, ohne Signatur (wird "
        "nicht automatisch ergaenzt, du schreibst NICHTS was wie eine Signatur aussieht). Bei "
        "'ignore'/'needs_ahmad': null.\n"
        "ahmad_note: nur bei 'notable' oder 'needs_ahmad' — ein klarer Satz auf Deutsch was Ahmad "
        "wissen/tun muss. Sonst null.\n\n"
        "ZUSAETZLICH, unabhaengig von der Kategorie oben: ist das eine Rechnung/ein Zahlungsbeleg "
        "mit einem eindeutig erkennbaren Betrag UND Datum (z.B. Tool-Abo-Rechnung, Kauf-Bestaetigung, "
        "Zahlungsbeleg)? NUR wenn wirklich beides klar erkennbar ist: is_invoice=true, invoice_amount "
        "(nur die Zahl, z.B. 15.00), invoice_currency (z.B. EUR oder USD), invoice_date (Format JJJJ-MM-TT, "
        "aus der Mail oder falls fehlend das Empfangsdatum), invoice_vendor (Anbieter/Absender kurz), "
        "invoice_kategorie (eine von: Software & Tools, Personal, Proxies & Infrastruktur, Sonstiges). "
        "Im Zweifel (unklarer Betrag, keine echte Rechnung, nur eine Werbe-Mail ueber ein Angebot) "
        "IMMER is_invoice=false und alle invoice_*-Felder null — lieber eine echte Rechnung verpassen "
        "als etwas Falsches in Ahmads Finanz-Historie eintragen.\n\n"
        'Antworte NUR als JSON: {"category": "ignore|routine|notable|needs_ahmad", '
        '"draft_body": "..." oder null, "ahmad_note": "..." oder null, '
        '"is_invoice": true/false, "invoice_amount": Zahl oder null, "invoice_currency": "..." oder null, '
        '"invoice_date": "..." oder null, "invoice_vendor": "..." oder null, "invoice_kategorie": "..." oder null}'
    )

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        start, end = raw.index("{"), raw.rindex("}") + 1
        decision = json.loads(raw[start:end])
    except Exception as e:
        return {"category": "error", "draft_id": None, "ahmad_note": f"Klassifikation fehlgeschlagen: {e}", "archived": False, "invoice": None}

    category = decision.get("category", "needs_ahmad")
    draft_body = decision.get("draft_body")
    ahmad_note = decision.get("ahmad_note")
    draft_id = None

    invoice = None
    if decision.get("is_invoice") and decision.get("invoice_amount") and decision.get("invoice_date"):
        invoice = {
            "amount": decision.get("invoice_amount"),
            "currency": decision.get("invoice_currency") or "EUR",
            "date": decision.get("invoice_date"),
            "vendor": decision.get("invoice_vendor") or email.get("from", "Unbekannt"),
            "kategorie": decision.get("invoice_kategorie") or "Sonstiges",
        }

    if category in ("routine", "notable") and draft_body:
        draft_id = await create_draft_reply(
            to=email["from"], subject=email["subject"], body=draft_body,
            thread_id=email["thread_id"], in_reply_to=email.get("message_id_header", ""),
        )

    if category in ("notable", "needs_ahmad") and ahmad_note and notify_ahmad_fn:
        prefix = "Jarvis Update (E-Mail)" if category == "notable" else "Jarvis braucht deine Antwort (E-Mail)"
        await notify_ahmad_fn(f"{prefix}: {ahmad_note}\n\nVon: {email['from']}, Betreff: {email['subject']}")

    archived = False
    if category == "ignore" and trust_level == "confident":
        result = await archive_message(email["id"])
        archived = result == "archiviert"

    return {"category": category, "draft_id": draft_id, "ahmad_note": ahmad_note, "archived": archived, "invoice": invoice}
