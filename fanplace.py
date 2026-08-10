"""
Jarvis V2 — Fanplace
Logs into Fanplace and reads the Creator Snapshot widget (earnings, new
subscribers) directly from the page — exact numbers, not vision-guessed.
"""

import asyncio
import json
import os
import re
import time

from playwright.async_api import async_playwright

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "fanplace_session.json")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht,
# _log() unten hat das bisher still verschluckt (except OSError: pass).
LOG_PATH = (
    "/var/log/jarvis-fanplace.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-fanplace.log")
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_playwright = None
_browser = None
_context = None

# Short-lived cache — the real scrape takes ~11s (deliberate retry-wait loop
# below, don't touch that), which is fine once but was the dominant cost
# whenever "Jarvis, lets go" fires more than once in quick succession (e.g.
# testing, or repeated double-claps). Earnings/subscriber counts don't move
# meaningfully within 2 minutes, so reusing the last read is safe.
_cache_result = None
_cache_time = 0.0
CACHE_TTL_SECONDS = 120


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _parse_snapshot(text: str) -> dict:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    result = {"earnings": {}, "subscribers": {}}
    section = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "EARNINGS":
            section = "earnings"
        elif "SUBSCRIBER" in line:
            section = "subscribers"
        elif section and i + 1 < len(lines) and line not in ("View dashboard", "→"):
            result[section][line] = lines[i + 1]
            i += 1
        i += 1
    return result


def _format_for_jarvis(data: dict) -> str:
    e = data.get("earnings", {})
    s = data.get("subscribers", {})
    return (
        f"Fanplace Earnings — Heute: {e.get('Today', '?')}, Diese Woche: {e.get('This week', '?')}, "
        f"Diesen Monat: {e.get('This month', '?')}, All-Time: {e.get('ALL-TIME', '?')}.\n"
        f"Neue Abonnenten — Heute: {s.get('Today', '?')}, Diese Woche: {s.get('This week', '?')}, "
        f"Diesen Monat: {s.get('This month', '?')}, Aktive Abonnenten gesamt: {s.get('ACTIVE', '?')}."
    )


