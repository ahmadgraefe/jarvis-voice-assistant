"""
Jarvis V2 — Browser Tools
Web search via DuckDuckGo Lite, page visits via Playwright, URL opening.
"""

import json
import re
import webbrowser
import subprocess
from urllib.parse import unquote, parse_qs, urlparse
import httpx
from playwright.async_api import async_playwright

_browser = None
_context = None
_tabs = {}  # Tab-Handle -> Playwright Page, Tier 3 Punkt 16 (2026-08-08)

# Roadmap Punkt 25 — einheitliche Grenze fuer alles was als Rohtext direkt in
# die Konversation geht (vorher drei verschiedene Werte 3000/5000/9000, dazu
# hat server.py das Ergebnis nochmal zusaetzlich auf 2000 gekappt und damit
# den bereits gebauten Fortschritt bei _describe_tab wieder zunichte gemacht).
WEB_CONTENT_MAX_CHARS = 8000


def _bring_chromium_to_front():
    """Bring the Playwright Chromium window to the foreground on macOS.
    Playwright's downloaded browser shows up as "Google Chrome for Testing"
    in the process list, not "Chromium"."""
    try:
        subprocess.run([
            "osascript", "-e",
            'tell application "System Events" to set frontmost of '
            '(first process whose name contains "Chrome for Testing") to true'
        ], capture_output=True, timeout=3)
    except Exception:
        pass


async def _get_browser():
    global _browser, _context
    if _browser is None:
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        _context = await _browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            no_viewport=True,
        )
    return _context


async def search_and_read(query: str) -> dict:
    """Search DuckDuckGo in visible browser, click first result, read the page."""
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        # DuckDuckGo search (no cookie banner, no reCAPTCHA)
        search_url = f"https://duckduckgo.com/?q={query}"
        await page.goto(search_url, timeout=15000)
        _bring_chromium_to_front()
        await page.wait_for_timeout(2000)

        # Click first organic result
        first_link = page.locator('[data-testid="result-title-a"]').first
        if await first_link.count() > 0:
            await first_link.click()
            await page.wait_for_timeout(3000)

            # Read page content
            title = await page.title()
            url = page.url
            text = await page.evaluate("""
                () => {
                    const selectors = ['main', 'article', '[role="main"]', '.content', '#content', 'body'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim().length > 100) {
                            return el.innerText.trim();
                        }
                    }
                    return document.body?.innerText?.trim() || '';
                }
            """)
            return {"title": title, "url": url, "content": text[:WEB_CONTENT_MAX_CHARS]}
        else:
            return {"title": "Keine Ergebnisse", "url": search_url, "content": "Keine Ergebnisse gefunden."}
    except Exception as e:
        return {"error": str(e), "url": query}
    finally:
        pass


async def search_multiple(query: str, count: int = 5) -> list:
    """Tier 3, Punkt 16 — mehrere Suchergebnisse statt blind auf Treffer 1
    zu klicken (das macht search_and_read, bewusst unveraendert gelassen,
    fuer einfache Faktenfragen weiterhin die richtige Wahl). Klickt NICHTS
    an, liest nur die Ergebnisliste selbst (Titel/URL/Snippet) — schnell,
    fuer den Ueberblick vor einer gezielten Auswahl per visit()."""
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        search_url = f"https://duckduckgo.com/?q={query}"
        await page.goto(search_url, timeout=15000)
        _bring_chromium_to_front()
        await page.wait_for_timeout(2000)

        results = await page.evaluate("""
            (count) => {
                const items = document.querySelectorAll('[data-testid="result"]');
                const out = [];
                for (const item of items) {
                    if (out.length >= count) break;
                    const titleEl = item.querySelector('[data-testid="result-title-a"]');
                    if (!titleEl) continue;
                    const title = titleEl.innerText.trim();
                    const url = titleEl.href;
                    // DuckDuckGo hat KEIN stabiles data-testid fuer den Snippet-Text
                    // (nur result/result-title-a/result-extras-* existieren) — daher aus
                    // dem vollen innerText die letzte Zeile nehmen, die weder der Titel
                    // noch die Breadcrumb-URL-Zeile ist.
                    const lines = item.innerText.split("\\n").map(l => l.trim()).filter(Boolean);
                    let snippet = "";
                    for (let i = lines.length - 1; i >= 0; i--) {
                        const l = lines[i];
                        if (l === title || l.includes("\\u203a") || l.startsWith("http")) continue;
                        snippet = l;
                        break;
                    }
                    out.push({title, url, snippet});
                }
                return out;
            }
        """, count)
        return results
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        await page.close()


