"""
Jarvis V2 — Instagram Monitoring
Logs in once (session persists), then periodically checks follower counts
(reliable via page text) and recent post performance (via screenshot +
Claude Vision, since Instagram's view-count overlays sit in obfuscated,
frequently-changing CSS — a screenshot is far more robust than chasing
class names). Runs fully headless — unlike Fanplace, Instagram raises no
Cloudflare-style wall against headless browsing of profile/post pages with
a valid saved session, so nothing ever renders a visible window at all.
"""

import asyncio
import base64
import json
import os
import re
import time

from playwright.async_api import async_playwright

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
SESSION_PATH = os.path.join(os.path.dirname(__file__), "instagram_session.json")
SNAPSHOTS_PATH = os.path.join(os.path.dirname(__file__), "memory", "instagram_snapshots.jsonl")
TRACKED_LINKS_PATH = os.path.join(os.path.dirname(__file__), "memory", "tracked_links.jsonl")
LINK_SNAPSHOTS_PATH = os.path.join(os.path.dirname(__file__), "memory", "link_snapshots.jsonl")
VIDEO_ANALYSIS_PATH = os.path.join(os.path.dirname(__file__), "memory", "video_analysis.jsonl")
POST_SHOTS_DIR = os.path.join(os.path.dirname(__file__), "memory", "post_screenshots")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-instagram.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-instagram.log")
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

os.makedirs(os.path.dirname(SNAPSHOTS_PATH), exist_ok=True)

_playwright = None
_browser = None
_context = None


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def add_competitor_account(handle: str) -> bool:
    """Add a newly discovered account to config.json's competitor list.
    Returns False if it's already tracked (Luna Vale or competitor)."""
    config = _load_config()
    tracked = set(config.get("luna_vale_accounts", [])) | set(config.get("competitor_accounts", []))
    if handle in tracked:
        return False
    config.setdefault("competitor_accounts", []).append(handle)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _log(f"Neuer Account automatisch hinzugefuegt: @{handle}")
    return True


def remove_competitor_account(handle: str) -> bool:
    """Ahmad (2026-08-07): auto-discovered accounts that turn out NOT to be
    a true 1:1 niche/content match should stop being actively tracked, not
    just sit unrated in the sheet forever. Returns False if it wasn't
    tracked as a competitor to begin with."""
    config = _load_config()
    competitors = config.get("competitor_accounts", [])
    if handle not in competitors:
        return False
    competitors.remove(handle)
    config["competitor_accounts"] = competitors
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _log(f"Account @{handle} aus aktiver Beobachtung entfernt (kein 1:1-Fit).")
    return True


COMPETITOR_NICHES_PATH = os.path.join(os.path.dirname(__file__), "memory", "competitor_niches.json")


def record_competitor_niche(handle: str, niche: str):
    """Remembers which niche a discovered competitor belongs to (2026-08-06
    fix) — without this, sheets_tools hardcoded EVERY new competitor as
    'Goth/alternative' in the Target Creator List regardless of which niche
    search actually found them, silently mislabeling Cowgirl/Cosplay finds."""
    niches = get_competitor_niches()
    niches[handle] = niche
    os.makedirs(os.path.dirname(COMPETITOR_NICHES_PATH), exist_ok=True)
    with open(COMPETITOR_NICHES_PATH, "w", encoding="utf-8") as f:
        json.dump(niches, f, ensure_ascii=False, indent=2)