async def _get_frontmost_app() -> str:
    """Leerer String wenn nicht ermittelbar (z.B. 'osascript' existiert auf
    dem Hetzner-Server gar nicht) — _restore_focus() no-opt dann einfach,
    macOS-Fensterverwaltung ist auf dem unsichtbaren Xvfb-Server ohnehin
    bedeutungslos, siehe screen_capture.py's identisches Muster."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "System Events" to name of first application process whose frontmost is true',
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
    except Exception:
        return ""


async def _restore_focus(app_name: str):
    """Launching a headed Chrome instance steals frontmost focus regardless of
    window position — hand it back to whatever you were actually looking at."""
    if not app_name:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", f'tell application "{app_name}" to activate'
        )
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        pass


async def _move_window_offscreen():
    """macOS ignores Chrome's --window-position flag on launch (window still shows
    up at the default on-screen spot), so instead: move it off-screen via System
    Events right after the window actually exists, THEN minimize it — belt and
    suspenders. Off-screen positioning alone can still flash briefly at creation
    or resurface via Cmd+Tab/Mission Control; minimizing sends it to the Dock
    where it can't visibly interrupt anything, and Playwright drives it via CDP
    regardless of on-screen/minimized state so screenshots etc. still work fine.
    The Playwright-managed browser shows up as process "Google Chrome for
    Testing", not "Chromium".

    server.py and background_brain.py are TWO SEPARATE Python processes,
    each with its OWN module-level _browser it launches once and reuses —
    so as soon as BOTH have made at least one Fanplace call, there are
    legitimately TWO processes both named "Google Chrome for Testing" at
    once. Confirmed live (2026-08-06): AppleScript's singular `process "X"`
    reference becomes unreliable once there are two same-named processes —
    it can resolve to the "wrong" one (wrong window state, or none yet),
    causing this to silently time out even though a usable window existed
    the whole time under the OTHER process. Fix: operate on EVERY matching
    process and EVERY one of its windows, not a single implicit reference —
    correct regardless of how many instances happen to be alive.
    Retry budget generous (up to ~15s) as a separate safety margin for
    genuine cold-boot slowness on top of that."""
    script = (
        'tell application "System Events"\n'
        '    repeat 50 times\n'
        '        set matchingProcs to (every process whose name is "Google Chrome for Testing")\n'
        '        if (count of matchingProcs) > 0 then\n'
        '            set anyMoved to false\n'
        '            repeat with proc in matchingProcs\n'
        '                repeat with w in windows of proc\n'
        '                    set position of w to {-3000, 0}\n'
        '                    set value of attribute "AXMinimized" of w to true\n'
        '                    set anyMoved to true\n'
        '                end repeat\n'
        '            end repeat\n'
        '            if anyMoved then return "OK"\n'
        '        end if\n'
        '        delay 0.3\n'
        '    end repeat\n'
        '    return "TIMEOUT"\n'
        'end tell'
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    outcome = stdout.decode().strip()
    if outcome != "OK":
        _log(f"WARNUNG: Fenster konnte nicht rechtzeitig weggeschoben werden (Ergebnis: {outcome!r})")
    return outcome == "OK"


async def _get_context():
    global _playwright, _browser, _context
    if _context is None:
        _playwright = await async_playwright().start()
        # headless=True gets blocked by Cloudflare's bot check, so this has to be a
        # real (non-headless) browser — moved off-screen (once a page/window
        # actually exists — see _move_window_offscreen) so it never pops up in
        # front of you or steals focus, even briefly.
        _browser = await _playwright.chromium.launch(headless=False)
        storage = SESSION_PATH if os.path.exists(SESSION_PATH) else None
        _context = await _browser.new_context(storage_state=storage, user_agent=UA)
    return _context


def reset_browser():
    """Roadmap Punkt 21 — gleiches Muster wie instagram_tools.reset_browser()."""
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


async def _login(page) -> bool:
    email_input = await page.query_selector('input[placeholder="Email"]')
    if not email_input:
        return False
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    await page.fill('input[placeholder="Email"]', config["fanplace_email"])
    await page.fill('input[placeholder="Password"]', config["fanplace_password"])
    await page.click('button:has-text("Login")')
    await page.wait_for_timeout(4000)
    return True


def _looks_like_placeholder(data: dict) -> bool:
    """The widget renders with $0.00 / 0 placeholders for a moment before the real
    numbers arrive via an async fetch — an all-time total of exactly $0.00 on an
    active account is what gives away that we read it too early."""
    return data.get("earnings", {}).get("ALL-TIME") in ("$0.00", None)


async def get_snapshot() -> str:
    """Human-readable formatted string — what every existing caller wants."""
    data = await get_snapshot_data()
    if isinstance(data, str):  # error passthrough
        return data
    return _format_for_jarvis(data)


async def get_snapshot_data():
    """Cached wrapper, same TTL as get_snapshot() — returns the RAW parsed
    dict ({"earnings": {...}, "subscribers": {...}}) instead of the
    formatted string, for callers that need actual numbers (e.g. comparing
    against a previous check to catch a subscriber drop). Returns an
    'ERROR: ...' string on failure, same convention as get_snapshot()."""
    global _cache_result, _cache_time
    now = time.monotonic()
    if _cache_result is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        _log(f"cache hit ({now - _cache_time:.0f}s alt)")
        return _cache_result

    result = await _fetch_snapshot_data()
    if not (isinstance(result, str) and result.startswith("ERROR:")):
        _cache_result = result
        _cache_time = now
    return result


async def _fetch_snapshot_data():
    """Log in (reusing a saved session if possible) and read earnings/subscriber numbers.
    Retries a few times — Fanplace's client-side rendering can be slow to settle,
    especially while other apps are also launching from the same double-clap."""
    last_error = None
    for attempt in range(1, 4):
        try:
            previous_app = await _get_frontmost_app()
            ctx = await _get_context()
            page = await ctx.new_page()
            try:
                moved = False
                try:
                    moved = await _move_window_offscreen()
                except Exception:
                    pass
                await _restore_focus(previous_app)
                if not moved:
                    # The move itself timed out, meaning the window may well
                    # come into existence (and grab focus) AFTER the restore
                    # above already ran — one more restore a beat later
                    # catches that late arrival instead of leaving it frontmost.
                    await asyncio.sleep(1.5)
                    await _restore_focus(previous_app)
                await page.goto("https://fanplace.com/", timeout=20000)
                await page.wait_for_timeout(3000)

                if not await page.query_selector("a.fp-creator-snapshot"):
                    _log(f"attempt {attempt}: snapshot not present, trying login")
                    await _login(page)
                    await page.goto("https://fanplace.com/", timeout=20000)

                await page.wait_for_selector("a.fp-creator-snapshot", timeout=15000)

                # Give the async earnings fetch time to settle — re-read a couple
                # times if it still looks like the initial zero placeholder.
                data = {}
                for _ in range(4):
                    await page.wait_for_timeout(1500)
                    text = await page.inner_text("a.fp-creator-snapshot")
                    data = _parse_snapshot(text)
                    if not _looks_like_placeholder(data):
                        break

                await ctx.storage_state(path=SESSION_PATH)
                _log(f"attempt {attempt}: success — {text[:80]!r}")
                return data
            finally:
                await page.close()
        except Exception as e:
            last_error = e
            _log(f"attempt {attempt}: failed — {e}")
            await asyncio.sleep(2)

    return f"ERROR: Fanplace-Daten konnten nicht geladen werden: {last_error}"


# ---------------------------------------------------------------------------
# Affiliate links — the ONLY place Fanplace attributes subs/clicks to a
# SPECIFIC bio-link placement instead of one combined account-wide number
# (Ahmad, 2026-08-07, pointed here for Link Funnel's per-account New Subs).
# Each link's short token (e.g. "1fU7EdP83l") is literally the start of the
# '?l_=' tracking parameter on the matching SLT.bio bio-link URL — confirmed
# by comparing a real link side-by-side — but for OUR purposes the label
# text itself already says which account a link belongs to ("Instagram Bio
# (@lunaxvale)"), so no cross-referencing against SLT.bio is even needed.
# ---------------------------------------------------------------------------

_AFFILIATE_LINK_PATTERN = re.compile(
    r'^(?P<label>[^\n(]+\([^\n)]+\))\n'
    r'(?P<token>[A-Za-z0-9]{6,20})\n'
    r'.*?TOTAL CLICKS\n(?P<clicks>[\d,]+)\n'
    r'.*?TOTAL SUBS\n(?P<subs>[\d,]+)\n'
    r'.*?Edit\n•\nCopy Link\n•\nArchive',
    re.MULTILINE | re.DOTALL,
)


def _parse_affiliate_links(text: str) -> list:
    """Anchored on the fixed structure of each link card (label+parens, then
    a short token, then eventually TOTAL CLICKS/TOTAL SUBS, then the
    Edit/Copy Link/Archive action row) rather than naively splitting on that
    action row — a naive split leaves the 'Created: <date> • Daily Stats'
    trailer of block N sitting at the START of block N+1's text, which was
    read as if it were block N+1's own label/token (caught live, 2026-08-07:
    'label' came back as 'Created:' and 'token' as a date string)."""
    results = []
    for m in _AFFILIATE_LINK_PATTERN.finditer(text):
        results.append({
            "label": m.group("label").strip(),
            "token": m.group("token").strip(),
            "total_clicks": int(m.group("clicks").replace(",", "")),
            "total_subs": int(m.group("subs").replace(",", "")),
        })
    return results


async def get_affiliate_links() -> list:
    """Per-link (per bio-page placement) CUMULATIVE click/sub totals, straight
    from Fanplace's own dashboard — exact numbers, no OCR/vision involved.
    'Cumulative' matters: there's no reliable way to pull a specific past
    week's numbers without driving Fanplace's own date-range picker (fragile
    UI automation this project has repeatedly gotten burned by) — callers
    that need a WEEKLY figure should diff two cumulative snapshots instead
    (see background_brain.py's link_funnel_pass)."""
    previous_app = await _get_frontmost_app()
    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        moved = False
        try:
            moved = await _move_window_offscreen()
        except Exception:
            pass
        await _restore_focus(previous_app)
        if not moved:
            await asyncio.sleep(1.5)
            await _restore_focus(previous_app)

        await page.goto("https://fanplace.com/affiliate/links", timeout=20000)
        await page.wait_for_timeout(3000)
        if not await page.query_selector("text=TOTAL CLICKS"):
            await _login(page)
            await page.goto("https://fanplace.com/affiliate/links", timeout=20000)
            await page.wait_for_timeout(3000)

        text = await page.inner_text("body")
        await ctx.storage_state(path=SESSION_PATH)
    finally:
        await page.close()

    links = _parse_affiliate_links(text)
    _log(f"Affiliate-Links gelesen: {len(links)} Eintraege.")
    return links


async def get_payout_history() -> list:
    """Echte Payout-Historie mit Datum, Brutto/Fees/Netto von
    https://payouts.fanplace.com/ — Grundlage fuer Tier 3 Punkt 15
    (Finanz-Tracking), zunaechst genutzt um den neuen Kosten-/Einnahmen-
    Tracker mit Ahmads tatsaechlicher Historie vorzubefuellen. Liest ueber
    die echte DOM-Tabellenstruktur (nicht ueber flachen inner_text, der bei
    anderen Fanplace-Seiten schon zu Fehl-Parsing gefuehrt hat, siehe
    _parse_affiliate_links), damit Zahlen niemals verwechselt werden.
    Gibt die Werte UNVERAENDERT wieder, wie Fanplace sie zeigt — auch wenn
    eine Zeile rechnerisch nicht ganz aufgeht (beobachtet, 2026-08-08),
    niemals selbst 'korrigieren' oder schaetzen."""
    previous_app = await _get_frontmost_app()
    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        moved = False
        try:
            moved = await _move_window_offscreen()
        except Exception:
            pass
        await _restore_focus(previous_app)
        if not moved:
            await asyncio.sleep(1.5)
            await _restore_focus(previous_app)

        await page.goto("https://payouts.fanplace.com/", timeout=20000)
        await page.wait_for_timeout(3000)
        if not await page.query_selector("table"):
            await _login(page)
            await page.goto("https://payouts.fanplace.com/", timeout=20000)
            await page.wait_for_timeout(3000)

        table = await page.query_selector("table")
        rows = await table.query_selector_all("tbody tr") if table else []
        payouts = []
        for r in rows:
            cells = await r.query_selector_all("td")
            if len(cells) < 7:
                continue
            texts = [(await c.inner_text()).strip() for c in cells]
            payouts.append({
                "date": texts[0],
                "period": texts[1].replace("\n", " "),
                "status": texts[2],
                "method": texts[3],
                "gross": texts[4],
                "fees": texts[5],
                "net": texts[6],
            })

        await ctx.storage_state(path=SESSION_PATH)
    finally:
        await page.close()

    _log(f"Payout-Historie gelesen: {len(payouts)} Eintraege.")
    return payouts


def get_totals_by_account(links: list) -> dict:
    """Sums click/sub totals across every link placement (TikTok Bio,
    Instagram Bio, Facebook Bio, ...) that mentions the same @handle in its
    label — one account can have several bio-link placements at once."""
    totals = {}
    for link in links:
        match = re.search(r'\(@?([\w.]+)\)', link["label"])
        if not match:
            continue
        handle = match.group(1).lower()
        entry = totals.setdefault(handle, {"clicks": 0, "subs": 0})
        entry["clicks"] += link["total_clicks"]
        entry["subs"] += link["total_subs"]
    return totals
