"""TikTok — nur LESEN, nur oeffentlich, kein Login.

Ahmad, 2026-08-12: er wollte wissen, ob und WANN ein Reel/Video wirklich
draussen ist, und zwar auch auf TikTok — dort postet er laut
claude_app_status.md genauso wie auf Instagram. Fuer Instagram gab es dafuer
schon Werkzeuge (trial_reel_check, reel_analysis, video_analysis), fuer TikTok
gab es im ganzen Projekt keine einzige Zeile Code.

Was dieses Modul kann:
  * `published_at()` — den EXAKTEN Veroeffentlichungszeitpunkt aus der
    Video-ID rechnen. TikTok kodiert den Unix-Zeitstempel in den oberen 32 Bit
    der 19-stelligen Video-ID; das ist reine Arithmetik, braucht kein Netz und
    funktioniert auch dann noch, wenn TikTok das Scrapen blockt.
  * `get_recent_video_links()` — die neuesten Videos eines oeffentlichen
    Profils (fuer "ist da inzwischen was gepostet worden?").
  * `check_video_public_stats()` — die oeffentlich sichtbaren Zahlen EINES
    Videos per Screenshot + Vision, Screenshot wird lokal abgelegt.

Was dieses Modul bewusst NICHT kann und nie koennen soll: einloggen, posten,
liken, kommentieren, oder das TikTok-Analytics-/Insights-Panel oeffnen. Die
Insights sieht nur, wer IM Posting-Account eingeloggt ist — auf den echten
Posting-Accounts wird nicht eingeloggt und nicht getippt (feste Grenzen 2+3 in
claude_app_status.md, Ban-Risiko). Fehler werden zurueckgegeben, nie
verschluckt: "Profil nicht erreichbar" darf niemals als "es wurde nichts
gepostet" ankommen.
"""

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SHOTS_DIR = os.path.join(os.path.dirname(__file__), "memory", "tiktok_screenshots")
LOG_PATH = os.path.join(os.path.dirname(__file__), "memory", "tiktok.log")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BERLIN = ZoneInfo("Europe/Berlin")

# TikTok-IDs vor ~2019 sind kuerzer und tragen den Zeitstempel nicht in den
# oberen Bits. Ein Ergebnis ausserhalb dieses Fensters ist deshalb kein
# Zeitpunkt, sondern ein Hinweis darauf, dass die Rechnung hier nicht gilt.
_PLAUSIBLE_FROM = datetime(2016, 1, 1, tzinfo=timezone.utc)

_playwright = None
_browser = None
_context = None


def _log(msg: str):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def own_accounts() -> list:
    """Eigene TikTok-Handles aus config.json ("tiktok_accounts").

    Bewusst eine eigene Liste und NICHT die Instagram-Handles: dass ein
    Instagram-Name auf TikTok derselben Person gehoert, ist eine Annahme, und
    genau solche Annahmen sollen hier nicht als Fakt durchgehen. Fehlt der
    Schluessel, ist die Liste leer — das Werkzeug sagt das dann offen, statt
    sich Accounts auszudenken."""
    cfg = _load_config()
    return [str(a).strip().lstrip("@") for a in cfg.get("tiktok_accounts", []) if str(a).strip()]


def canonical_video_url(url: str) -> str:
    """URL auf eine navigierbare TikTok-Video-/Foto-URL bringen, sonst ''.

    Erlaubt sind /@handle/video/ID, /@handle/photo/ID und die Kurzlinks
    (vm./vt.tiktok.com/XXXX) aus dem Teilen-Menue — bei denen steht die ID
    erst nach einer Weiterleitung fest, deshalb liefert video_id() dort
    nichts und der Zeitpunkt kommt aus der Seite statt aus der ID."""
    url = (url or "").strip().strip('<>"\'')
    if not url:
        return ""
    if not re.match(r'^https?://', url, re.I):
        url = "https://" + url.lstrip("/")
    if not re.match(r'^https?://(?:[\w-]+\.)*tiktok\.com/', url, re.I):
        return ""
    if re.search(r'/(?:video|photo)/\d+', url):
        return url
    if re.match(r'^https?://(?:vm|vt)\.tiktok\.com/[\w-]+', url, re.I):
        return url
    return ""


def video_id(url: str) -> str:
    """Die numerische Video-ID aus einer TikTok-URL, sonst ''."""
    match = re.search(r'/(?:video|photo)/(\d+)', url or "")
    return match.group(1) if match else ""


def normalize_video_url(url: str) -> str:
    """Stabiler Vergleichsschluessel fuer dieselbe TikTok-URL in
    verschiedenen Formen (mit/ohne Query, mit/ohne www) — dasselbe Problem,
    das instagram_tools.normalize_video_url fuer Instagram loest."""
    vid = video_id(url)
    return vid or (url or "").strip().rstrip("/").lower()