def get_competitor_niches() -> dict:
    if not os.path.exists(COMPETITOR_NICHES_PATH):
        return {}
    try:
        with open(COMPETITOR_NICHES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


async def _get_context():
    """Headless — tested working against Instagram with a valid saved session
    (login/2FA already happened once, non-headless, to get that session file).
    Unlike Fanplace (blocked by Cloudflare in headless mode), Instagram raises
    no such wall for reading public/logged-in profile pages, so there's no
    off-screen-window juggling here: nothing ever renders on screen at all."""
    global _playwright, _browser, _context
    if _context is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
        storage = SESSION_PATH if os.path.exists(SESSION_PATH) else None
        _context = await _browser.new_context(storage_state=storage, user_agent=UA, locale="de-DE")
    return _context


def reset_browser():
    """Roadmap Punkt 21 — nach einem Watchdog-Timeout (background_brain.py,
    _run_pass_safely) die Modul-Singletons verwerfen statt eine womoeglich
    kaputte Instanz weiterzuverwenden. Setzt SOFORT synchron auf None, damit
    der naechste _get_context()-Aufruf sicher neu startet, und schliesst die
    alte Instanz nur best effort im Hintergrund (ein await hier koennte
    selbst haengen, wenn die Session wirklich tot ist)."""
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


async def _screenshot(page, timeout: float = 15000, attempts: int = 2) -> bytes:
    """Screenshot a post page, tolerating the occasional stalled capture.

    Reel pages autoplay a looping video, and headless Chrome occasionally
    misses a fresh frame for the capture. `animations="disabled"` freezes
    CSS animations/transitions so there's less in flight, the shorter
    timeout means a stall costs 15s instead of 30s, and one retry keeps a
    transient stall from losing the datapoint entirely."""
    last_error = None
    for attempt in range(attempts):
        try:
            return await page.screenshot(type="png", timeout=timeout, animations="disabled")
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                await page.wait_for_timeout(1500)
    raise last_error


async def _goto_tolerant(page, url: str, timeout: float = 20000, load_timeout: float = 10000):
    """Navigate without letting a slow-loading media asset cost us the whole
    datapoint (2026-08-08 fix).

    Playwright's default `wait_until="load"` waits for EVERY resource, and on
    an image/video-heavy Instagram post that sporadically blows the 20s budget
    long after the stats overlay we screenshot has already rendered — the
    exact failure check_profile documents and already worked around. Seen
    live: gothjadee/p/Dbxa9rhAZG4 and thatgothdoll/reel/DbvFHCdxqwi both died
    with 'Timeout 20000ms exceeded ... waiting until "load"', while the very
    next post on the same account loaded fine 9 seconds later — so it's one
    stalled asset, not a network outage.

    Returns once the DOM is parsed, then still gives `load` its usual chance:
    on a healthy page that's identical to the old behaviour, and on a stalled
    one we screenshot what HAS rendered instead of logging an error."""
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("load", timeout=load_timeout)
    except Exception:
        pass  # a single asset is still streaming — the stats are long since painted


async def _capture_video_frames(page, timestamps=(0, 1.5, 3, 5, 8, 12)) -> list:
    """Multiple screenshots spread across the video's timeline instead of
    ONE static frame — Ahmad's ask (2026-08-06): a single frame can't show
    hook timing, WHEN a transition happens, or pacing, since those are all
    about what changes over time, not a single instant. Reels autoplay on
    page load, so waiting N seconds between captures roughly corresponds to
    that many seconds into playback. Returns [(t, png_bytes), ...] — a
    missed frame is skipped, not fatal to the whole sequence."""
    frames = []
    last_t = 0
    for t in timestamps:
        wait_ms = int((t - last_t) * 1000)
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        try:
            frame = await _screenshot(page, timeout=8000, attempts=1)
            frames.append((t, frame))
        except Exception:
            pass
        last_t = t
    return frames


async def analyze_video_deep(url: str, anthropic_client, timestamps=(0, 1.5, 3, 5, 8, 12)) -> dict:
    """Multi-frame STRUCTURAL analysis of ONE video (hook timing, transition
    point, pacing) — much more expensive than the single-screenshot path
    (analyze_recent_videos/search_hashtag_top_videos), used deliberately for
    a small, weekly set of already-confirmed winners, not routine checks."""
    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        await _goto_tolerant(page, url)
        await page.wait_for_timeout(2000)  # let the initial load/autoplay settle
        frames = await _capture_video_frames(page, timestamps)
    except Exception as e:
        _log(f"analyze_video_deep({url}): Fehler beim Aufnehmen: {e}")
        return {"url": url, "error": str(e)}
    finally:
        await page.close()

    if not frames:
        return {"url": url, "error": "keine Frames aufgenommen"}

    content = []
    for t, frame_bytes in frames:
        b64 = base64.b64encode(frame_bytes).decode("utf-8")
        content.append({"type": "text", "text": f"Frame bei ca. {t}s:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    content.append({"type": "text", "text": (
        "Das sind mehrere Frames DESSELBEN Instagram-Reels, zeitlich nacheinander (Sekunden wie "
        "jeweils angegeben). Analysiere die STRUKTUR ueber die Zeit hinweg, nicht nur ein "
        "einzelnes Bild:\n"
        "1. hook_timing: was passiert in den ersten 1-2 Sekunden, wie stark ist der Hook\n"
        "2. transition: gibt es einen Outfit-/Szenen-Wechsel — wenn ja bei ca. welcher Sekunde und "
        "wie abrupt/fliessend\n"
        "3. pacing: wirkt das Video schnell geschnitten/energiegeladen oder eher ruhig/statisch\n"
        "4. structure_summary: 1-2 Saetze was strukturell (nicht thematisch) den Erfolg ausmacht\n"
        "Antworte in GENAU diesem Format, eine Zeile pro Punkt, OHNE Anfuehrungszeichen um die "
        "Werte, OHNE JSON, OHNE Markdown:\n"
        "HOOK_TIMING: <text>\n"
        "TRANSITION: <text>\n"
        "PACING: <text>\n"
        "STRUCTURE_SUMMARY: <text>\n"
        "Jeder Wert muss auf EINER Zeile stehen (keine Zeilenumbrueche innerhalb eines Werts)."
    )})

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        analysis = _parse_labeled_lines(raw)
        if not analysis:
            raise ValueError(f"kein erwartetes Feld gefunden in Antwort: {raw[:200]!r}")
    except Exception as e:
        _log(f"analyze_video_deep({url}): Fehler bei Vision-Analyse: {e}")
        return {"url": url, "error": str(e)}

    analysis["url"] = url
    return analysis