async def visit(url: str, max_chars: int = WEB_CONTENT_MAX_CHARS) -> dict:
    """Visit a URL and extract main text content."""
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        text = await page.evaluate("""
            () => {
                const selectors = ['main', 'article', '[role="main"]', '.content', '#content', 'body'];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 100) {
                        return el.innerText.trim();
                    }
                }
                return document.body?.innerText?.trim() || '';
            }
        """)
        title = await page.title()
        return {"title": title, "url": url, "content": text[:max_chars]}
    except Exception as e:
        return {"error": str(e), "url": url}
    finally:
        await page.close()


_EXTRACT_CONTENT_JS = """
    () => {
        const selectors = ['main', 'article', '[role="main"]', '.content', '#content', 'body'];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.innerText.trim().length > 100) {
                return el.innerText.trim().replace(/\\n{3,}/g, '\\n\\n');
            }
        }
        return (document.body?.innerText?.trim() || '').replace(/\\n{3,}/g, '\\n\\n');
    }
"""


async def _wait_for_dynamic_content(page):
    """Roadmap Punkt 25 — viele Vergleichs-/Ergebnisseiten (Fluege, Preise,
    Angebote) laden ihre eigentliche Ergebnisliste erst NACH dem initialen
    DOM-Ready nach (Lazy-Load, Infinite-Scroll, async Preis-Abfragen).
    Ohne das zu warten wuerde Jarvis eine halbleere Seite lesen. Manche
    Seiten (Analytics/Polling) werden NIE 'idle' — darf darum nicht haengen
    bleiben, nur best-effort warten."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    for _ in range(3):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(800)


async def _describe_tab(tab_id: str, max_chars: int = WEB_CONTENT_MAX_CHARS) -> dict:
    page = _tabs[tab_id]
    await _wait_for_dynamic_content(page)
    text = await page.evaluate(_EXTRACT_CONTENT_JS)
    return {"tab_id": tab_id, "title": await page.title(), "url": page.url, "content": text[:max_chars]}


async def open_tab(url: str, tab_id: str = None) -> dict:
    """Tier 3, Punkt 16 — oeffnet eine ECHTE, bestehen bleibende Page (anders
    als search_and_read/visit, die jeweils eine Wegwerf-Page pro Aufruf
    nutzen) und merkt sie sich unter einem Handle, damit spaeter gezielt
    dorthin zurueckgewechselt werden kann. Liest den Inhalt sofort mit
    (das Modell 'versteht sofort was gerade geoeffnet wurde'), inklusive
    Warten auf dynamisch nachladende Inhalte (Roadmap Punkt 25, siehe
    _wait_for_dynamic_content)."""
    ctx = await _get_browser()
    if not tab_id:
        tab_id = f"tab{len(_tabs) + 1}"
    page = await ctx.new_page()
    try:
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
    except Exception as e:
        await page.close()
        return {"error": str(e), "url": url}
    _tabs[tab_id] = page
    _bring_chromium_to_front()
    return await _describe_tab(tab_id)


async def switch_tab(tab_id: str) -> dict:
    """Holt einen bereits offenen Tab nach vorne und liest seinen AKTUELLEN
    Inhalt (kann sich seit dem letzten Lesen veraendert haben, z.B. nach
    einem vorherigen click_element)."""
    if tab_id not in _tabs:
        return {"error": f"Kein offener Tab mit Handle '{tab_id}'. Offene Tabs: {list(_tabs.keys())}"}
    await _tabs[tab_id].bring_to_front()
    _bring_chromium_to_front()
    return await _describe_tab(tab_id)


async def list_tabs() -> list:
    """Alle aktuell offenen Tabs mit Titel/URL, fuer Ueberblick bei
    mehreren parallel offenen Seiten (Vergleichsaufgaben)."""
    out = []
    for tab_id, page in list(_tabs.items()):
        if page.is_closed():
            del _tabs[tab_id]
            continue
        out.append({"tab_id": tab_id, "title": await page.title(), "url": page.url})
    return out


async def close_tab(tab_id: str) -> str:
    if tab_id not in _tabs:
        return f"Kein offener Tab mit Handle '{tab_id}'."
    await _tabs[tab_id].close()
    del _tabs[tab_id]
    return f"Tab '{tab_id}' geschlossen."


async def read_tab(tab_id: str) -> dict:
    """Liest einen bereits offenen Tab neu, ohne ihn nach vorne zu holen —
    z.B. um zu pruefen was sich nach einer Aktion geaendert hat, ohne den
    sichtbaren Fokus zu wechseln."""
    if tab_id not in _tabs:
        return {"error": f"Kein offener Tab mit Handle '{tab_id}'. Offene Tabs: {list(_tabs.keys())}"}
    return await _describe_tab(tab_id)


# Eigener, groesserer Wert NUR fuer den Extraktions-Input (geht an Claude als
# Zwischenschritt, nicht direkt an die Konversation, darf darum grosszuegiger
# sein als WEB_CONTENT_MAX_CHARS).
EXTRACTION_INPUT_MAX_CHARS = 20000


async def extract_structured(client, tab_id: str, what: str) -> dict:
    """Roadmap Punkt 25 — verwandelt den Fliesstext eines bereits offenen Tabs
    in saubere Felder (Preis/Anbieter/Zeit/Ort etc.) statt den rohen Text dem
    Modell zum Selbst-Interpretieren zu ueberlassen. Gleiche Konvention wie
    gmail_tools.classify_and_draft_reply: Client wird reingereicht, Prompt
    verlangt explizit reines JSON, Parsing per index/rindex-Slice vor
    json.loads, ehrlicher Fehler statt Rateversuch bei Parse-Problemen."""
    if tab_id not in _tabs:
        return {"error": f"Kein offener Tab mit Handle '{tab_id}'. Offene Tabs: {list(_tabs.keys())}"}
    page = _tabs[tab_id]
    await _wait_for_dynamic_content(page)
    text = await page.evaluate(_EXTRACT_CONTENT_JS)
    text = text[:EXTRACTION_INPUT_MAX_CHARS]
    if not text.strip():
        return {"tab_id": tab_id, "url": page.url, "items": []}

    prompt = (
        f"Hier ist der Text einer Webseite:\n\n{text}\n\n"
        f"Extrahiere ALLE Eintraege zu: {what}\n"
        "Antworte NUR als JSON-Array, jedes Element ein Objekt mit sinnvollen, kurzen "
        "deutschen Feldnamen passend zur Anfrage (z.B. preis, anbieter, zeit, ort). "
        "Fehlt ein Feld erkennbar auf der Seite, nutze null statt zu raten. "
        "Steht nichts Passendes auf der Seite, antworte mit []."
    )
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        start, end = raw.index("["), raw.rindex("]") + 1
        items = json.loads(raw[start:end])
        return {"tab_id": tab_id, "url": page.url, "items": items}
    except Exception as e:
        return {"error": f"Strukturierte Extraktion fehlgeschlagen: {e}"}


async def _find_element(page, description: str):
    """DOM-basierte Element-Suche statt Vision+Koordinaten-Raten (SCREENCLICK) —
    strukturell zuverlaessiger innerhalb des von Playwright kontrollierten
    Browsers. Probiert in Reihenfolge: Rolle+Name (Buttons/Links), sichtbarer
    Text, Label (Formularfelder). Gibt None zurueck wenn nichts eindeutig
    passt — NIE raten welches Element gemeint war."""
    candidates = [
        page.get_by_role("button", name=description, exact=False),
        page.get_by_role("link", name=description, exact=False),
        page.get_by_text(description, exact=False),
        page.get_by_label(description, exact=False),
    ]
    for locator in candidates:
        try:
            count = await locator.count()
        except Exception:
            continue
        if count == 0:
            continue
        if count == 1:
            return locator.first
        # Mehrere Treffer: den ersten SICHTBAREN nehmen statt blind locator.first
        # (oft gibt es ein verstecktes Duplikat, z.B. ein Mobile-Menu-Analog).
        for i in range(count):
            candidate = locator.nth(i)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return locator.first
    return None


async def click_element(tab_id: str, description: str) -> str:
    """DOM-basierter Klick (Tier 3, Punkt 16) — kein Screenshot, kein
    Koordinaten-Raten wie bei SCREENCLICK, dafuer NUR innerhalb dieses
    Playwright-Browsers nutzbar (kein DOM ausserhalb davon). Findet nichts
    eindeutig Passendes -> klickt NICHTS, ehrlicher Hinweis statt Vermutung."""
    if tab_id not in _tabs:
        return f"ERROR: Kein offener Tab mit Handle '{tab_id}'."
    page = _tabs[tab_id]
    element = await _find_element(page, description)
    if element is None:
        return f"ERROR: Kein eindeutiges Element gefunden fuer '{description}' — nichts geklickt."
    try:
        await element.click(timeout=5000)
        # Roadmap Punkt 25 — ein Klick (z.B. "Suchen" bei einem Vergleich)
        # kann eine Ergebnisliste anstossen, die mehrere Sekunden braucht.
        # Gleiches Warten wie beim Tab-Lesen, damit ein direkt folgendes
        # browser_read_tab nicht auf eine halbfertige Seite trifft.
        await _wait_for_dynamic_content(page)
        return f"Geklickt: '{description}'."
    except Exception as e:
        return f"ERROR: Klick fehlgeschlagen: {e}"


async def fill_field(tab_id: str, field_description: str, value: str) -> str:
    """DOM-basiertes Ausfuellen eines Formularfelds (Label/Placeholder/
    Textbox-Rolle), gleiches 'nie raten'-Prinzip wie click_element."""
    if tab_id not in _tabs:
        return f"ERROR: Kein offener Tab mit Handle '{tab_id}'."
    page = _tabs[tab_id]
    candidates = [
        page.get_by_label(field_description, exact=False),
        page.get_by_placeholder(field_description, exact=False),
        page.get_by_role("textbox", name=field_description, exact=False),
    ]
    for locator in candidates:
        try:
            if await locator.count() == 1:
                await locator.first.fill(value, timeout=5000)
                return f"Feld '{field_description}' ausgefuellt."
        except Exception:
            continue
    return f"ERROR: Kein eindeutiges Feld gefunden fuer '{field_description}' — nichts ausgefuellt."


async def fetch_news() -> str:
    """Fetch current world news from worldmonitor.app in visible browser."""
    ctx = await _get_browser()
    page = await ctx.new_page()
    try:
        await page.goto("https://www.worldmonitor.app/", timeout=20000)
        _bring_chromium_to_front()
        await page.wait_for_timeout(6000)  # Wait for JS to render
        text = await page.evaluate("() => document.body.innerText")
        # Extract the news sections
        content = text[:4000]
        return f"World Monitor Nachrichten:\n{content}"
    except Exception as e:
        return f"News konnten nicht geladen werden: {e}"
    finally:
        pass  # Keep page open so user can see it


async def open_url(url: str):
    """Open URL in user's default browser (non-blocking)."""
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, webbrowser.open, url)
    return {"success": True, "url": url}


async def close():
    global _browser, _context
    if _browser:
        await _browser.close()
        _browser = None
        _context = None