def published_at(url_or_id: str) -> dict:
    """Veroeffentlichungszeitpunkt aus der TikTok-Video-ID.

    TikTok legt den Unix-Zeitstempel der Erstellung in die oberen 32 Bit der
    19-stelligen Video-ID (id >> 32). Das ist der Grund, warum dieses Werkzeug
    "wann wurde das gepostet?" auch dann exakt beantworten kann, wenn das
    Scrapen scheitert: es wird nichts geladen, nur gerechnet.

    Rueckgabe: {"id", "iso", "text", "unix", "error"} — bei allem, was nicht
    plausibel ist (zu kurze/alte ID, Kurzlink ohne ID, Zukunftsdatum), steht
    ein "error" drin und KEIN geratener Zeitpunkt."""
    raw = (url_or_id or "").strip()
    vid = raw if raw.isdigit() else video_id(raw)
    result = {"id": vid, "iso": None, "text": None, "unix": None, "error": None}
    if not vid:
        result["error"] = (
            "keine Video-ID in der URL (bei Kurzlinks vm./vt.tiktok.com steht die ID erst nach "
            "der Weiterleitung fest — dann den langen Link aus dem Teilen-Menue nehmen)"
        )
        return result
    try:
        unix = int(vid) >> 32
        moment = datetime.fromtimestamp(unix, tz=timezone.utc)
    except (ValueError, OSError, OverflowError) as e:
        result["error"] = f"ID nicht in einen Zeitpunkt umrechenbar ({e.__class__.__name__}: {e})"
        return result
    if not (_PLAUSIBLE_FROM <= moment <= datetime.now(timezone.utc)):
        result["error"] = (
            f"aus der ID errechneter Zeitpunkt ({moment.date()}) ist unplausibel — bei sehr alten "
            "oder untypisch kurzen IDs traegt die ID den Zeitstempel nicht, hier wird deshalb "
            "keiner genannt"
        )
        return result
    local = moment.astimezone(BERLIN)
    result["unix"] = unix
    result["iso"] = local.isoformat()
    result["text"] = local.strftime("%d.%m.%Y %H:%M Uhr (Berlin)")
    return result


async def _get_context():
    """Headless, OHNE gespeicherte Session — es wird nirgends eingeloggt.

    TikTok ist deutlich scrape-unfreundlicher als Instagram (Captcha/Bot-Wall
    kommt vor). Genau deshalb geben alle Funktionen hier ihren Fehler
    zurueck, statt ihn zu schlucken: eine Bot-Wall ist eine unbeantwortete
    Frage, kein "nichts gepostet"."""
    global _playwright, _browser, _context
    if _context is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        _context = await _browser.new_context(user_agent=UA, locale="de-DE")
    return _context


def reset_browser():
    """Singletons verwerfen (gleiche Idee wie instagram_tools.reset_browser):
    sofort synchron auf None, Aufraeumen nur best effort im Hintergrund, damit
    ein haengender Browser den naechsten Aufruf nicht mitreisst."""
    global _playwright, _browser, _context
    old_browser, old_playwright = _browser, _playwright
    _playwright, _browser, _context = None, None, None
    if old_browser is None:
        return

    async def _cleanup():
        try:
            await asyncio.wait_for(old_browser.close(), timeout=10)
        except Exception:
            pass
        if old_playwright is not None:
            try:
                await asyncio.wait_for(old_playwright.stop(), timeout=10)
            except Exception:
                pass

    try:
        asyncio.get_event_loop().create_task(_cleanup())
    except RuntimeError:
        pass


async def _goto_tolerant(page, url: str, timeout: float = 25000, load_timeout: float = 10000):
    """Wie in instagram_tools: nach dem geparsten DOM zurueckkommen und dem
    vollstaendigen 'load' nur noch eine begrenzte Chance geben — ein einzelnes
    haengendes Video-Asset soll nicht den ganzen Datenpunkt kosten."""
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("load", timeout=load_timeout)
    except Exception:
        pass


async def _screenshot(page, timeout: float = 15000, attempts: int = 2) -> bytes:
    last_error = None
    for attempt in range(attempts):
        try:
            return await page.screenshot(type="png", timeout=timeout, animations="disabled")
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                await page.wait_for_timeout(1500)
    raise last_error


async def _looks_blocked(page) -> str:
    """Erkennt die typischen TikTok-Abwehrseiten. Gibt einen Klartext-Grund
    zurueck oder '' — damit im Ergebnis "Bot-Wall" steht und nicht das
    irrefuehrende "keine Videos gefunden"."""
    try:
        text = ((await page.inner_text("body"))[:3000]).lower()
    except Exception:
        return ""
    for needle, reason in (
        ("verify to continue", "TikTok hat eine Captcha-/Verifizierungsseite ausgeliefert"),
        ("security check", "TikTok hat eine Sicherheitspruefung ausgeliefert"),
        ("sicherheitsupruefung", "TikTok hat eine Sicherheitspruefung ausgeliefert"),
        ("sicherheitsprüfung", "TikTok hat eine Sicherheitspruefung ausgeliefert"),
        ("couldn't find this account", "TikTok kennt diesen Account nicht"),
        ("konnte dieses konto nicht finden", "TikTok kennt diesen Account nicht"),
        ("video currently unavailable", "TikTok meldet das Video als nicht verfuegbar"),
        ("dieses video ist nicht verfügbar", "TikTok meldet das Video als nicht verfuegbar"),
    ):
        if needle in text:
            return reason
    return ""