_DEEP_ANALYSIS_FIELDS = {
    "HOOK_TIMING": "hook_timing",
    "TRANSITION": "transition",
    "PACING": "pacing",
    "STRUCTURE_SUMMARY": "structure_summary",
}


def _parse_labeled_lines(raw: str) -> dict:
    """Parses 'LABEL: value' lines into a dict. Deliberately NOT JSON — the
    values are free-text German prose that can contain quotes (e.g. quoted
    hook text like "That's not gonna fit"), which broke strict json.loads()
    on the first live test (2026-08-06). This format has no escaping problem
    since each value is just the rest of its line."""
    result = {}
    for line in raw.splitlines():
        line = line.strip().strip("`*").strip()
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = _DEEP_ANALYSIS_FIELDS.get(label.strip().upper())
        if key:
            result[key] = value.strip().strip('"').strip()
    return result


def _parse_follower_count(text: str):
    """Follower counts render like '14.800 Follower' (German) or '14.8K followers'."""
    match = re.search(r'([\d.,]+[KMk]?)\s*Follower', text)
    return match.group(1) if match else None


def _parse_post_count(text: str):
    match = re.search(r'([\d.,]+)\s*Beitr', text)
    return match.group(1) if match else None


def normalize_video_url(url: str) -> str:
    """The SAME Instagram post can appear as several different-shaped URLs:
    with or without the '/username' prefix (/user/reel/ID/ vs /reel/ID/),
    with or without a tracking query string (?igsh=...). Comparing raw URLs
    (substring or equality) treats these as DIFFERENT videos — confirmed as
    a real production bug (2026-08-07): a video already tracked via
    '/reel/ID/?igsh=...' got a second, duplicate Winner Tracking row created
    for it via '/lunaxvale/reel/ID/' from a fresh WhatsApp-insights read —
    the duplicate stayed at views=0/no insights while the original kept its
    real (but increasingly stale) numbers, and nothing ever pointed back at
    the right row. Extract just the stable post ID so any two URLs for the
    same post always compare equal, regardless of shape."""
    if not url:
        return ""
    match = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else url.strip().rstrip("/").lower()


def canonical_post_url(url: str) -> str:
    """A Vision read of Ahmad's WhatsApp self-chat sometimes reports the
    bare domain from WhatsApp's link-PREVIEW card ("instagram.com")
    instead of the actual URL in the message body — confirmed
    2026-08-07: it reached Page.goto() and blew up as "Cannot navigate to
    invalid URL", and what Ahmad saw was the unrelated-sounding "Account
    nicht sicher bestimmbar".

    Returns the URL with a scheme prepended if it was missing (Playwright
    needs an absolute URL — 'www.instagram.com/...' fails the same way),
    or '' if this isn't a post/reel link at all and therefore cannot
    identify a video. The rest of the URL is left untouched, so whatever
    Ahmad actually sent is what lands in the sheet."""
    url = (url or "").strip().strip('<>"\'')
    if not url:
        return ""
    if not re.match(r'^https?://', url, re.I):
        url = "https://" + url.lstrip("/")
    if not re.match(r'^https?://(?:[\w-]+\.)*instagram\.com/', url, re.I):
        return ""
    return url if re.search(r'/(?:p|reel|reels)/[A-Za-z0-9_-]+', url) else ""