async def get_recent_video_links(handle: str, limit: int = 5) -> dict:
    """Die neuesten Video-Links eines oeffentlichen TikTok-Profils.

    Rueckgabe {"links": [...], "error": None|Text, "blocked": None|Grund}.
    Der Fehler wird ZURUECKGEGEBEN (nicht nur geloggt), weil die Frage
    dahinter meist "wurde da inzwischen was gepostet?" lautet — und ein
    stiller Fehler dort die gefaehrlichste aller Antworten waere."""
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        return {"links": [], "error": "kein Handle angegeben", "blocked": None}
    page = None
    try:
        ctx = await _get_context()
        page = await ctx.new_page()
        await _goto_tolerant(page, f"https://www.tiktok.com/@{handle}")
        await page.wait_for_timeout(4000)
        blocked = await _looks_blocked(page)
        hrefs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="/photo/"]'))
                .map(a => a.href)"""
        )
        links = []
        for href in hrefs:
            if href not in links:
                links.append(href)
            if len(links) >= limit:
                break
        _log(f"@{handle}: {len(links)} Links{' (blockiert: ' + blocked + ')' if blocked else ''}")
        return {"links": links, "error": None, "blocked": blocked or None}
    except Exception as e:
        _log(f"@{handle}: ERROR {e}")
        return {"links": [], "error": f"{e.__class__.__name__}: {e}", "blocked": None}
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def check_video_public_stats(url: str, anthropic_client, save_screenshot: bool = True) -> dict:
    """Die OEFFENTLICH sichtbaren Zahlen EINES TikTok-Videos (Views/Likes/
    Kommentare/Shares) per Screenshot + Vision, plus optional der Screenshot
    als PNG.

    Ausdruecklich die oeffentliche Video-Ansicht, NICHT das Analytics-Panel:
    Reach, Publikums-Herkunft und Zuschauerbindung zeigt TikTok nur innerhalb
    des Accounts, in dem gepostet wurde — dort wird bewusst nicht eingeloggt.
    """
    result = {"url": url, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    page = None
    png_bytes = None
    try:
        ctx = await _get_context()
        page = await ctx.new_page()
        await _goto_tolerant(page, url)
        await page.wait_for_timeout(4000)
        blocked = await _looks_blocked(page)
        if blocked:
            result["blocked"] = blocked
        # Die endgueltige URL nach einer Kurzlink-Weiterleitung — erst damit
        # laesst sich bei vm./vt.-Links ueberhaupt eine Video-ID (und daraus
        # der Zeitpunkt) bestimmen.
        result["final_url"] = page.url
        png_bytes = await _screenshot(page)
    except Exception as e:
        _log(f"VIDEO {url}: ERROR {e}")
        result["error"] = f"{e.__class__.__name__}: {e}"
        return result
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    if save_screenshot:
        try:
            os.makedirs(SHOTS_DIR, exist_ok=True)
            path = os.path.join(
                SHOTS_DIR,
                f"{normalize_video_url(result.get('final_url') or url)}_"
                f"{time.strftime('%Y%m%d-%H%M%S')}.png",
            )
            with open(path, "wb") as f:
                f.write(png_bytes)
            result["screenshot_path"] = path
        except OSError as e:
            result["screenshot_error"] = str(e)

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png_bytes).decode("utf-8")}},
                    {"type": "text", "text": (
                        "Das ist ein Screenshot einer oeffentlichen TikTok-Video-Seite. Lies die "
                        "sichtbaren Statistiken ab: Aufrufe/Views, Likes, Kommentare, Shares, und "
                        "falls sichtbar das Veroeffentlichungsdatum. Antworte NUR im Format "
                        "'views=X likes=Y comments=Z shares=W posted=D' mit den Werten, die du "
                        "wirklich siehst (schreibe 'unbekannt' fuer alles, was nicht sichtbar ist "
                        "— rate nichts). Falls stattdessen eine Captcha-/Login-/Fehlerseite zu "
                        "sehen ist, antworte nur 'blockiert'. Keine weiteren Erklaerungen."
                    )},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        if raw.lower().startswith("blockiert"):
            result["blocked"] = result.get("blocked") or (
                "die geladene Seite zeigte keine Video-Ansicht (Captcha-/Login-/Fehlerseite)"
            )
        else:
            result["raw"] = raw
        _log(f"VIDEO {url}: {raw}")
    except Exception as e:
        _log(f"VIDEO {url}: Vision ERROR {e}")
        result["error"] = f"Zahlen nicht auswertbar ({e.__class__.__name__}: {e})"
    return result