async def get_post_display_name(url: str) -> str:
    """Instagram post pages don't expose the poster's @handle as a plain
    link (checked — profile links on the page are just nav chrome), but
    the og:title meta tag reliably has 'DisplayName on Instagram: \"caption\"'.
    Returns just the display name, or '' if not found."""
    if not re.match(r'^https?://', (url or "").strip(), re.I):
        # Cheaper and far clearer than letting Playwright open a page and
        # fail 20s later with a protocol error — see canonical_post_url.
        _log(f"get_post_display_name({url!r}): uebersprungen, keine absolute URL")
        return ""
    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        await _goto_tolerant(page, url)
        await page.wait_for_timeout(2500)
        og_title = await page.evaluate(
            """() => { const el = document.querySelector('meta[property="og:title"]'); return el ? el.content : null; }"""
        )
        if not og_title:
            return ""
        # German locale renders this as "auf Instagram", English as "on
        # Instagram" — the context is set to de-DE, so "auf" is actually
        # the common case here, but match both to not be locale-fragile.
        match = re.match(r'^(.+?)\s+(?:auf|on)\s+Instagram', og_title)
        return match.group(1).strip() if match else ""
    except Exception as e:
        _log(f"get_post_display_name({url}): ERROR {e}")
        return ""
    finally:
        await page.close()


async def identify_post_account(url: str, known_handles: list, anthropic_client) -> str:
    """Match a post's display name against the already-tracked accounts —
    via Claude, not a hardcoded/fuzzy string match, since display names and
    handles can differ non-trivially (e.g. 'Luna Vale 🖤' vs 'lunaxvale').
    Returns '' rather than a guess if it can't confidently match — wrong
    data going into Winner Tracking is worse than asking again."""
    display_name = await get_post_display_name(url)
    if not display_name:
        return ""

    response = await anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": (
                f"Instagram-Anzeigename: \"{display_name}\"\n"
                f"Bekannte Handles: {', '.join(known_handles)}\n\n"
                "Welches Handle gehoert am wahrscheinlichsten zu diesem Anzeigenamen? Antworte NUR "
                "mit dem exakten Handle aus der Liste, oder exakt 'UNBEKANNT' wenn du dir nicht "
                "sicher bist."
            ),
        }],
    )
    answer = response.content[0].text.strip()
    return answer if answer in known_handles else ""


async def check_profile(handle: str, attempts: int = 2) -> dict:
    """Visit one profile, return follower/post counts. Reuses the logged-in session.

    Tolerates a transient bad load, the same way `_screenshot` does. An error
    here isn't free: background_brain turns it into a pending question to
    Ahmad ("stimmt der Handle noch?"), so a dropped WLAN packet would
    otherwise surface as a false alarm about a perfectly healthy account.
    """
    ctx = await _get_context()
    last_error = None
    for attempt in range(attempts):
        page = await ctx.new_page()
        try:
            # "domcontentloaded" rather than the default "load": a profile page
            # pulls in dozens of images and video previews, and waiting for all
            # of them blew the 20s budget on loads that had long since rendered
            # the numbers we actually want. The counts are client-rendered, so
            # we wait for the follower text itself instead of for the network.
            await page.goto(
                f"https://www.instagram.com/{handle}/", timeout=20000, wait_until="domcontentloaded"
            )
            try:
                await page.wait_for_function(
                    "() => /Follower|followers/.test(document.body.innerText)", timeout=15000
                )
            except Exception:
                pass  # no follower text (deleted/private/banned) — parse what's there anyway
            await page.wait_for_timeout(1500)
            text = await page.evaluate("document.body.innerText")
            followers = _parse_follower_count(text)
            posts = _parse_post_count(text)
            result = {
                "handle": handle,
                "followers": followers,
                "posts": posts,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _log(f"{handle}: followers={followers} posts={posts}")
            return result
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                _log(f"{handle}: Versuch {attempt + 1} fehlgeschlagen ({e.__class__.__name__}), neuer Versuch...")
                await asyncio.sleep(5)
        finally:
            await page.close()

    _log(f"{handle}: ERROR {last_error}")
    return {"handle": handle, "error": str(last_error), "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}


async def check_all_tracked_accounts() -> list:
    """Check Luna Vale's own accounts + competitor accounts, save a snapshot."""
    config = _load_config()
    handles = config.get("luna_vale_accounts", []) + config.get("competitor_accounts", [])
    results = []
    for handle in handles:
        result = await check_profile(handle)
        results.append(result)
        await asyncio.sleep(3)  # don't hammer Instagram back-to-back

    with open(SNAPSHOTS_PATH, "a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return results


def get_previous_snapshot(handle: str):
    """Most recent PRIOR snapshot for a handle (for trend comparison), or None."""
    if not os.path.exists(SNAPSHOTS_PATH):
        return None
    matches = []
    with open(SNAPSHOTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("handle") == handle and "followers" in entry:
                matches.append(entry)
    return matches[-2] if len(matches) >= 2 else None


def format_trend_summary(max_chars: int = 4000) -> str:
    """Latest snapshot per tracked handle, with a trend arrow vs the prior
    check — CLEARLY SEPARATED into own vs. competitor accounts (2026-08-06
    fix). Used to be one flat, unlabeled list of all 13 tracked handles —
    caught live: Ahmad asked for an update on "his" accounts and got
    competitor follower counts back, because nothing in the text told the
    LLM which handles are actually his (and the competitors' much bigger
    numbers — up to 217k vs. his 15k/528/4.6k — made them read as the more
    'notable' accounts if anything, not less)."""
    if not os.path.exists(SNAPSHOTS_PATH):
        return "Noch keine Instagram-Daten gesammelt."

    config = _load_config()
    own_handles = set(config.get("luna_vale_accounts", []))

    latest_by_handle = {}
    with open(SNAPSHOTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest_by_handle[entry["handle"]] = entry

    def _format_line(handle, entry):
        if "error" in entry:
            return f"@{handle}: Fehler beim letzten Check ({entry['error']})"
        prev = get_previous_snapshot(handle)
        trend = ""
        if prev and prev.get("followers") and entry.get("followers"):
            trend = f" (vorher: {prev['followers']})"
        return f"@{handle}: {entry.get('followers', '?')} Follower{trend}, {entry.get('posts', '?')} Beitraege, Stand {entry.get('timestamp', '')}"

    own_lines = [_format_line(h, e) for h, e in latest_by_handle.items() if h in own_handles]
    competitor_lines = [_format_line(h, e) for h, e in latest_by_handle.items() if h not in own_handles]

    sections = []
    if own_lines:
        sections.append("=== EIGENE ACCOUNTS (Luna Vale, Ahmad) ===\n" + "\n".join(own_lines))
    if competitor_lines:
        sections.append("=== KONKURRENZ (NICHT Ahmads eigene Accounts) ===\n" + "\n".join(competitor_lines))

    text = "\n\n".join(sections) if sections else "Noch keine Instagram-Daten gesammelt."
    return text[-max_chars:] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Video-level analysis — views/likes/comments AND a rough read on whether the
# audience skews premium (English-speaking: USA/Canada/UK/Australia) or not,
# via a screenshot + Claude Vision per post since Instagram's stat overlays
# and comment threads sit in obfuscated, frequently-changing CSS.
# ---------------------------------------------------------------------------

async def _get_recent_post_links(page, limit: int) -> list:
    hrefs = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
            .map(a => a.href)"""
    )
    seen = []
    for h in hrefs:
        if h not in seen:
            seen.append(h)
        if len(seen) >= limit:
            break
    return seen


_NON_PROFILE_PATH_PREFIXES = (
    "p", "reel", "reels", "explore", "accounts", "direct", "stories", "tv",
    "tags", "legal", "about", "developer", "api", "graphql", "web", "popular",
    "notifications", "activity", "settings", "privacy", "terms", "help",
    "challenge", "consent", "session", "login", "logout", "hashtag", "",
)


async def _get_similar_account_candidates(page, exclude_handle: str) -> list:
    """Every OTHER profile link Instagram happens to surface on this page
    (suggested/similar-account chips, tagged collaborators) — a signal
    Instagram itself computed, reused from a page visit we're already making
    for video analysis so it costs zero extra Instagram traffic.

    IMPORTANT: because we're logged into Ahmad's PERSONAL viewing account,
    Instagram's own nav chrome (top-right profile icon etc.) links back to
    that account on every page. It must never be treated as a "discovered"
    competitor — that would violate the hard personal/business account
    separation Ahmad set up. Excluded explicitly, not just by lucky filtering."""
    try:
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1500)
        hrefs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href^="/"]'))
                .map(a => a.getAttribute('href'))"""
        )
    except Exception:
        return []

    own_handle = _load_config().get("instagram_username", "").lower()
    candidates = []
    seen = {exclude_handle.lower(), own_handle}
    for href in hrefs or []:
        if not href:
            continue
        first_segment = href.strip("/").split("/")[0].lower()
        if not first_segment or first_segment in _NON_PROFILE_PATH_PREFIXES:
            continue
        if "/" in href.strip("/") or first_segment in seen:
            continue
        seen.add(first_segment)
        candidates.append(first_segment)
    return candidates


async def analyze_recent_videos(handle: str, anthropic_client, count: int = 6) -> dict:
    """Visit a profile, open its `count` most recent posts/reels, and read
    views/likes/comment-audience-quality off each via screenshot + Vision.
    Also harvests any similar-account suggestions Instagram shows on that
    same profile page, for account discovery at no extra Instagram traffic."""
    ctx = await _get_context()
    page = await ctx.new_page()
    links = []
    similar_accounts = []
    try:
        await _goto_tolerant(page, f"https://www.instagram.com/{handle}/")
        await page.wait_for_timeout(3000)
        links = await _get_recent_post_links(page, count)
        similar_accounts = await _get_similar_account_candidates(page, handle)
    except Exception as e:
        _log(f"{handle}: Fehler beim Sammeln der Video-Links: {e}")
    finally:
        await page.close()

    results = []
    for url in links:
        post_page = await ctx.new_page()
        try:
            await _goto_tolerant(post_page, url)
            await post_page.wait_for_timeout(3500)
            png_bytes = await _screenshot(post_page)
            b64 = base64.b64encode(png_bytes).decode("utf-8")

            response = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text": (
                            "Das ist ein Screenshot eines Instagram Posts/Reels. Lies ab: Views, Likes, "
                            "Anzahl Kommentare (falls sichtbar). Schau dir dann die sichtbaren Kommentar-"
                            "Texte an und schaetze grob ein, ob die Zielgruppe primaer englischsprachig "
                            "ist (USA/Kanada/UK/Australien = Premium-Markt) oder primaer nicht-englisch-"
                            "sprachig (z.B. ueberwiegend spanische Kommentare = niedrigere Prioritaet). "
                            "Antworte NUR in diesem Format, eine Zeile: "
                            "'views=X likes=Y comments=Z audience=premium/niedrig/unklar'."
                        )},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            entry = {"handle": handle, "url": url, "raw": raw, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
            _log(f"VIDEO {handle} {url}: {raw}")
        except Exception as e:
            entry = {"handle": handle, "url": url, "error": str(e), "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
            _log(f"VIDEO {handle} {url}: ERROR {e}")
        finally:
            await post_page.close()
        results.append(entry)
        await asyncio.sleep(2)

    if results:
        with open(VIDEO_ANALYSIS_PATH, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return {"videos": results, "similar_accounts": similar_accounts}


async def search_hashtag_top_videos(keyword: str, anthropic_client, count: int = 3) -> list:
    """Instagram's hashtag page surfaces a 'Top posts' grid first — already
    ranked by engagement, exactly the fresh-trend signal Ahmad wants ('die
    besten Suchbegriffe eingeben, danach nach guten Videos ausschau halten').
    Reuses the same per-post screenshot+Vision read as analyze_recent_videos,
    plus a content-quality read (hook/transformation/energy/US-fit) matching
    Ahmad's Content Research Agent brief (2026-08-06) so a caller building a
    Jerome brief can judge recreatability, not just raw numbers."""
    tag = re.sub(r'[^a-z0-9]', '', keyword.lower())
    if not tag:
        return []
    ctx = await _get_context()
    page = await ctx.new_page()
    links = []
    try:
        await _goto_tolerant(page, f"https://www.instagram.com/explore/tags/{tag}/")
        await page.wait_for_timeout(3000)
        links = await _get_recent_post_links(page, count)
    except Exception as e:
        _log(f"Hashtag-Suche #{tag}: Fehler beim Sammeln der Links: {e}")
    finally:
        await page.close()

    results = []
    for url in links:
        post_page = await ctx.new_page()
        try:
            await _goto_tolerant(post_page, url)
            await post_page.wait_for_timeout(3500)
            png_bytes = await _screenshot(post_page)
            b64 = base64.b64encode(png_bytes).decode("utf-8")

            response = await anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text": (
                            "Das ist ein Screenshot eines Instagram Posts/Reels aus einer Hashtag-Top-"
                            "Posts-Suche. Lies ab: Views, Likes, Anzahl Kommentare (falls sichtbar). "
                            "Beurteile dann als Content-Recherche fuer Nachbau-faehige Kurzvideos: "
                            "(1) format = worum es inhaltlich geht (Hook/Thema/Struktur), "
                            "(2) hook = wie stark ist der visuelle/textliche Hook in den ersten 1-2 "
                            "Sekunden (falls erkennbar), "
                            "(3) elements = was macht es konkret nachbaubar (z.B. Outfit-Wechsel, "
                            "Transformation/Kontrast, hohe Energie/Bewegung, Comedy-Punchline, "
                            "Street-Interview-Stil), "
                            "(4) us_signals = sichtbare US/englischsprachige Hinweise (Flaggen, "
                            "College-Kleidung, US-Orte, englischer Text) oder 'keine erkennbar', "
                            "(5) quality = kurze Qualitaetseinschaetzung (sauberes vertikales Video "
                            "gute Beleuchtung, oder 'niedrige Qualitaet'/'schlechte Beleuchtung' falls "
                            "zutreffend). Antworte NUR in diesem Format, eine Zeile: 'views=X likes=Y "
                            "comments=Z format=<...> hook=<...> elements=<...> us_signals=<...> "
                            "quality=<...>'."
                        )},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            results.append({"tag": tag, "url": url, "raw": raw, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")})
            _log(f"HASHTAG #{tag} {url}: {raw}")
        except Exception as e:
            _log(f"HASHTAG #{tag} {url}: ERROR {e}")
        finally:
            await post_page.close()
        await asyncio.sleep(2)

    return results


def format_video_analysis(handle: str = None, max_chars: int = 4000) -> str:
    """Most recent video-analysis readings, optionally filtered to one handle."""
    if not os.path.exists(VIDEO_ANALYSIS_PATH):
        return "Noch keine Video-Analyse durchgefuehrt."
    entries = []
    with open(VIDEO_ANALYSIS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if handle is None or e.get("handle") == handle:
                entries.append(e)
    recent = entries[-15:]
    lines = []
    for e in recent:
        if "error" in e:
            lines.append(f"@{e['handle']}: Fehler ({e['error']})")
        else:
            lines.append(f"@{e['handle']}: {e['raw']} (Stand {e.get('timestamp', '')})")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Link tracking — Ahmad sends a specific post/reel URL, Jarvis watches its
# performance (views/likes/comments) over time. Uses a screenshot + Claude
# Vision since per-post stats sit in obfuscated CSS, same reasoning as the
# view-count problem noted in the module docstring.
# ---------------------------------------------------------------------------

def add_tracked_link(url: str, label: str = "") -> str:
    """Start tracking an Instagram post/reel link over time."""
    if any(l["url"] == url for l in get_tracked_links()):
        return "Dieser Link wird bereits verfolgt."
    entry = {"url": url, "label": label, "added": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(TRACKED_LINKS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return "Link wird jetzt verfolgt."


def get_tracked_links() -> list:
    if not os.path.exists(TRACKED_LINKS_PATH):
        return []
    entries = []
    with open(TRACKED_LINKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


async def check_post(url: str, anthropic_client) -> dict:
    """Screenshot one post/reel (headless) and read its stats via Claude
    Vision. Appends the reading to the link snapshot log."""
    ctx = await _get_context()
    page = await ctx.new_page()
    try:
        await _goto_tolerant(page, url)
        await page.wait_for_timeout(4000)
        png_bytes = await _screenshot(page)
        b64 = base64.b64encode(png_bytes).decode("utf-8")

        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": (
                        "Das ist ein Screenshot eines Instagram Posts/Reels. Lies die sichtbaren "
                        "Statistiken ab: Aufrufe/Views, Likes, Kommentare. Antworte NUR im Format "
                        "'views=X likes=Y comments=Z' mit den Zahlen, die du siehst (schreibe "
                        "'unbekannt' fuer Werte, die nicht sichtbar sind). Keine weiteren Erklaerungen."
                    )},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        result = {"url": url, "raw": raw, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        _log(f"LINK {url}: {raw}")
    except Exception as e:
        result = {"url": url, "error": str(e), "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
        _log(f"LINK {url}: ERROR {e}")
    finally:
        await page.close()

    with open(LINK_SNAPSHOTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


async def get_recent_post_links(handle: str, limit: int = 4) -> dict:
    """Nur die Links der neuesten Beitraege/Reels eines Profils — ohne die
    Vision-Analyse pro Post, die analyze_recent_videos zusaetzlich macht.

    Rueckgabe {"links": [...], "error": None oder Text}. Der Fehler wird
    bewusst ZURUECKGEGEBEN und nicht nur geloggt: bei einer Frage wie "wurde
    da inzwischen was gepostet?" waere "Profil nicht erreichbar" als "es
    wurde nichts gepostet" gelesen die schlimmste moegliche Antwort."""
    page = None
    try:
        # _get_context() bewusst INNERHALB des try: schon der Browser-Start
        # kann scheitern (fehlender/kaputter Chromium, tote Session), und auch
        # das muss als ehrliches "error" zurueckkommen statt den Aufrufer mit
        # einer Exception zu treffen.
        ctx = await _get_context()
        page = await ctx.new_page()
        await _goto_tolerant(page, f"https://www.instagram.com/{handle}/")
        await page.wait_for_timeout(3000)
        return {"links": await _get_recent_post_links(page, limit), "error": None}
    except Exception as e:
        _log(f"{handle}: Fehler beim Sammeln der neuesten Post-Links: {e}")
        return {"links": [], "error": f"{e.__class__.__name__}: {e}"}
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def check_post_public_stats(url: str, anthropic_client, save_screenshot: bool = False) -> dict:
    """Die OEFFENTLICH sichtbaren Zahlen EINES Posts (Views/Likes/Kommentare)
    per Screenshot + Vision, optional mit dem Screenshot als PNG-Datei
    (Pfad in "screenshot_path").

    Bewusst getrennt von check_post(): das schreibt jede Messung ins
    Link-Snapshot-Log, das zu den ausdruecklich verfolgten Links gehoert
    (add_tracked_link/format_link_trends) — ein einmaliger Blick auf einen
    beliebigen Post hat darin nichts zu suchen, sonst tauchen dort Links als
    "verfolgt" auf, die nie jemand verfolgen wollte.

    WICHTIG: das ist die oeffentliche Post-Ansicht, NICHT das Insights-Panel.
    Reach, Publikums-Herkunft und Interaktionsraten zeigt Instagram nur
    innerhalb des Accounts, in dem gepostet wurde — dort ist diese Session
    nicht eingeloggt (sie gehoert dem privaten Ansehen-Account)."""
    result = {"url": url, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    page = None
    try:
        # wie in get_recent_post_links: auch ein fehlgeschlagener Browser-Start
        # wird zum ehrlichen "error"-Feld, nicht zur Exception beim Aufrufer.
        ctx = await _get_context()
        page = await ctx.new_page()
        await _goto_tolerant(page, url)
        await page.wait_for_timeout(4000)
        png_bytes = await _screenshot(page)
    except Exception as e:
        _log(f"POST {url}: ERROR {e}")
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
            os.makedirs(POST_SHOTS_DIR, exist_ok=True)
            path = os.path.join(
                POST_SHOTS_DIR,
                f"{normalize_video_url(url)}_{time.strftime('%Y%m%d-%H%M%S')}.png",
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
                        "Das ist ein Screenshot eines Instagram Posts/Reels. Lies die sichtbaren "
                        "Statistiken ab: Aufrufe/Views, Likes, Kommentare. Antworte NUR im Format "
                        "'views=X likes=Y comments=Z' mit den Zahlen, die du siehst (schreibe "
                        "'unbekannt' fuer Werte, die nicht sichtbar sind). Keine weiteren Erklaerungen."
                    )},
                ],
            }],
        )
        result["raw"] = response.content[0].text.strip()
        _log(f"POST {url}: {result['raw']}")
    except Exception as e:
        _log(f"POST {url}: Vision ERROR {e}")
        result["error"] = f"Zahlen nicht auswertbar ({e.__class__.__name__}: {e})"
    return result


async def check_all_tracked_links(anthropic_client) -> list:
    """Check every tracked link once, spaced out to not hammer Instagram."""
    links = get_tracked_links()
    results = []
    for link in links:
        result = await check_post(link["url"], anthropic_client)
        results.append(result)
        await asyncio.sleep(3)
    return results


def format_link_trends(max_chars: int = 4000) -> str:
    """Latest reading per tracked link, with the prior reading for comparison."""
    if not os.path.exists(LINK_SNAPSHOTS_PATH):
        return "Noch keine Links verfolgt."

    by_url = {}
    with open(LINK_SNAPSHOTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_url.setdefault(entry["url"], []).append(entry)

    lines = []
    for url, entries in by_url.items():
        latest = entries[-1]
        if "error" in latest:
            lines.append(f"{url}: Fehler beim letzten Check ({latest['error']})")
            continue
        prev_str = f" (vorher: {entries[-2]['raw']})" if len(entries) >= 2 and "raw" in entries[-2] else ""
        lines.append(f"{url}: {latest.get('raw', '?')}{prev_str}, Stand {latest.get('timestamp', '')}")

    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text
