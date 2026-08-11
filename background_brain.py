"""
Jarvis V2 — Background Brain
Runs continuously and independently of the double-clap trigger / server.py —
its own launchd agent, always on. Periodically:
  - checks Instagram (Luna Vale accounts + competitors + tracked links)
  - researches social-media algorithm/niche trends
  - compares against prior snapshots, flags significant follower swings
  - sends Ahmad an immediate WhatsApp self-alert on anything important
  - queues a pending question (memory.py) when something needs his judgment
    call, so Jarvis raises it once next time they actually talk
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

import anthropic
import httpx

import app_control
import calendar_tools
import claude_code_tool
import content_strategy
import fanplace
import gmail_tools
import goal_tracker
import finance_tracker
import instagram_tools
import jerome_comm
import knowledge_graph
import memory
import push_notifications
import research
import screen_capture
import semantic_memory
import sheets_tools
import slt_bio_tools

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CLAUDE_CODE_STATE_PATH = os.path.join(os.path.dirname(__file__), "memory", "claude_code_scan_state.json")
SKILL_GROWTH_SCAN_STATE_PATH = os.path.join(os.path.dirname(__file__), "memory", "skill_growth_scan_state.json")

# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht,
# _log() (siehe unten) hat das bisher still ueber `except OSError: pass`
# verschluckt — jede FEHLER-Zeile in diesem Modul (und in fanplace.py,
# instagram_tools.py, research.py, jerome_comm.py, content_strategy.py,
# screen_control.py, slt_bio_tools.py, gleiches Muster dort) ging seit der
# Hetzner-Migration ins Leere, inklusive der Fehler die _collect_new_error_lines
# unten fuer self_improve_pass einsammeln soll. /var/log/jarvis-brain.log und
# /var/log/jarvis-server.log existieren bereits (systemd StandardOutput/Error),
# die anderen werden hier neu angelegt.
IS_SERVER = os.environ.get("JARVIS_ROLE") == "server"
_LOG_DIR = "/var/log" if IS_SERVER else os.path.expanduser("~/Library/Logs")
JARVIS_LOGS = [
    os.path.join(_LOG_DIR, "jarvis-brain.log"),
    os.path.join(_LOG_DIR, "jarvis-instagram.log"),
    os.path.join(_LOG_DIR, "jarvis-research.log"),
]
LOG_PATH = os.path.join(_LOG_DIR, "jarvis-brain.log")

INSTAGRAM_INTERVAL_SECONDS = 4 * 60 * 60      # Ahmad, 2026-08-11: 90min (16x/Tag ueber 21
# Accounts) war zu viel -- 6x/Tag reicht fuer Follower-Trend + Fanplace-Churn-Check locker,
# senkt gleichzeitig unnoetig haeufige automatisierte Profil-Besuche (Ban-Risiko-Reduktion).
BUSINESS_CYCLE_INTERVAL_SECONDS = 5 * 60 * 60  # discovery/sheet-sync/video-analysis/trial-reel
RESEARCH_INTERVAL_SECONDS = 12 * 60 * 60      # Ahmad wants 1-2 research WhatsApp updates/day, not 4-6
SELF_IMPROVE_INTERVAL_SECONDS = 30 * 60       # Ahmad (2026-08-06): "er soll IMMER auf Fehlersuche gehen" —
                                                # own fast dedicated cadence, decoupled from the slower
                                                # business cycle. Cheap when idle: it's a no-op unless a
                                                # genuinely NEW error line showed up since the last scan.

SKILL_GROWTH_INTERVAL_SECONDS = 30 * 60        # Ahmad (2026-08-10): "ich brauche es, damit er eigenstaendiger
                                                # wird" — reaktiver Zweig (Luecken-Scan) im selben schnellen
                                                # Takt wie Self-Improve.
SKILL_GROWTH_IDEA_INTERVAL_SECONDS = 6 * 60 * 60  # eigener-Ideen-Zweig bewusst SELTENER als der reaktive —
                                                # spekulativer und riskanter (Ahmads eigene Einschaetzung).

CALENDAR_CHECK_INTERVAL_SECONDS = 3 * 60 * 60  # Terminkonflikte sind selten, aber
# zeitkritisch genug fuer mehr als den 6h-Business-Takt. Aendert NICHTS am
# Kalender, meldet nur (Tier 1 Punkt 7, 2026-08-08, Ahmads bewusste Wahl).

GMAIL_REPLY_INTERVAL_SECONDS = 30 * 60  # aehnlich haeufig wie der Jerome-Kanal,
# aber E-Mail ist typischerweise nicht dringender als das.

MEETING_REMINDER_INTERVAL_SECONDS = 5 * 60  # eigener schneller Takt, keine
# Arbeitszeit-Gate (Termine koennen jederzeit sein), Tier 3 Punkt 14, 2026-08-08.
MEETING_REMINDER_LEAD_MINUTES = 20

SCREEN_AWARENESS_INTERVAL_SECONDS = 25 * 60  # Roadmap Punkt 19, 2026-08-09 —
# Ahmads bewusste Wahl nach Rueckfrage: "alle 20-30 Minuten", niedrigfrequent
# sowohl fuers Risiko (Screenshot geht bei jeder Aufnahme an Claude Vision)
# als auch fuer die Kosten. Bewusst KEIN _alert() dabei, rein passiv/ambient
# — ein Alarm bei jedem Zyklus waere aufdringlich, nicht was Ahmad wollte.

FINANCE_SYNC_INTERVAL_SECONDS = 6 * 60 * 60  # Tier 3 Punkt 15, 2026-08-08 —
# Kurs+Payouts muessen nicht minuetlich aktuell sein, alle 6h reicht locker.

GOAL_CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # der Pass selbst tickt alle 6h — die
# eigentliche Faelligkeit steuert jedes Ziel selbst ueber sein eigenes
# check_in_days (Tier 1 Punkt 8, 2026-08-08).

MEMORY_CONSOLIDATION_INTERVAL_SECONDS = 24 * 60 * 60  # Tier 2 Punkt 11 ("Dreaming"),
# Wartungs-Charakter, kein dringender Pass — einmal taeglich reicht.

JEROME_INTERVAL_SECONDS = 15 * 60  # Ahmad (2026-08-06): "kuerzer als 90 Minuten... damit wir schneller reagieren"
# Jerome is in the Philippines, works ~4h/day starting "meistens ab 11-12 Uhr
# deutscher Zeit" — checking every 15min around the clock would burn Vision
# calls all night for nothing; only check inside his actual work window
# (with buffer on both sides for "meistens" not being exact).
JEROME_WORK_HOUR_START = 10
JEROME_WORK_HOUR_END = 17

LOOP_TICK_SECONDS = 60

# Roadmap Punkt 21 — jeder Pass ist rein async (kein blockierendes requests/
# time.sleep, geprueft), asyncio.wait_for kann darum jeden einzelnen Pass
# sauber unterbrechen statt dass ein Haenger die ganze Schleife einfriert.
# 3 Minuten ist grosszuegig ueber den langsamsten normalen Passes (Multi-
# Frame-Vision-Analyse), aber weit unter dem 60s-Tick, sodass ein Haenger
# nicht stundenlang unbemerkt bleibt.
PASS_TIMEOUT_SECONDS = 180

CREDIT_ALERT_COOLDOWN_SECONDS = 6 * 3600  # one clear heads-up per ~6h while the issue persists, not every tick

# _run_health_check zaehlt wiederkehrende Fehler ueber 24h, meldet sie aber nur,
# wenn der letzte Vorfall hoechstens so lange her ist. Ohne diese Schranke war
# die Meldung "wiederholen sich trotz Self-Improve" nach JEDEM erfolgreichen Fix
# noch volle 24h zu sehen — live vorgefunden 2026-08-11: der Morgen-Briefing-
# KeyError wurde um 07:28 korrigiert, hoerte mit dem Neustart um 08:02 auf, und
# stand trotzdem im 20:00-Tagesabschluss als "wiederholt sich trotz Self-Improve".
# Genau die Sorte Fehlalarm, die Ahmad den System-Check ignorieren lehrt. Bewusst
# grosszuegige 6h (nicht 1-2h): der Check laeuft nur einmal taeglich, ein wirklich
# noch kaputter Pass mit mehrstuendigem Takt soll weiterhin auffallen.
HEALTH_REPEAT_STILL_ACTIVE_SECONDS = 6 * 3600

TIMER_STATE_PATH = os.path.join(os.path.dirname(__file__), "memory", "pass_timers.json")

DAILY_SUMMARY_HOUR = 20  # local 24h clock — earliest hour the once-daily end-of-day summary may fire
MORNING_SUMMARY_HOUR = 7  # Roadmap Punkt 6 — Gegenstueck ohne Doppelklatschen, frueh genug fuer den Start in den Tag
CONTENT_BRIEF_HOUR = 9   # Jerome needs his tasks early in the day, not at night

FOLLOWER_ALERT_THRESHOLD = 20  # absolute follower delta worth a WhatsApp ping

FANPLACE_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "memory", "fanplace_snapshot.json")
FANPLACE_SUB_DROP_THRESHOLD = 3  # active subscribers lost since the last check — small base (dozens), so a low bar

# Discovered accounts need a plausible, real audience — not an empty shell,
# not a mega-celebrity that isn't a real "competitor" at Luna Vale's scale.
DISCOVERY_MIN_FOLLOWERS = 300
DISCOVERY_MAX_FOLLOWERS = 300_000

# Video analysis (5-6 posts x screenshot+Vision each) is expensive in Instagram
# traffic, so each research-cadence tick only covers a rotating slice of the
# tracked accounts rather than all of them at once.
VIDEO_ACCOUNTS_PER_PASS = 3
VIDEO_POSTS_PER_ACCOUNT = 6


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _load_timers() -> dict:
    """Wall-clock (time.time(), NOT time.monotonic()) timestamps for each
    interval gate, persisted to disk. Without this, every restart of this
    process (and there have been several today, for unrelated updates) reset
    the in-memory clock and forced every gate to fire immediately — which is
    exactly why Ahmad got 5 research WhatsApp pings in under 2 hours instead
    of the intended 1-2/day. Missing/corrupt file -> 0 (epoch), so a genuinely
    fresh install still runs everything once immediately, same as before."""
    if not os.path.exists(TIMER_STATE_PATH):
        return {}
    try:
        with open(TIMER_STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_timer(key: str, epoch: float):
    timers = _load_timers()
    timers[key] = epoch
    os.makedirs(os.path.dirname(TIMER_STATE_PATH), exist_ok=True)
    with open(TIMER_STATE_PATH, "w") as f:
        json.dump(timers, f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


CREDIT_ERROR_PATTERNS = ("credit balance", "insufficient_quota", "billing", "quota exceeded")
_credit_issue_seen_at = None  # set by _exc() below whenever a matching error is formatted


def _exc(e: BaseException) -> str:
    """Several exception types (TimeoutError above all) carry NO message, so a
    bare f"...: {e}" logs a blank line that says nothing about what failed —
    which cost a whole debugging round once already, and is worse here than
    elsewhere because self_improve_pass reads this very log to decide what to
    hand to Claude Code. Always keep at least the class name.

    Also doubles as the funnel point for credit/billing-error detection
    (2026-08-06, Ahmad's ask: "damit wir merken bevor es wehtut") — EVERY
    error in this file already gets formatted through here (16+ call sites),
    so this is the one place that can flag it without touching all of them.
    Actually alerting Ahmad happens once per main-loop tick, see main()."""
    global _credit_issue_seen_at
    text = str(e)
    formatted = f"{type(e).__name__}: {text}" if text else type(e).__name__
    if any(p in formatted.lower() for p in CREDIT_ERROR_PATTERNS):
        _credit_issue_seen_at = time.time()
    return formatted


def _parse_count(raw):
    """'14.800' (German thousands-sep) / '4.8K' (abbreviated, dot = decimal)
    -> int, best-effort. None if unparseable. These two formats use '.' for
    opposite purposes, so the suffix has to be checked BEFORE stripping dots."""
    if not raw:
        return None
    raw = raw.strip()
    suffix = raw[-1].upper() if raw[-1].upper() in ("K", "M") else None
    try:
        if suffix:
            value = float(raw[:-1].replace(",", "."))
            return int(value * (1000 if suffix == "K" else 1_000_000))
        return int(raw.replace(".", "").replace(",", ""))
    except ValueError:
        return None


def _parse_de_pct(raw):
    """Winner Tracking's US-Audience column is a de_DE-locale fraction like
    '0,358' (=35.8%) — best-effort float, None if unparseable/absent."""
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def _parse_inbox_percent(raw):
    """Insights-Eingang's 'US Audience %' column is a human-typed number
    ('44.5', '44,5', or '44,5%') — never a screenshot, so no guessing
    involved, just locale/format tolerance. None if unparseable/absent."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip().rstrip("%").replace(",", ".")
    try:
        val = float(text)
    except ValueError:
        return None
    return val / 100 if val > 1 else val


async def _alert(config: dict, text: str, speak_live: bool = True):
    """Immediate WhatsApp self-alert. Standardmaessig wird der Text zusaetzlich
    als 'live event' gequeued (memory.add_live_event), sodass server.py ihn
    SOFORT in ein GERADE ERST verbundenes Gespraech hineinspricht, auch ohne
    dass Ahmad irgendwas gesagt hat — siehe memory.py's live_events section.

    speak_live=False (Ahmad, 2026-08-11, echter Vorfall): fuer Hinweise ohne
    echte Dringlichkeit (z.B. routine Gmail-Triage) wirkte dieses sofortige,
    kontextlose Vorlesen "sinnlos" ("er liest mir das einfach so vor, wenn
    ich ihn oeffne"), obwohl WhatsApp/Push schon zuverlaessig zugestellt
    haben. Push-Benachrichtigung und WhatsApp bleiben in JEDEM Fall bestehen,
    nur der sofortige Sprach-Interrupt beim naechsten Verbinden entfaellt.

    Best-effort only: darf nie werfen und nie den WhatsApp-Versand blockieren/
    ersetzen, der bleibt der eine Kanal der zuverlaessig ankommt, egal ob
    gerade ein Gespraech laeuft."""
    if speak_live:
        try:
            memory.add_live_event(text)
        except Exception as e:
            _log(f"live_event Warteschlange fehlgeschlagen (ignoriert): {e}")

        # Roadmap Punkt 3 — Event-Bus statt blindem Warten auf den 5-Sekunden-
        # Datei-Poll von server.py (_live_events_poll_loop dort): direkter Aufruf,
        # senkt die Zustellzeit im Normalfall auf nahezu sofort. Rein best effort
        # und mit kurzem Timeout — der Datei-Weg oben bleibt der Fallback, falls
        # server.py gerade nicht erreichbar ist (z.B. eigener Neustart).
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                await client.post("http://127.0.0.1:8340/internal/push_event", json={"text": text})
        except Exception:
            pass

    try:
        push_notifications.send_push_to_all("Jarvis", text)
    except Exception as e:
        _log(f"Push-Benachrichtigung fehlgeschlagen (ignoriert): {e}")

    phone = config.get("alert_phone")
    if not phone:
        _log(f"ALARM (kein alert_phone konfiguriert): {text}")
        return
    result = await app_control.send_whatsapp(phone, text)
    _log(f"ALARM gesendet: {text} -> {result}")


def _has_open_question_about(handle: str) -> bool:
    # Checks the last 20h (open OR already-'asked'), not just currently-open
    # questions — same reasoning as trial_wave_nudge_pass's dedup: 'asked'
    # flips the instant a question enters an activate prompt, not once
    # Ahmad actually heard it, so this check runs every ~90min and could
    # otherwise re-queue the same broken-account question within the hour.
    return memory.has_recent_question_about(f"@{handle}", hours=20)


async def _classify_question_urgency(client, question: str) -> str:
    """Wie dringend ist eine offene Frage an Ahmad — generalisiert vom
    gleichen Klassifikations-Muster wie jerome_comm.handle_jerome_message's
    routine/notable/needs_ahmad (gleiches Modell, gleiches JSON-Format,
    gleiche Brace-Slicing-Parse-Logik), nur auf low/medium/high statt
    business-spezifischer Kategorien. Fallback 'medium' bei jedem Fehler —
    weder faelschlich als dringend noch faelschlich als vernachlaessigbar
    einstufen, wenn die Klassifikation selbst scheitert."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": (
                "Eine Frage, die Jarvis (KI-Assistent) seinem Nutzer Ahmad als naechstes "
                "stellen will:\n\n"
                f'"{question}"\n\n'
                "Wie dringend/wichtig ist es, dass Ahmad das JETZT hoert (statt es einfach "
                "in der Liste offener Punkte zu haben)? "
                "high = blockiert etwas Wichtiges oder eine Chance verstreicht bald, "
                "medium = normale Nachfrage, keine Eile aber sollte zeitnah beantwortet werden, "
                "low = reine Formalitaet/Kleinigkeit, kann warten.\n\n"
                'Antworte NUR als JSON: {"urgency": "low|medium|high"}'
            )}],
        )
        raw = response.content[0].text.strip()
        start, end = raw.index("{"), raw.rindex("}") + 1
        urgency = json.loads(raw[start:end]).get("urgency", "medium")
        return urgency if urgency in ("low", "medium", "high") else "medium"
    except Exception as e:
        _log(f"Urgency-Klassifikation fehlgeschlagen (Fallback medium): {e}")
        return "medium"


async def instagram_pass(config: dict):
    _log("Instagram-Check startet...")
    results = await instagram_tools.check_all_tracked_accounts()

    for r in results:
        handle = r["handle"]
        if "error" in r:
            if not _has_open_question_about(handle):
                question = (
                    f"Beim Instagram-Check von @{handle} gab es zuletzt einen Fehler "
                    f"({r['error']}). Ist der Account noch aktiv bzw. stimmt der Handle noch?"
                )
                client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
                try:
                    urgency = await _classify_question_urgency(client, question)
                finally:
                    await client.close()
                memory.add_pending_question(question, urgency=urgency)
            continue

        prev = instagram_tools.get_previous_snapshot(handle)
        if not prev:
            continue
        cur_followers = _parse_count(r.get("followers"))
        prev_followers = _parse_count(prev.get("followers"))
        if cur_followers is None or prev_followers is None:
            continue

        # Follower-swing WhatsApp alerts only for Luna Vale's OWN accounts —
        # Ahmad's explicit call: competitor growth updates were "too much
        # spam". Competitor data still gets tracked/read normally, just not
        # pushed as a notification.
        if handle not in config.get("luna_vale_accounts", []):
            continue

        delta = cur_followers - prev_followers
        if abs(delta) >= FOLLOWER_ALERT_THRESHOLD:
            direction = "gewachsen" if delta > 0 else "geschrumpft"
            text = (
                f"Jarvis Update: @{handle} ist {direction} um {abs(delta)} Follower "
                f"({prev_followers} -> {cur_followers})."
            )
            await _alert(config, text)

    links = instagram_tools.get_tracked_links()
    if links:
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            await instagram_tools.check_all_tracked_links(client)
        finally:
            await client.close()

    _log("Instagram-Check fertig.")


def _parse_money(raw) -> float:
    """'$2,269.50' -> 2269.50, best-effort. None if unparseable."""
    if not raw:
        return None
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _load_fanplace_snapshot() -> dict:
    if not os.path.exists(FANPLACE_SNAPSHOT_PATH):
        return {}
    try:
        with open(FANPLACE_SNAPSHOT_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_fanplace_snapshot(active_subs: int, all_time_earnings: float, full_data: dict = None):
    """full_data (2026-08-11, Ahmads Cockpit) haelt zusaetzlich den kompletten
    Rohabruf (earnings/subscribers Today/Week/Month/ALL-TIME) fest, nicht nur
    die zwei Felder fuer den Churn-Vergleich oben -- server.py's /api/cockpit
    liest das direkt aus dieser Datei statt selbst live zu scrapen (Fanplace
    braucht einen sichtbaren, nicht-headless Browser gegen Cloudflare, das
    waere bei jedem Seitenaufruf zu langsam/riskant fuer Browser-Kollisionen
    mit dieser Pass hier)."""
    os.makedirs(os.path.dirname(FANPLACE_SNAPSHOT_PATH), exist_ok=True)
    payload = {"active_subs": active_subs, "all_time_earnings": all_time_earnings}
    if full_data:
        payload["full"] = full_data
        payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(FANPLACE_SNAPSHOT_PATH, "w") as f:
        json.dump(payload, f)


async def fanplace_pass(config: dict):
    """Same pattern as the Instagram follower-swing check: compare against
    the last snapshot, only interrupt Ahmad for a BAD surprise. Revenue
    growth/new-subs are already covered in the daily summary — this is
    purely a churn tripwire (active subscriber count dropping), since
    that's the one Fanplace number that's actually worth reacting to
    immediately rather than reading about at day's end."""
    data = await fanplace.get_snapshot_data()
    if isinstance(data, str):  # "ERROR: ..."
        _log(f"Fanplace-Check fehlgeschlagen: {data}")
        return

    active_subs = _parse_count(data.get("subscribers", {}).get("ACTIVE"))
    all_time = _parse_money(data.get("earnings", {}).get("ALL-TIME"))
    if active_subs is None:
        return

    prev = _load_fanplace_snapshot()
    prev_subs = prev.get("active_subs")
    _save_fanplace_snapshot(active_subs, all_time, data)

    if prev_subs is None:
        return  # first-ever check, nothing to compare against yet

    dropped = prev_subs - active_subs
    if dropped >= FANPLACE_SUB_DROP_THRESHOLD:
        await _alert(
            config,
            f"⚠️ Jarvis Update: Aktive Fanplace-Abonnenten sind von {prev_subs} auf {active_subs} "
            f"gesunken (-{dropped}). Lohnt sich ein Blick, ob das normal ist.",
        )
        _log(f"Fanplace-Check: Abo-Rueckgang gemeldet ({prev_subs} -> {active_subs}).")
    else:
        _log(f"Fanplace-Check ok (aktive Abos: {active_subs}).")


async def _run_health_check(config: dict) -> str:
    """Ahmad's explicit choice (2026-08-06): completely silent when
    everything's fine, only speak up on a REAL problem. Checks the signals
    that would have actually caught today's real incidents sooner: is
    server.py (a separate process from this one) even responding, are any
    passes overdue by far more than their own interval, and are the same
    errors repeating despite self_improve_pass already trying. Returns ""
    when healthy."""
    issues = []

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "3", "http://localhost:8340/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        code = stdout.decode().strip()
        if code != "200":
            issues.append(f"server.py antwortet nicht normal (HTTP {code or 'kein Code — evtl. nicht gestartet'}).")
    except Exception as e:
        issues.append(f"server.py-Check selbst fehlgeschlagen: {_exc(e)}")

    timers = _load_timers()
    now = time.time()
    current_hour = int(time.strftime("%H"))
    in_jerome_hours = JEROME_WORK_HOUR_START <= current_hour < JEROME_WORK_HOUR_END
    for key, interval, label in (
        ("instagram", INSTAGRAM_INTERVAL_SECONDS, "Instagram-Check"),
        ("business", BUSINESS_CYCLE_INTERVAL_SECONDS, "Business-Zyklus (Discovery/Trial-Reel/Video-Analyse)"),
        ("research", RESEARCH_INTERVAL_SECONDS, "Recherche"),
        ("self_improve", SELF_IMPROVE_INTERVAL_SECONDS, "Self-Improve"),
        ("jerome", JEROME_INTERVAL_SECONDS, "Jerome-Chat-Check"),
    ):
        if key == "jerome" and not in_jerome_hours:
            continue  # gated to 10-17 Uhr by design — stale outside that window is EXPECTED, not a problem
        last = timers.get(key, 0)
        if last == 0:
            continue  # never run yet — not necessarily a problem (fresh install)
        overdue = now - last
        if overdue > interval * 3:
            issues.append(f"{label} lief zuletzt vor {overdue/3600:.1f}h (Takt eigentlich {interval/3600:.1f}h) — deutlich ueberfaellig.")

    since = now - 24 * 3600
    recent_errors = _collect_new_error_lines(since)
    from collections import Counter
    counts = Counter()
    last_seen = {}
    for raw in recent_errors:
        m = re.match(r'^\S+\.log: \[([^\]]+)\]\s*', raw)
        text = raw[m.end():] if m else raw
        counts[text] += 1
        if m:
            try:
                epoch = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            last_seen[text] = max(last_seen.get(text, 0), epoch)
    repeats = [
        text for text, n in counts.items()
        if n >= 3 and now - last_seen.get(text, 0) <= HEALTH_REPEAT_STILL_ACTIVE_SECONDS
    ]
    if repeats:
        newest = max(repeats, key=lambda t: last_seen.get(t, 0))
        ago_h = (now - last_seen.get(newest, now)) / 3600
        issues.append(
            f"{len(repeats)} Fehler wiederholen sich trotz Self-Improve und treten weiterhin auf "
            f"(zuletzt vor {ago_h:.1f}h), z.B.: {newest[:150]}"
        )

    return "\n".join(f"- {i}" for i in issues)


JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
JARVIS_SERVER_LOG = os.path.join(_LOG_DIR, "jarvis-server.log")


async def _is_server_running() -> bool:
    """Gleiche Pruefung wie scripts/launch-session.sh's start_server() und
    _run_health_check() oben — curl gegen den Health-Endpoint, kein Python-
    Prozess-Scan (der wuerde ein hängendes, nicht mehr antwortendes
    server.py faelschlich als 'laeuft' zaehlen)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "2", "http://localhost:8340/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() == "200"
    except Exception:
        return False


async def _ensure_server_running_silently():
    """Startet server.py im Hintergrund OHNE Fenster/Fokus zu stehlen, falls
    er gerade nicht laeuft — Ahmads bewusste Entscheidung (2026-08-08) gegen
    die aggressivere Variante (automatisches Chrome-Oeffnen wie beim
    Doppelklatschen). Ein schon offener, aber unfokussierter Jarvis-Browser-
    Tab verbindet sich ueber seine bestehende Reconnect-Logik (main.js,
    ws.onclose, alle 3s) von selbst neu, sobald der Server antwortet — dann
    kann der Live-Event-Kanal (memory.add_live_event, siehe _alert oben)
    die Abend-Zusammenfassung sprechen, sobald Ahmad als naechstes hinschaut.

    Server (2026-08-10, Roadmap Punkt 21): ein rohes subprocess.Popen wie im
    Mac-Zweig unten wuerde hier eine zweite, von systemd unverwaltete
    server.py-Instanz daneben starten (Port-Konflikt bzw. Chaos), weil
    jarvis-server.service bereits als eigener Dienst laeuft — der richtige
    Hebel ist systemctl restart, nicht ein neuer Kindprozess."""
    if await _is_server_running():
        return
    if IS_SERVER:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "restart", "jarvis-server",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            _log("jarvis-server.service antwortete nicht, per systemctl neu gestartet.")
        except Exception as e:
            _log(f"systemctl-Neustart von jarvis-server fehlgeschlagen: {_exc(e)}")
        return
    try:
        with open(JARVIS_SERVER_LOG, "a") as log_file:
            subprocess.Popen(
                [sys.executable, os.path.join(JARVIS_DIR, "server.py")],
                cwd=JARVIS_DIR, stdout=log_file, stderr=log_file,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        _log("server.py still im Hintergrund gestartet (Abend-Briefing).")
    except Exception as e:
        _log(f"Stiller Server-Start fehlgeschlagen (ignoriert, WhatsApp geht trotzdem raus): {e}")


async def daily_summary_pass(config: dict):
    """Once per calendar day, no earlier than DAILY_SUMMARY_HOUR — an
    end-of-day WhatsApp wrap-up so Ahmad gets a real daily rhythm (one
    morning-ish full briefing when he first opens Jarvis, one evening
    summary here) instead of the same numbers repeated all day."""
    if memory.has_daily_summary_today():
        return
    if int(time.strftime("%H")) < DAILY_SUMMARY_HOUR:
        return

    _log("Tages-Zusammenfassung startet...")
    try:
        fanplace_snapshot = await fanplace.get_snapshot()
    except Exception as e:
        fanplace_snapshot = f"ERROR: {e}"

    try:
        sltbio_snapshot = await slt_bio_tools.format_for_jarvis()
    except Exception as e:
        sltbio_snapshot = f"ERROR: {e}"

    instagram_summary = instagram_tools.format_trend_summary()
    knowledge_today = memory.get_knowledge(max_chars=3000)

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "Du bist Jarvis. Schreib Ahmad eine KURZE Tages-Abschluss-Zusammenfassung fuer "
                    "WhatsApp (max. 4-5 Saetze, Absaetze mit Leerzeilen fuer Lesbarkeit) basierend auf:\n\n"
                    f"Fanplace:\n{fanplace_snapshot}\n\nSLT.bio (Link-in-Bio Klicks):\n{sltbio_snapshot}\n\n"
                    f"Instagram:\n{instagram_summary}\n\n"
                    f"Heute recherchiert:\n{knowledge_today}\n\n"
                    "Nur das Wichtigste, keine Wiederholung von Selbstverstaendlichem. Deutsch, "
                    "Jarvis-Stil (trocken, praezise, per Sie)."
                ),
            }],
        )
        summary = response.content[0].text.strip()
    except Exception as e:
        _log(f"FEHLER bei Tages-Zusammenfassung (LLM): {_exc(e)}")
        return
    finally:
        await client.close()

    try:
        health_issues = await _run_health_check(config)
    except Exception as e:
        health_issues = f"- Gesundheitscheck selbst fehlgeschlagen: {_exc(e)}"

    message = f"📋 Jarvis Tagesabschluss:\n\n{summary}"
    if health_issues:
        message += f"\n\n⚠️ System-Check:\n{health_issues}"
    await _ensure_server_running_silently()
    await _alert(config, message)
    memory.mark_daily_summary_done()
    _log(f"Tages-Zusammenfassung gesendet. Health-Issues: {health_issues or 'keine'}")


async def research_pass(config: dict):
    _log("Recherche startet...")
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        findings = await research.run_research_cycle(client)
        if findings:
            digest = await research.summarize_for_whatsapp(client, findings)
            await _alert(config, f"📚 Jarvis Recherche-Update:\n\n{digest}")
    finally:
        await client.close()

    _log(f"Recherche fertig, {len(findings)} neue Erkenntnisse.")


async def _validate_and_add_candidate(handle: str, config: dict, source: str, niche: str = None) -> bool:
    """Shared gate for ANY discovered handle, regardless of where it came
    from (web search, or Instagram's own on-page suggestions): must be a
    real, reachable account with a plausible follower count. Returns True
    if it was newly added. niche (if known) gets recorded so the Sheet sync
    labels it correctly instead of defaulting to Goth/alternative."""
    # Never track Ahmad's own personal viewing account as a "competitor" —
    # it always shows up in Instagram's nav chrome since we browse logged in
    # as it. Hard boundary, not just a filtering heuristic.
    if handle.lower() == config.get("instagram_username", "").lower():
        return False

    result = await instagram_tools.check_profile(handle)
    followers = _parse_count(result.get("followers"))
    if "error" in result or followers is None or not (DISCOVERY_MIN_FOLLOWERS <= followers <= DISCOVERY_MAX_FOLLOWERS):
        # Nur ueber den Fehler reden, wenn es einen GIBT (2026-08-07 fix): diese
        # Zeile hing bisher immer "error=None" an, und _collect_new_error_lines
        # matcht "error" case-insensitiv. Damit zaehlte JEDE voellig normale
        # Ablehnung (zu wenig/zu viele Follower, Handle existiert nicht — die
        # Websuche liefert erwartbar auch Nieten) als neuer "Fehler", loeste
        # einen Claude-Code-Self-Improve-Lauf aus und schickte Ahmad eine
        # WhatsApp ueber ein Gate, das genau richtig gearbeitet hat. Genau die
        # Ping-Schleife, vor der der Kommentar bei SELF_IMPROVE_LOG_MARKERS warnt.
        reason = f"followers={followers}"
        if result.get("error"):
            reason += f", error={result['error']}"
        _log(f"Kandidat @{handle} ({source}) verworfen ({reason})")
        return False

    if not instagram_tools.add_competitor_account(handle):
        return False

    if niche:
        instagram_tools.record_competitor_niche(handle, niche)

    memory.add_knowledge(
        f"Neuer Account @{handle} automatisch zur Konkurrenz-Beobachtung hinzugefuegt "
        f"({result.get('followers')} Follower, Quelle: {source}).",
        category="discovery",
    )

    # Sheet sync deliberately NOT done per-handle here — sheets_sync_pass runs
    # right after discovery_pass in the same cycle and covers the whole list at
    # once, so this doesn't need its own file on the Desktop every single time.
    await _alert(
        config,
        f"Jarvis Update: Neuer vielversprechender Account @{handle} entdeckt und zur "
        f"Beobachtung hinzugefuegt ({result.get('followers')} Follower). Wird im "
        f"naechsten Sheet-Sync in die Target Creator List aufgenommen.",
    )
    return True


async def sheets_sync_pass(config: dict):
    """Make sure EVERY currently tracked competitor — not just freshly
    discovered ones — has a row in the Target Creator List tab. Catches the
    accounts Ahmad added by hand before this system existed, and self-heals
    any sync that failed earlier (e.g. before Sheets auth was completed).
    Caller must pass a freshly-loaded config — discovery_pass may have just
    grown competitor_accounts and a stale config would miss the addition."""
    handles = config.get("competitor_accounts", [])
    if not handles:
        return
    try:
        niches = instagram_tools.get_competitor_niches()
        result = await sheets_tools.sync_competitor_accounts(handles, niches)
        _log(f"Sheet-Sync (alle Konkurrenten): {result}")
        if not result.startswith("ERROR") and "bereits" not in result:
            await _alert(config, f"Jarvis: Tracking-Sheet live aktualisiert — {result}")
    except Exception as e:
        _log(f"FEHLER beim vollstaendigen Sheet-Sync: {_exc(e)}")


DISCOVERY_NICHES = [
    "Dark Feminine / Alt-Goth Aesthetic",
    "Cowgirl / Country-Girl Aesthetic",
    "Cosplay / Anime Character Transformation",
]


async def discovery_pass(config: dict):
    """Look for promising new accounts via web search and, if they check
    out, add them straight to the watchlist — Ahmad opted for auto-add over
    asking first. Searches EACH of Luna Vale's 3 niches separately (2026-08-06
    fix) — a single combined search used to skew entirely toward whichever
    niche dominated the known-accounts list (all-goth at the time), so
    Cowgirl/Cosplay competitors were never actually being found."""
    _log("Account-Discovery (Websuche) startet...")
    known = config.get("luna_vale_accounts", []) + config.get("competitor_accounts", [])
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    all_candidates = []
    try:
        for niche in DISCOVERY_NICHES:
            try:
                found = await research.discover_new_accounts(client, known, niche=niche)
                all_candidates.extend((h, niche) for h in found)
            except Exception as e:
                _log(f"FEHLER bei Account-Discovery fuer Nische '{niche}': {_exc(e)}")
    finally:
        await client.close()

    for handle, niche in all_candidates:
        await _validate_and_add_candidate(handle, config, source=f"Websuche ({niche})", niche=niche)

    _log(f"Account-Discovery (Websuche) fertig, {len(all_candidates)} Kandidaten geprueft ueber {len(DISCOVERY_NICHES)} Nischen.")


VIRAL_ALERTED_PATH = os.path.join(os.path.dirname(__file__), "memory", "viral_alerted_videos.json")
VIRAL_LIKES_MULTIPLIER = 2.0  # same "2x baseline" definition the sheet itself uses for Outlier
VIRAL_MIN_BASELINE_SAMPLES = 2  # don't flag off a single prior data point


def _load_viral_alerted() -> set:
    if not os.path.exists(VIRAL_ALERTED_PATH):
        return set()
    try:
        with open(VIRAL_ALERTED_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_viral_alerted(url: str):
    alerted = _load_viral_alerted()
    alerted.add(url)
    os.makedirs(os.path.dirname(VIRAL_ALERTED_PATH), exist_ok=True)
    with open(VIRAL_ALERTED_PATH, "w") as f:
        json.dump(sorted(alerted), f)


def _parse_video_raw_stats(raw: str) -> dict:
    """Parse instagram_tools' 'views=X likes=Y comments=Z audience=...'
    format into ints — None for anything not visible ('unbekannt' etc.),
    never a guessed number."""
    stats = {}
    for key in ("views", "likes", "comments"):
        match = re.search(rf'{key}=([\d.,]+)', raw)
        if match:
            try:
                stats[key] = int(match.group(1).replace(".", "").replace(",", ""))
            except ValueError:
                stats[key] = None
        else:
            stats[key] = None
    return stats


def _recent_likes_baseline(handle: str, exclude_url: str) -> tuple:
    """Average likes from this account's recent video-analysis history,
    excluding the video being evaluated — returns (average, sample_count).
    Likes, not views: views are frequently not visible via screenshot
    (Instagram hides them on many post layouts), likes reliably are."""
    if not os.path.exists(instagram_tools.VIDEO_ANALYSIS_PATH):
        return None, 0
    likes_values = []
    with open(instagram_tools.VIDEO_ANALYSIS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("handle") != handle or entry.get("url") == exclude_url or "raw" not in entry:
                continue
            stats = _parse_video_raw_stats(entry["raw"])
            if stats.get("likes") is not None:
                likes_values.append(stats["likes"])
    if not likes_values:
        return None, 0
    recent = likes_values[-10:]
    return sum(recent) / len(recent), len(recent)


async def _check_viral_candidates(account: str, videos: list, config: dict, is_own_account: bool):
    """If a video's likes are well above this account's recent normal
    range, tell Ahmad with the LINK so he can see it himself. Two
    different purposes depending on whose account it is:
      - Luna Vale's own: this is a real Trial Reel data point — log it
        into Winner Tracking so the Trial Reel pass can pick it up.
      - Competitor: this is inspiration to study/recreate, not a Winner
        Tracking entry (that tab is about Ahmad's own content) — instead
        a short note with the link goes onto that competitor's row in
        Target Creator List, and the WhatsApp message is framed as
        'worth a look', not as Ahmad's own performance data."""
    alerted = _load_viral_alerted()
    for video in videos:
        url = video.get("url")
        if not url or url in alerted or "raw" not in video:
            continue
        stats = _parse_video_raw_stats(video["raw"])
        if stats.get("likes") is None:
            continue

        baseline, sample_count = _recent_likes_baseline(account, url)
        if baseline is None or sample_count < VIRAL_MIN_BASELINE_SAMPLES:
            continue
        if stats["likes"] < baseline * VIRAL_LIKES_MULTIPLIER:
            continue

        if is_own_account:
            entry = {"account": account, "video_link": url, "views": stats.get("views") or 0}
            if stats.get("comments") is not None:
                entry["comments_total"] = stats["comments"]
            sheet_result = await sheets_tools.add_winner_tracking_entry(entry)
            baseline_result = await sheets_tools.compute_baseline_avg(account, url)
            _log(f"Viral-Kandidat (eigen) @{account} {url}: {sheet_result} | {baseline_result}")
            await _alert(
                config,
                f"🔥 Jarvis Update: @{account} hat vermutlich ein virales Video — "
                f"{stats['likes']} Likes (ueblich zuletzt ca. {round(baseline)}). {url}",
            )
        else:
            note_result = await sheets_tools.add_target_creator_note(
                account, f"Virales Video ({time.strftime('%Y-%m-%d')}): {url}"
            )
            _log(f"Viral-Kandidat (Konkurrenz) @{account} {url}: {note_result}")
            await _alert(
                config,
                f"👀 Jarvis Update: Konkurrenz-Video von @{account} geht viral (vermutlich) — "
                f"lohnt sich zum Nachbauen: {url}",
            )
        _mark_viral_alerted(url)


async def video_analysis_pass(config: dict, start_index: int) -> int:
    """Deep per-post check (views/likes/audience quality) for a rotating
    slice of tracked accounts, so a full sweep happens across a few ticks
    instead of hammering every account's posts every single cycle. Also
    harvests any similar-account suggestions Instagram surfaced on those
    same profile pages — a second, more targeted discovery source that
    costs zero extra Instagram traffic since the page is already loaded."""
    handles = config.get("luna_vale_accounts", []) + config.get("competitor_accounts", [])
    if not handles:
        return 0

    luna_vale = set(config.get("luna_vale_accounts", []))
    known_lower = {h.lower() for h in handles}
    similar_candidates = []

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        for i in range(min(VIDEO_ACCOUNTS_PER_PASS, len(handles))):
            idx = (start_index + i) % len(handles)
            handle = handles[idx]
            try:
                result = await instagram_tools.analyze_recent_videos(handle, client, count=VIDEO_POSTS_PER_ACCOUNT)
                for c in result.get("similar_accounts", []):
                    if c not in known_lower and c not in similar_candidates:
                        similar_candidates.append(c)
                await _check_viral_candidates(handle, result.get("videos", []), config, is_own_account=handle in luna_vale)
            except Exception as e:
                _log(f"FEHLER bei Video-Analyse @{handle}: {_exc(e)}")
            await asyncio.sleep(3)

        _log(f"Account-Discovery (Aehnliche Accounts): {len(similar_candidates)} Kandidaten aus Profilseiten.")
        for c in similar_candidates:
            await _validate_and_add_candidate(c, config, source="Aehnliche Accounts")
    finally:
        await client.close()

    return (start_index + VIDEO_ACCOUNTS_PER_PASS) % len(handles)


def _read_scan_cursor() -> float:
    try:
        with open(CLAUDE_CODE_STATE_PATH) as f:
            return json.load(f).get("last_scan_epoch", 0)
    except (OSError, json.JSONDecodeError):
        return 0


def _write_scan_cursor(epoch: float):
    with open(CLAUDE_CODE_STATE_PATH, "w") as f:
        json.dump({"last_scan_epoch": epoch}, f)


# Self-Improve's OWN bookkeeping lines are German prose that naturally
# contains the word "Fehler" — "1 neue Fehler gefunden", "...ist
# fehleranfaellig...", plus whatever Claude Code's summary says. Before the
# 2026-08-07 case-insensitive widening below they slipped through because
# they aren't uppercase; afterwards they matched, and each pass started
# feeding the NEXT pass its own output as if it were a fresh error (only
# "keine neuen Fehler" was ever special-cased). That is a self-sustaining
# loop: a real fix -> a reflection line -> another Claude Code run with
# nothing to fix -> another WhatsApp ping to Ahmad -> repeat every 30min.
# Genuine exception logs (e.g. "FEHLER bei Selbstreflektion") are NOT listed
# here on purpose — those are real failures and should still be investigated.
#
# Dasselbe gilt (2026-08-07, zweites Leck) fuer den WhatsApp-Echo in _alert():
# das Log bekommt dort den KOMPLETTEN Alarmtext. Bei der Self-Improve-Meldung
# ist das wortwoertlich nochmal Claude Codes Zusammenfassung ("...waren gar
# keine Fehler...") — nur mit dem Prefix "ALARM gesendet:" davor, weshalb die
# Marker oben nicht gegriffen haben und der 13:10-Alarm 9 Minuten spaeter als
# frischer Fehler zurueckkam. Alarme sind grundsaetzlich nur Benachrichtigungen
# ueber etwas, das die ausloesende Stelle ohnehin schon selbst geloggt hat
# (jede echte Exception laeuft durch _exc() an ihrem eigenen Call-Site) — die
# Echo-Zeile ist also NIE die einzige Aufzeichnung eines Fehlers und kann
# gefahrlos komplett ignoriert werden. Deckt zugleich alle anderen Alarme ab,
# deren Text zufaellig "Fehler"/"error" enthaelt (Recherche-Digest,
# Jerome-Antworten, die Guthaben-Warnung in main()).
#
# Drittes Leck derselben Art (2026-08-11, live vorgefunden): Erfolgs-Zeilen,
# die FREIEN Text von woanders in das Log echoen, koennen das Wort "Fehler"
# rein inhaltlich enthalten, ohne dass irgendetwas schiefgegangen ist.
# Ausloeser war "Screen-Awareness erfasst: Ahmad beschaeftigt sich gerade mit
# der Ueberpruefung und Fehlerbehandlung in seinem Bash-Script." — eine voll
# erfolgreiche Vision-Beschreibung von Ahmads Bildschirm, in der "Fehler-"
# nur als Teilstring in "Fehlerbehandlung" steckt. Das hat einen kompletten
# Claude-Code-Lauf mit Datei-/Shell-Zugriff und potentiellem systemctl-Neustart
# beider Dienste ausgeloest, fuer gar kein Problem. Dieselbe Falle bei den
# Skill-Growth-Zeilen (Claude Codes Zusammenfassung + die vom Modell
# formulierte Faehigkeits-Luecke, exakt analog zu "self-improve ergebnis:")
# und beim Morgen-Briefing, dessen Text die 24h-Self-Improve-Zusammenfassung
# woertlich mitfuehrt ("... N Fehler behoben ..."). Alle diese Zeilen werden
# nur im Erfolgsfall geschrieben; ihre echten Fehlschlaege loggen an derselben
# Stelle separat mit "FEHLER ..."/"fehlgeschlagen" und bleiben sichtbar.
# Die beiden Self-Improve-Status-Marker sind bewusst PRAEZISE und nicht bloss
# "self-improve:" (so stand es bis 2026-08-11): das pauschale Prefix hat auch
# "Self-Improve: Neustart von jarvis-brain fehlgeschlagen: ..." verschluckt —
# genau der Fehlschlag, der einen erfolgreichen Fix wirkungslos macht (siehe
# den systemctl-Block in self_improve_pass) und darum als einziger aus diesem
# Bereich sichtbar bleiben MUSS. Es sind nur diese zwei Status-Zeilen, die
# ueberhaupt "Fehler"/"error" enthalten, alle anderen "Self-Improve:"-Zeilen
# matchen den Scanner ohnehin nicht.
SELF_IMPROVE_LOG_MARKERS = (
    "self-improve: keine neuen fehler",   # "...seit dem letzten Scan."
    "neue fehler gefunden, delegiere",    # "Self-Improve: N neue Fehler gefunden, delegiere an..."
    "self-improve ergebnis:",     # Claude Code's own summary, often quotes the error text
    "selbstreflektion gespeichert:",
    "alarm gesendet:",            # _alert()'s echo of an outgoing WhatsApp text
    "alarm (kein alert_phone",    # ...und dieselbe Zeile, wenn keine Nummer konfiguriert ist
    "screen-awareness erfasst:",  # Vision-Beschreibung von Ahmads Bildschirm, freier Text
    "skill-growth (luecke)",      # die vom Modell formulierte Faehigkeits-Luecke, freier Text
    "skill-growth (eigene idee)", # dito, der Ideen-Zweig
    "skill-growth ergebnis:",     # Claude Codes eigene Zusammenfassung, wie bei Self-Improve
    "morgen-briefing gesendet:",  # enthaelt die Self-Improve-/Skill-Growth-Zusammenfassung
)


def _collect_new_error_lines(since_epoch: float) -> list:
    """New FEHLER/ERROR log lines across Jarvis's own logs since the last
    scan, so Claude Code investigates each recurring problem only once.

    Case-INSENSITIVE match (2026-08-07 fix): the check used to require the
    exact uppercase substrings "FEHLER"/"ERROR", but 10 call sites across
    content_strategy.py and instagram_tools.py log with mixed-case "Fehler"
    (e.g. "Fehler bei Vision-Analyse") — those lines were silently invisible
    to this scanner since the day it was built, meaning real, repeated
    errors in video capture, hashtag search, and content-brief data
    gathering never once reached Claude Code for investigation. Caught by
    Ahmad asking whether old errors get cleaned up after a fix — they
    weren't even being SEEN, let alone cleaned up."""
    lines = []
    for path in JARVIS_LOGS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    lowered = line.lower()
                    if "fehler" not in lowered and "error" not in lowered:
                        continue
                    if any(marker in lowered for marker in SELF_IMPROVE_LOG_MARKERS):
                        continue  # self_improve_pass's own status lines, not real error reports
                    try:
                        ts = time.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                        epoch = time.mktime(ts)
                    except (ValueError, IndexError):
                        continue
                    if epoch > since_epoch:
                        lines.append(f"{os.path.basename(path)}: {line.strip()}")
        except OSError:
            continue
    return lines


async def self_improve_pass(config: dict):
    """Ahmad explicitly authorized Jarvis to reach for Claude Code on its
    own, including unsupervised, with full file/shell access. Scope it to
    something concrete rather than an open-ended 'do whatever' loop: scan
    Jarvis's own logs for recurring errors since the last pass and delegate
    diagnosis/fixing to Claude Code. Silent by design (Ahmad, 2026-08-07:
    "nur Wichtiges... das soll 100% automatisch funktionieren, keine
    Selbstverbesserungs-Updates in den Chat") — self-repair is routine
    maintenance, not something worth a WhatsApp ping every time. Still fully
    logged (jarvis-brain.log) and feeds _reflect_on_fix's lesson into
    Jarvis's own long-term memory either way, so nothing is actually lost,
    it just doesn't interrupt Ahmad's chat."""
    since = _read_scan_cursor()
    now_epoch = time.time()
    errors = _collect_new_error_lines(since)
    _write_scan_cursor(now_epoch)

    if not errors:
        _log("Self-Improve: keine neuen Fehler seit dem letzten Scan.")
        return

    _log(f"Self-Improve: {len(errors)} neue Fehler gefunden, delegiere an Claude Code.")
    task = (
        "Im Hintergrund-Prozess dieses Jarvis-Projekts (background_brain.py und die Module, "
        "die er nutzt) sind seit dem letzten Check folgende Fehler aufgetreten:\n\n"
        + "\n".join(errors[-30:]) +
        "\n\nUntersuche die Ursache im Code. Wenn du eine sichere, minimale Korrektur machen "
        "kannst, tu es. Wenn die Ursache unklar ist oder eine Entscheidung von Ahmad braucht "
        "(z.B. ein fehlender API-Zugang), aendere NICHTS und beschreibe stattdessen genau was "
        "das Problem ist.\n\n"
        "WICHTIG (Ahmad, 2026-08-10, nach einem echten Vorfall — Jarvis erklaerte live einen "
        "laengst durch insights_inbox_pass ersetzten alten Workflow, weil niemand sein eigenes "
        "Gedaechtnis/claude_app_status.md aktualisiert hatte, als der Workflow im Code geaendert "
        "wurde): falls deine Korrektur einen dokumentierten Geschaeftsprozess/Workflow betrifft "
        "(z.B. WIE Ahmad Daten eingibt, welcher Kanal wofuer genutzt wird), pruefe ob "
        "claude_app_status.md dazu etwas Veraltetes sagt, und aktualisiere es in DERSELBEN "
        "Aenderung. Ein Workflow der nur im Code existiert, aber nirgends in Jarvis' eigenem "
        "Wissen dokumentiert ist, fuehrt sonst dazu dass Jarvis im Gespraech weiter den alten "
        "Prozess erklaert.\n\n"
        "WICHTIG (2026-08-11, nach einem echten Vorfall): fuehre selbst KEINE Git-Befehle aus "
        "(kein git add/commit/push) -- das Commiten+Pushen uebernimmt automatisch der Prozess, "
        "der dich aufgerufen hat, direkt NACH dieser Aufgabe. Ein eigener Commit aendert den "
        "Besitzer von .git auf diesen eingeschraenkten User, wodurch der aufrufende Prozess "
        "(laeuft als root) 'git status' danach nicht mehr ausfuehren kann ('detected dubious "
        "ownership') -- genau das ist schon passiert und hat einen echten, korrekten Fix vorerst "
        "unsichtbar gemacht. Bearbeite nur Dateien, committe nichts selbst.\n\n"
        "Fasse am Ende in 2-3 Saetzen auf Deutsch zusammen was du getan oder herausgefunden hast."
    )
    result, commit_hash = await claude_code_tool.run_claude_code_with_commit(task)
    _log(f"Self-Improve Ergebnis: {result[:300]}")

    # Roadmap Punkt 22 — sichtbares Changelog, NACHTRAEGLICH nachvollziehbar
    # (morgens im Briefing / auf Nachfrage), ohne den laufenden Chat zu
    # unterbrechen (bleibt bewusst still, siehe Docstring oben).
    memory.add_self_improve_entry("\n".join(errors[-30:]), result, commit_hash)

    # VOR dem moeglichen Neustart unten (der diesen Prozess selbst killt) --
    # sonst wuerde die Lektion bei jedem erfolgreichen Fix verloren gehen.
    await _reflect_on_fix(config, errors, result)

    if commit_hash and IS_SERVER:
        # Ohne das hier bleibt eine erfolgreiche Korrektur wirkungslos: Python
        # importiert Module einmal beim Start, ein geaendertes .py auf der
        # Platte aendert nichts am laufenden Prozess. Beide Dienste neu
        # starten (nicht nur jarvis-brain), weil die Korrektur auch ein von
        # server.py mitgenutztes Modul betroffen haben kann (z.B. memory.py) —
        # live vorgefunden, 2026-08-10/11: ein echter, korrekter Fix haette ohne
        # das hier nie wirklich gegriffen, selbst nach erfolgreichem Commit.
        # Reihenfolge bewusst: jarvis-server zuerst, jarvis-brain zuletzt --
        # der Neustart von jarvis-brain killt diesen Prozess selbst.
        for service in ("jarvis-server", "jarvis-brain"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "restart", service,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
                _log(f"Self-Improve: {service} neu gestartet (Fix aus Commit {commit_hash}).")
            except Exception as e:
                _log(f"Self-Improve: Neustart von {service} fehlgeschlagen: {_exc(e)}")


async def _reflect_on_fix(config: dict, errors: list, fix_result: str):
    """Ahmad's ask (2026-08-07): functional self-awareness — not just fixing
    an error and moving on, but extracting the LESSON so the same class of
    mistake is less likely later. One cheap follow-up call, saved into the
    'self' memory category, which already flows into every live
    conversation's LANGZEITGEDAECHTNIS block. Best-effort: a failure here
    should never mask the fix itself, which already succeeded/was reported
    above by the time this runs."""
    try:
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": (
                        "Diese Fehler sind aufgetreten:\n" + "\n".join(errors[-10:]) +
                        f"\n\nSo wurde reagiert:\n{fix_result[:500]}\n\n"
                        "Formuliere daraus EINE knappe Lektion (ein Satz, Deutsch) darueber, was an "
                        "Jarvis' eigenem Verhalten/Werkzeugen unzuverlaessig war und WANN das typischerweise "
                        "auftritt — nicht das Fehlersymptom wiederholen, sondern was Jarvis kuenftig anders "
                        "einschaetzen/vorsichtiger behandeln sollte. Wenn keine echte wiederverwendbare "
                        "Lektion erkennbar ist (z.B. reiner Internet-Ausfall, einmaliger Zufall), antworte "
                        "exakt 'KEINE_LEKTION'."
                    ),
                }],
            )
        finally:
            await client.close()
        lesson = "\n".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        _log(f"FEHLER bei Selbstreflektion: {_exc(e)}")
        return

    if not lesson or "KEINE_LEKTION" in lesson:
        return
    memory.remember(lesson, category="self")
    _log(f"Selbstreflektion gespeichert: {lesson}")


LUNA_VALE_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")


def _get_luna_vale_knowledge() -> str:
    """Base playbook PLUS whatever live-Jarvis learned mid-conversation and
    saved via REMEMBER (category=business) — same reasoning as jerome_comm's
    identical helper: Ahmad should be able to update Jarvis's real knowledge
    just by telling it something live, not only through the separate
    background research pipeline (2026-08-06)."""
    try:
        with open(LUNA_VALE_STATUS_PATH, "r", encoding="utf-8") as f:
            base = f.read().strip()
    except OSError:
        base = ""
    live_updates = memory.get_category("business")
    if live_updates:
        base += f"\n\n## Live von Ahmad ergaenzt (waehrend Gespraechen)\n{live_updates}"
    return base


def _read_skill_growth_state() -> dict:
    try:
        with open(SKILL_GROWTH_SCAN_STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_skill_growth_state(state: dict):
    with open(SKILL_GROWTH_SCAN_STATE_PATH, "w") as f:
        json.dump(state, f)


def _recent_history_since(cursor_epoch: float, max_turns: int = 60) -> str:
    """memory.get_recent_history() ist rein zahlenbasiert (letzte N Zuege),
    kein Zeitfilter — hier manuell auf alles NACH dem letzten Scan-Cursor
    eingrenzen, damit derselbe alte Gespraechsausschnitt nicht bei jedem
    30-Min-Tick erneut als 'Luecke' auftaucht, bis er aus dem N-Fenster
    herausgerutscht ist."""
    raw = memory.get_recent_history(max_turns=max_turns)
    if not raw:
        return ""
    kept = []
    for line in raw.split("\n"):
        try:
            ts = time.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
            epoch = time.mktime(ts)
        except (ValueError, IndexError):
            kept.append(line)  # kann nicht geparst werden -> sicherheitshalber behalten
            continue
        if epoch > cursor_epoch:
            kept.append(line)
    return "\n".join(kept)


async def _find_capability_gap(config: dict, conversation_excerpt: str) -> str:
    """Reaktiver Zweig: ehrliches 'nichts gefunden' ist der Normalfall, genau
    wie ueberall sonst in diesem Projekt (kein Erfinden von Luecken)."""
    if not conversation_excerpt.strip():
        return ""
    try:
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        "Hier ist ein Ausschnitt aus Ahmads juengster Konversation mit Jarvis:\n\n"
                        + conversation_excerpt[-4000:] +
                        "\n\nWollte Ahmad hier erkennbar etwas, das Jarvis NICHT konnte oder wofuer "
                        "kein Werkzeug existierte (z.B. 'das kann ich nicht', 'dafuer habe ich kein "
                        "Werkzeug')? Falls ja: beschreibe die fehlende Faehigkeit in EINEM knappen "
                        "Satz auf Deutsch. Falls nicht klar erkennbar, antworte EXAKT KEINE_LUECKE."
                    ),
                }],
            )
        finally:
            await client.close()
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        _log(f"FEHLER beim Luecken-Scan: {_exc(e)}")
        return ""
    if not text or "KEINE_LUECKE" in text:
        return ""
    return text


async def _propose_skill_idea(config: dict) -> str:
    """Eigene-Ideen-Zweig: bewusst zurueckhaltend formuliert ('nur eine
    wirklich naheliegende Idee, keine Spekulation') und seltener getaktet
    als der reaktive Zweig, siehe SKILL_GROWTH_IDEA_INTERVAL_SECONDS."""
    knowledge = _get_luna_vale_knowledge()[:3000]
    researched = memory.get_knowledge(max_chars=2000)
    try:
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        "Das ist Jarvis' Geschaeftswissen und selbst recherchiertes Wissen:\n\n"
                        f"{knowledge}\n\n{researched}\n\n"
                        "Basierend NUR darauf: gibt es eine konkrete, klar umrissene neue Faehigkeit "
                        "(ein einzelnes Werkzeug), die Jarvis sich sinnvollerweise selbst bauen sollte? "
                        "Nur eine wirklich naheliegende, gut begruendete Idee, keine Spekulation und "
                        "nichts das schon offensichtlich existiert. Falls nichts Klares, antworte "
                        "EXAKT KEINE_IDEE."
                    ),
                }],
            )
        finally:
            await client.close()
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        _log(f"FEHLER beim Ideen-Vorschlag: {_exc(e)}")
        return ""
    if not text or "KEINE_IDEE" in text:
        return ""
    return text


async def skill_growth_pass(config: dict):
    """Ahmad (2026-08-10): "ich brauche es, damit er eigenstaendiger wird" —
    ausdrueckliche Erlaubnis, dass Jarvis sich selbst neue TOOL_REGISTRY-
    Eintraege gibt, nicht nur Fehler behebt (siehe self_improve_pass oben,
    gleicher sichere Baumechanismus: claude_code_tool ueber Git, keine
    separate unauthentifizierte Laufzeitumgebung). Reaktiv (Luecken-Scan,
    jeder Tick) UND eigene Ideen (viel seltener, siehe
    SKILL_GROWTH_IDEA_INTERVAL_SECONDS) — Ahmads eigene Wahl bei der
    Rueckfrage, mit Tageslimit dagegen dass das ausufert."""
    state = _read_skill_growth_state()
    now_epoch = time.time()
    scan_cursor = state.get("last_scan_epoch", now_epoch - SKILL_GROWTH_INTERVAL_SECONDS)
    idea_cursor = state.get("last_idea_epoch", 0)

    if memory.get_skill_builds_today() >= memory.MAX_SKILL_BUILDS_PER_DAY:
        _log("Skill-Growth: Tageslimit bereits erreicht, ueberspringe.")
        state["last_scan_epoch"] = now_epoch
        _write_skill_growth_state(state)
        return

    gap_or_idea = await _find_capability_gap(config, _recent_history_since(scan_cursor))
    source = "Luecke"

    if not gap_or_idea and now_epoch - idea_cursor >= SKILL_GROWTH_IDEA_INTERVAL_SECONDS:
        gap_or_idea = await _propose_skill_idea(config)
        source = "eigene Idee"
        state["last_idea_epoch"] = now_epoch

    state["last_scan_epoch"] = now_epoch
    _write_skill_growth_state(state)

    if not gap_or_idea:
        _log("Skill-Growth: keine Luecke und keine Idee diesen Tick.")
        return

    _log(f"Skill-Growth ({source}): {gap_or_idea[:150]} — delegiere an Claude Code.")
    task = (
        f"Jarvis (dieses Projekt) fehlt folgende Faehigkeit: {gap_or_idea}\n\n"
        "Pruefe ZUERST per grep in server.py's TOOL_REGISTRY ob es dafuer schon ein Werkzeug "
        "gibt (auch unter anderem Namen) -- falls ja, aendere NICHTS und beschreibe das "
        "stattdessen.\n\n"
        "Falls nicht: baue ein neues Werkzeug nach dem etablierten Muster in server.py "
        "(ToolSpec-Dataclass, TOOL_REGISTRY-Dict, siehe z.B. den Eintrag 'browser_extract' als "
        "Vorlage) -- Handler-Funktion + Schema-Eintrag, deutsche Beschreibung, ehrliche "
        "Fehlerbehandlung statt erfundener Werte.\n\n"
        "WICHTIG -- falls dieses Werkzeug etwas WIRKLICH REALES ausloesen koennte (Nachricht "
        "senden, Kalender/Geld/Kauf/oeffentlich sichtbare Aktion): baue einen "
        "'confirmed: bool'-Parameter (optional, im input_schema NICHT als required) ein. Der "
        "Handler ruft VOR der eigentlichen Aktion memory.is_self_built_skill_confirmed(name) "
        "auf -- ist das False UND wurde confirmed nicht als True uebergeben, fuehre NICHTS aus, "
        "gib stattdessen eine Nachricht zurueck die genau erklaert was passieren wuerde und dass "
        "Ahmads ausdrueckliche Bestaetigung noetig ist, bevor es wirklich passiert. Erst wenn "
        "confirmed=True hereinkommt, fuehre die Aktion aus und rufe danach EINMALIG "
        "memory.mark_self_built_skill_confirmed(name) auf, danach laeuft es automatisch. Nutze "
        "GENAU diese beiden schon vorhandenen memory.py-Funktionen, erfinde keinen eigenen "
        "Bestaetigungs-Mechanismus. Rein lesende/harmlose Werkzeuge brauchen dieses Gate NICHT.\n\n"
        "WICHTIG (Ahmad, 2026-08-10, nach einem echten Vorfall — Jarvis erklaerte live einen "
        "laengst ersetzten alten Workflow, weil niemand claude_app_status.md aktualisiert hatte, "
        "als sich der echte Prozess geaendert hatte): falls dieses neue Werkzeug einen "
        "dokumentierten Geschaeftsprozess/Workflow beruehrt oder ersetzt, pruefe ob "
        "claude_app_status.md dazu etwas Veraltetes sagt, und aktualisiere es in DERSELBEN "
        "Aenderung.\n\n"
        "WICHTIG (2026-08-11, nach einem echten Vorfall): fuehre selbst KEINE Git-Befehle aus "
        "(kein git add/commit/push) -- das Commiten+Pushen uebernimmt automatisch der Prozess, "
        "der dich aufgerufen hat, direkt NACH dieser Aufgabe. Ein eigener Commit aendert den "
        "Besitzer von .git auf diesen eingeschraenkten User, wodurch der aufrufende Prozess "
        "(laeuft als root) 'git status' danach nicht mehr ausfuehren kann ('detected dubious "
        "ownership'). Bearbeite nur Dateien, committe nichts selbst.\n\n"
        "Pruefe am Ende mit 'python3 -m py_compile server.py' dass alles syntaktisch sauber ist. "
        "Fasse in 2-3 Saetzen auf Deutsch zusammen was du gebaut oder herausgefunden hast."
    )
    result, commit_hash = await claude_code_tool.run_claude_code_with_commit(task, timeout=600)
    _log(f"Skill-Growth Ergebnis: {result[:300]}")
    memory.add_skill_growth_entry(gap_or_idea, result, commit_hash)

    if commit_hash:
        memory.increment_skill_builds_today()
        if IS_SERVER:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "restart", "jarvis-server",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
                _log(f"Skill-Growth: jarvis-server neu gestartet (neues Werkzeug aus Commit {commit_hash}).")
            except Exception as e:
                _log(f"Skill-Growth: Neustart nach neuem Werkzeug fehlgeschlagen: {_exc(e)}")


async def _determine_virality_factor(client: anthropic.AsyncAnthropic, account: str, row: dict) -> dict:
    """One Claude call, informed by the ALREADY-documented business patterns
    (debate-hook videos outperform, etc.) — never a fresh guess, always
    grounded in what's already known to work for this business.

    Output MUST be English — this text goes straight into Jerome's WhatsApp
    message and he doesn't speak German (documented in the workbook itself).
    The business-knowledge INPUT can stay German, that's just reasoning
    material for Claude, never shown to Jerome directly."""
    knowledge = _get_luna_vale_knowledge()
    prompt = (
        f"Business knowledge (German, for your context only):\n{knowledge}\n\n"
        f"A video from @{account} has this status in the Winner Tracking sheet: {json.dumps(row, ensure_ascii=False)}.\n"
        "Based on the documented business patterns (e.g. which hook types are proven to perform "
        "better): identify in ONE sentence WHAT is likely responsible for this video's success "
        "(virality_factor), and in ONE sentence a CONCRETE, clear Trial Reel instruction for the "
        "editor (next_step) — exactly ONE variable should change per Trial Reel. "
        "IMPORTANT: answer in ENGLISH — the editor (Jerome) does not speak German. "
        'Respond ONLY as JSON: {"virality_factor": "...", "next_step": "..."}'
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"virality_factor": "Could not be determined clearly", "next_step": "Manual review needed"}


async def trial_reel_pass(config: dict):
    """Scan Winner Tracking (only Luna Vale's own accounts — Trial Reels
    are about Ahmad's content, not competitors') for videos the sheet
    already flagged KEEP or Outlier, and that haven't been sent to Jerome
    yet. For each: reason about why it worked, log it, message Jerome."""
    accounts = config.get("luna_vale_accounts", [])
    if not accounts:
        return

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    candidates_found = 0
    delivered = 0
    try:
        for account in accounts:
            rows = await sheets_tools.read_winner_tracking(account=account, limit=20)
            for row in rows:
                if row.get("error") or not row.get("video_link"):
                    continue
                decision = str(row.get("decision") or "").upper()
                outlier = str(row.get("outlier") or "").upper()
                us_audience = _parse_de_pct(row.get("us_audience_pct"))
                # Ahmad (2026-08-06): don't wait for a full KEEP — a CAUTION
                # row with strong US audience (his example: >25%) is still
                # worth a trial-reel test, not just confirmed KEEP/Outlier winners.
                eligible = decision == "KEEP" or outlier == "YES" or (us_audience is not None and us_audience >= 0.25)
                if not eligible:
                    continue
                if jerome_comm.already_notified(row["video_link"]):
                    continue

                candidates_found += 1
                verdict = await _determine_virality_factor(client, account, row)
                result = await jerome_comm.handle_trial_reel_candidate(
                    account, row["video_link"], verdict["virality_factor"], verdict["next_step"],
                    notify_ahmad_fn=lambda text: _alert(config, text),
                )
                _log(f"Trial-Reel-Kandidat @{account}: {result}")
                # handle_trial_reel_candidate only marks a video notified on
                # an actual successful send — reuse that as the truth signal
                # instead of assuming success just because nothing raised.
                if jerome_comm.already_notified(row["video_link"]):
                    delivered += 1
    finally:
        await client.close()

    _log(f"Trial-Reel-Scan fertig: {candidates_found} Kandidat(en) gefunden, {delivered} tatsaechlich an Jerome zugestellt.")


TRIAL_WAVE_NUDGE_HOURS = 8  # don't ask before Jerome realistically had time to shoot+post it


async def trial_wave_nudge_pass(config: dict):
    """Ahmad's own correction (2026-08-06): Jarvis can't reliably SEE a
    posted trial reel itself — it's shown mostly to non-followers by design,
    not something scraping the account would reliably catch. So this never
    tries to detect anything; it just asks a plain, natural check-in
    question through the EXISTING pending-questions mechanism, surfaced once
    at Ahmad's next 'lets go' — the same channel he already reads daily.

    Elapsed time is read from jerome_comm's OWN precise creation-timestamp
    file, not the sheet's Date column — that column has no time component,
    so a wave created at 3pm looked exactly like one from midnight and got
    flagged as hours-old immediately (caught live, 2026-08-06)."""
    accounts = config.get("luna_vale_accounts", [])
    if not accounts:
        return

    now = time.time()
    for account in accounts:
        open_waves = await sheets_tools.read_open_trial_waves(account)
        if not open_waves:
            continue

        created_ats = [
            ts for w in open_waves
            if (ts := jerome_comm.get_wave_created_at(account, w.get("wave"))) is not None
        ]
        # Unknown creation time (e.g. a wave that predates this tracking) ->
        # fall back to asking rather than silently never asking again.
        oldest = min(created_ats) if created_ats else 0
        if now - oldest < TRIAL_WAVE_NUDGE_HOURS * 3600:
            continue

        # Dedup against the last ~20h regardless of open/asked status — a
        # question flips to 'asked' the instant it's included in an
        # activate prompt, not once Ahmad actually heard it, so checking
        # only open questions let the same nudge get silently re-queued
        # minutes later (caught live, 2026-08-06).
        if memory.has_recent_question_about(f"@{account}", "Trial Reel", hours=20):
            continue

        question = (
            f"Hast du (oder Jerome) gestern einen Trial Reel fuer @{account} gepostet? "
            f"Wenn ja, schick mir Link + Insights-Screenshot in unseren Chat, dann trage ich "
            f"das Ergebnis ein."
        )
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            urgency = await _classify_question_urgency(client, question)
        finally:
            await client.close()
        memory.add_pending_question(question, urgency=urgency)
        _log(f"Trial-Reel-Nachfrage fuer @{account} eingereiht (urgency={urgency}).")


GMAIL_PROCESSED_PATH = os.path.join(os.path.dirname(__file__), "memory", "gmail_processed_messages.json")


def _load_processed_gmail_ids() -> set:
    if not os.path.exists(GMAIL_PROCESSED_PATH):
        return set()
    try:
        with open(GMAIL_PROCESSED_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_gmail_processed(message_id: str):
    done = _load_processed_gmail_ids()
    done.add(message_id)
    os.makedirs(os.path.dirname(GMAIL_PROCESSED_PATH), exist_ok=True)
    with open(GMAIL_PROCESSED_PATH, "w") as f:
        json.dump(sorted(done), f)


async def gmail_reply_pass(config: dict):
    """Klassifiziert neue Postfach-Mails und legt fuer Routine-Anfragen
    einen Antwort-ENTWURF an (gmail_tools.classify_and_draft_reply) —
    sendet NIEMALS selbst, Ahmads bewusste Entscheidung (2026-08-08).
    Verarbeitet jede Mail nur einmal (memory/gmail_processed_messages.json),
    damit dieselbe Newsletter-Mail nicht bei jedem Takt neu eingestuft wird.

    trust_level (Tier 3, Punkt 13) kommt aus config.json's optionalem
    Schluessel 'gmail_trust_level', Default 'conservative' (heutiges
    Verhalten, unveraendert). 'confident' aendert NUR eins: ignore-Mails
    werden automatisch archiviert statt liegenzubleiben — kein Einfluss
    auf Entwuerfe/Senden, das bleibt bei jeder Stufe gleich.

    Rechnungs-Erkennung (Tier 3, Punkt 15, 2026-08-08, Ahmads ausdruecklicher
    Wunsch): erkennt classify_and_draft_reply eine Rechnung, wird sie SOFORT
    in 'Variable Kosten' des Finanz-Sheets eingetragen und Ahmad per _alert
    informiert — erst eintragen, dann informieren, keine Rueckfrage vorher
    (Ahmads eigene Beschreibung der gewuenschten Reihenfolge). Vertretbar
    weil es nur die interne Tracking-Liste betrifft, nichts geht an Dritte
    raus, jederzeit von Ahmad korrigierbar."""
    processed = _load_processed_gmail_ids()
    try:
        recent = await gmail_tools.list_recent_emails()
    except Exception as e:
        _log(f"FEHLER beim Gmail-Abruf: {_exc(e)}")
        return

    new_emails = [e for e in recent if e["id"] not in processed]
    if not new_emails:
        return

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])

    async def _notify_ahmad(text: str):
        # speak_live=False (Ahmad, 2026-08-11): routine E-Mail-Hinweise sind
        # nicht so dringend wie z.B. ein Follower-Einbruch -- WhatsApp/Push
        # kommen weiterhin sofort an, nur der Sprach-Interrupt beim naechsten
        # Verbinden entfaellt (wirkte kontextlos/"sinnlos").
        await _alert(config, text, speak_live=False)

    trust_level = config.get("gmail_trust_level", "conservative")

    try:
        for e in new_emails:
            try:
                detail = await gmail_tools.fetch_email_detail(e["id"])
                result = await gmail_tools.classify_and_draft_reply(
                    client, detail, notify_ahmad_fn=_notify_ahmad, trust_level=trust_level
                )
                _log(f"Gmail [{result['category']}]: '{detail['subject']}' von {detail['from']} "
                     f"(draft_id={result['draft_id']}, archiviert={result.get('archived', False)}).")

                invoice = result.get("invoice")
                if invoice:
                    try:
                        betrag = float(invoice["amount"])
                        waehrung = invoice["currency"]
                        if waehrung.upper() == "EUR":
                            betrag_eur = betrag
                        else:
                            rate = await finance_tracker.fetch_usd_eur_rate()
                            betrag_eur = round(betrag * rate, 2)
                        await finance_tracker.append_variable_cost(
                            datum=invoice["date"], beschreibung=invoice["vendor"], kategorie=invoice["kategorie"],
                            betrag_eur=betrag_eur, waehrung=waehrung, betrag_original=betrag,
                            notiz=f"Automatisch aus E-Mail erkannt (Betreff: {detail['subject']}).",
                        )
                        await _alert(
                            config,
                            f"Jarvis Update (Rechnung erkannt): {invoice['vendor']}, {betrag_eur:.2f} €, "
                            f"{invoice['date']} — in Variable Kosten eingetragen.",
                        )
                        _log(f"Rechnung erkannt und eingetragen: {invoice['vendor']}, {betrag_eur:.2f} €.")
                    except Exception as ex:
                        _log(f"FEHLER beim Eintragen der erkannten Rechnung ({detail.get('subject', '?')}): {_exc(ex)}")
            except Exception as ex:
                _log(f"FEHLER bei E-Mail-Klassifikation ({e.get('subject', '?')}): {_exc(ex)}")
            finally:
                _mark_gmail_processed(e["id"])
    finally:
        await client.close()


async def goal_progress_pass(config: dict):
    """Fuer jedes faellige Ziel (goal_tracker.get_due_goals, gesteuert vom
    jeweils eigenen check_in_days) eine dringlichkeits-eingestufte Nachfrage
    anlegen (_classify_question_urgency, Tier 1 Punkt 5) — 'hakt proaktiv
    nach, wenn etwas stockt', Ahmads eigene Beschreibung von Tier 1 Punkt 8.
    Aendert nichts an den Zielen selbst — goal_tracker.update_goal() ist der
    einzige Weg last_checked zurueckzusetzen, ein Ahmad-Update ist der
    einzige Weg die Nachfrage zu stoppen. Das Dedup-Fenster orientiert sich
    bewusst am EIGENEN check_in_days des Ziels, nicht am 6h-Pass-Takt oben
    (GOAL_CHECK_INTERVAL_SECONDS steuert nur wie oft der Pass ueberhaupt
    NACHSCHAUT, nicht wie oft er bei ausbleibender Antwort erneut nervt) —
    sonst wuerde ein seit Tagen stilles Ziel alle 6h eine neue Frage
    bekommen statt im eigenen Rhythmus des Ziels."""
    due = goal_tracker.get_due_goals()
    if not due:
        return

    for goal in due:
        description = goal["description"]
        dedup_hours = goal.get("check_in_days", 3) * 24
        if memory.has_recent_question_about(description, hours=dedup_hours):
            continue

        question = (
            f"Kurzer Check-in zu deinem Ziel \"{description}\": gibt es was Neues, "
            f"oder haengt es gerade fest?"
        )
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            urgency = await _classify_question_urgency(client, question)
        finally:
            await client.close()
        memory.add_pending_question(question, urgency=urgency)
        _log(f"Ziel-Check-in eingereiht: '{description}' (urgency={urgency}).")


async def _consolidate_category_classify(client, category: str, entries: list) -> dict:
    """Tier 2, Punkt 11 ('Dreaming') — gleiches Brace-Slicing-JSON-Parse-
    Muster wie _classify_question_urgency oben. Bewusst konservativ: der
    sichere Fallback ist IMMER 'nichts tun' (anders als bei urgency, wo
    'medium' ein akzeptabler Mittelweg ist) — ein echter Fakt darf durch
    diesen Pass niemals verloren gehen, siehe memory.consolidate_category()s
    Archivierungs-Garantie."""
    numbered = "\n".join(f"{i}: {e['text']}" for i, e in enumerate(entries))
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": (
                f"Fakten aus Ahmads Langzeitgedaechtnis, Kategorie '{category}', jeweils nummeriert:\n\n"
                f"{numbered}\n\n"
                "Finde NUR Gruppen von MINDESTENS ZWEI Eintraegen, die WIRKLICH DIESELBE Aussage "
                "machen (nur anders formuliert) — NICHT nur thematisch verwandte oder sich "
                "ergaenzende, aber inhaltlich EIGENSTAENDIGE Fakten. Ein Eintrag alleine ist NIEMALS "
                "ein Merge-Kandidat, auch nicht um ihn nur umzuformulieren oder Tippfehler zu "
                "korrigieren — merge_indices braucht immer mindestens zwei Zahlen. Der "
                "zusammengefuehrte keep_text MUSS wirklich JEDES inhaltliche Detail aus JEDEM "
                "der zusammengefuehrten Originale enthalten, nichts darf beim Zusammenfassen "
                "verloren gehen. Finde zusaetzlich einzelne Fakten die durch eine ANDERE, "
                "eindeutig widersprechende spaetere Aussage ueberholt sind, zum Archivieren. "
                "WICHTIG: keep_text darf AUSSCHLIESSLICH Informationen aus GENAU den in "
                "merge_indices gelisteten Eintraegen enthalten — auch wenn andere Eintraege "
                "thematisch naheliegen oder direkt daneben stehen, duerfen deren Inhalte NIEMALS "
                "mit einfliessen, wenn ihr Index nicht explizit in derselben merge_indices-Liste "
                "steht. Im Zweifel bei alldem: NICHTS tun, lieber zu wenig als zu viel — ein "
                "echter, noch gueltiger Fakt oder eine einzelne Nuance darin darf NIEMALS "
                "verloren gehen, faelschlich mit einem eigenstaendigen anderen Fakt vermischt, "
                "oder um nicht deklarierte Zusatzinformationen ergaenzt werden.\n\n"
                'Antworte NUR als JSON: {"merges": [{"keep_text": "...", "merge_indices": [int,int,...]}], '
                '"archive_indices": [int,...]}'
            )}],
        )
        raw = response.content[0].text.strip()
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
        merges = [m for m in (data.get("merges", []) or []) if len(m.get("merge_indices", [])) >= 2]
        return {
            "merges": merges,
            "archive_indices": data.get("archive_indices", []) or [],
        }
    except Exception as e:
        _log(f"Konsolidierungs-Klassifikation fehlgeschlagen fuer '{category}' (Fallback: nichts tun): {e}")
        return {"merges": [], "archive_indices": []}


async def memory_consolidation_pass(config: dict):
    """Tier 2, Punkt 11 ('Dreaming') — Ahmads eigene Beschreibung: das
    Muster des lokalen Claude-Code-memory-Skills (Duplikate mergen,
    Veraltetes archivieren) auf Jarvis' eigenes Gedaechtnis angewendet.
    Nur Kategorien mit genug Eintraegen pruefen, sonst gibt es nichts zu
    konsolidieren. Aendert conversation_log.jsonl unabhaengig davon rein
    alters-basiert (kein LLM-Urteil noetig dafuer)."""
    by_category = {}
    for e in memory.get_all_profile_entries():
        by_category.setdefault(e["category"], []).append(e)

    for category, entries in by_category.items():
        if len(entries) < 3:
            continue
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            decision = await _consolidate_category_classify(client, category, entries)
        finally:
            await client.close()
        if decision["merges"] or decision["archive_indices"]:
            result = memory.consolidate_category(category, decision["merges"], decision["archive_indices"])
            _log(f"Gedaechtnis-Konsolidierung '{category}': {result}")

    log_result = memory.archive_old_conversation_log(days=30)
    _log(f"Gespraechsverlauf-Archivierung: {log_result}")


async def calendar_conflict_pass(config: dict):
    """Erkennt echte Zeitueberschneidungen zwischen zwei zeitgebundenen
    Terminen in den naechsten 7 Tagen und legt bei Fund eine dringlichkeits-
    eingestufte Frage an Ahmad an (siehe _classify_question_urgency oben,
    Tier 1 Punkt 5). Aendert am Kalender selbst NICHTS — das ist Ahmads
    bewusste Entscheidung (2026-08-08), nicht eine technische Grenze.
    Ganztaegige Termine (Geburtstage etc.) werden bewusst ausgeschlossen,
    eine Ueberschneidung mit einem echten Meeting ist da kein echter Konflikt."""
    try:
        events = await calendar_tools.list_upcoming_events_structured(days=7)
    except Exception as e:
        _log(f"FEHLER beim Kalender-Konfliktcheck (Abruf): {_exc(e)}")
        return

    def _parse(dt_str: str):
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

    timed = []
    for e in events:
        if e["all_day"] or not e["start"] or not e["end"]:
            continue
        try:
            timed.append((e, _parse(e["start"]), _parse(e["end"])))
        except ValueError:
            continue
    timed.sort(key=lambda x: x[1])

    for i in range(len(timed) - 1):
        event_a, start_a, end_a = timed[i]
        event_b, start_b, end_b = timed[i + 1]
        if start_b >= end_a:
            continue  # keine Ueberschneidung, nur benachbart

        title_a, title_b = event_a["summary"], event_b["summary"]
        if memory.has_recent_question_about(title_a, title_b, hours=CALENDAR_CHECK_INTERVAL_SECONDS / 3600):
            continue

        question = (
            f"Terminkonflikt: \"{title_a}\" und \"{title_b}\" ueberschneiden sich "
            f"({start_b.strftime('%d.%m. %H:%M')} bis {end_a.strftime('%H:%M')} Uhr). "
            f"Welchen soll ich verschieben oder absagen?"
        )
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
        try:
            urgency = await _classify_question_urgency(client, question)
        finally:
            await client.close()
        memory.add_pending_question(question, urgency=urgency)
        _log(f"Terminkonflikt gefunden: '{title_a}' / '{title_b}' (urgency={urgency}).")


MEETING_REMINDERS_SENT_PATH = os.path.join(os.path.dirname(__file__), "memory", "meeting_reminders_sent.json")


def _load_meeting_reminders_sent() -> set:
    if not os.path.exists(MEETING_REMINDERS_SENT_PATH):
        return set()
    try:
        with open(MEETING_REMINDERS_SENT_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_meeting_reminded(event_id: str):
    done = _load_meeting_reminders_sent()
    done.add(event_id)
    os.makedirs(os.path.dirname(MEETING_REMINDERS_SENT_PATH), exist_ok=True)
    with open(MEETING_REMINDERS_SENT_PATH, "w") as f:
        json.dump(sorted(done), f)


async def _build_meeting_context(event: dict) -> str:
    """Sammelt Kontext aus bereits vorhandenen Faehigkeiten statt einer neuen
    Notizen-Funktion — semantic_memory (Tier 2 Punkt 9) und knowledge_graph
    (Tier 2 Punkt 10) SIND Jarvis' 'Notizen'. Best-effort: ein Fehler in
    einer Quelle darf die anderen nicht verhindern."""
    parts = []
    if event.get("description"):
        parts.append(f"Beschreibung: {event['description'][:300]}")
    if event.get("location"):
        parts.append(f"Ort: {event['location']}")

    try:
        longterm = semantic_memory.search_longterm_memory(event["summary"], max_chars=800)
        if longterm:
            parts.append(f"Bekannte Fakten:\n{longterm}")
    except Exception as e:
        _log(f"Meeting-Kontext (semantic_memory) fehlgeschlagen: {e}")

    try:
        graph = knowledge_graph.query_entity(event["summary"])
        if graph and "Nichts zu" not in graph:
            parts.append(f"Wissensgraph:\n{graph}")
    except Exception as e:
        _log(f"Meeting-Kontext (knowledge_graph) fehlgeschlagen: {e}")

    try:
        query = f"from:{event['attendees'][0]} OR to:{event['attendees'][0]}" if event.get("attendees") else event["summary"]
        mails = await gmail_tools.search_emails(query, max_results=3)
        if mails:
            mail_lines = "\n".join(f"- {m['from']}: {m['subject']}" for m in mails)
            parts.append(f"Kuerzliche Mail dazu:\n{mail_lines}")
    except Exception as e:
        _log(f"Meeting-Kontext (Gmail-Suche) fehlgeschlagen: {e}")

    return "\n\n".join(parts)


async def meeting_reminder_pass(config: dict):
    """Vorbereitungs-Reminder kurz vor einem Termin (Tier 3, Punkt 14,
    2026-08-08) — 'in 20 Min Meeting X, hier der Kontext dazu aus Mail/
    Notizen', Ahmads eigene Beschreibung. Zeitkritisch anders als
    calendar_conflict_pass (add_pending_question wartet bis zum naechsten
    Gespraech) — nutzt deshalb _alert() fuer sofortige Zustellung, egal ob
    Ahmad gerade mit Jarvis spricht oder nicht. Aendert nichts am Kalender."""
    try:
        events = await calendar_tools.list_upcoming_events_structured(days=1)
    except Exception as e:
        _log(f"FEHLER beim Meeting-Reminder-Check (Abruf): {_exc(e)}")
        return

    already_sent = _load_meeting_reminders_sent()
    now = datetime.now().astimezone()
    lead = timedelta(minutes=MEETING_REMINDER_LEAD_MINUTES)

    for e in events:
        if e["all_day"] or not e["start"] or not e["id"] or e["id"] in already_sent:
            continue
        try:
            start = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (now <= start <= now + lead):
            continue

        context = await _build_meeting_context(e)
        minutes_left = max(1, round((start - now).total_seconds() / 60))
        if context:
            client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=200,
                    messages=[{"role": "user", "content": (
                        f"In {minutes_left} Minuten hat Ahmad einen Termin: \"{e['summary']}\".\n\n"
                        f"Gesammelter Kontext dazu:\n{context}\n\n"
                        "Fasse das in 1-2 kurzen, natuerlichen deutschen Saetzen als Reminder-"
                        "Nachricht zusammen, die Ahmad kurz vor dem Termin hilft sich zu orientieren. "
                        "Nur was wirklich relevant ist, keine Floskeln, keine Ueberschrift."
                    )}],
                )
                text = f"In {minutes_left} Minuten: {e['summary']}. " + response.content[0].text.strip()
            except Exception as ex:
                _log(f"Meeting-Reminder-Zusammenfassung fehlgeschlagen: {_exc(ex)}")
                text = f"In {minutes_left} Minuten: {e['summary']}."
            finally:
                await client.close()
        else:
            text = f"In {minutes_left} Minuten: {e['summary']}."

        await _alert(config, text)
        _mark_meeting_reminded(e["id"])
        _log(f"Meeting-Reminder gesendet: '{e['summary']}' (in {minutes_left} Min).")


async def screen_awareness_pass(config: dict):
    """Roadmap Punkt 19, 2026-08-09 — periodische, niedrigfrequente
    Bildschirm-Beobachtung. Rein passiv: kein _alert(), landet nur im
    eigenen screen_awareness_log (memory.py), NICHT im kuratierten
    Langzeitgedaechtnis. Screenshot-Bild existiert nie ausserhalb dieser
    einen Vision-Analyse (Ahmads ausdrueckliche Wahl), nur der Text bleibt."""
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        result = await screen_capture.describe_screen_for_awareness(client, [])
    except Exception as e:
        _log(f"FEHLER bei Screen-Awareness: {_exc(e)}")
        return
    finally:
        await client.close()

    if result.get("skipped"):
        _log(f"Screen-Awareness uebersprungen: {result.get('reason', '')}")
        return

    text = result.get("text", "").strip()
    if text:
        memory.add_screen_awareness_entry(text)
        _log(f"Screen-Awareness erfasst: {text[:100]}")


async def finance_sync_pass(config: dict):
    """Haelt das Finanz-Sheet (finance_tracker.py, Tier 3 Punkt 15,
    2026-08-08) aktuell, ohne dass Ahmad etwas tun muss: Wechselkurs,
    Fanplace-Payout-Historie, und ein zurueckhaltender Trend-Check.
    Rechnungs-Erkennung aus E-Mails laeuft separat in gmail_reply_pass
    (eigener, schnellerer Takt, dort passiert es im selben Abwasch wie die
    ohnehin schon laufende Klassifikation)."""
    try:
        rate = await finance_tracker.fetch_usd_eur_rate()
        await finance_tracker.update_fx_rate(rate)
        _log(f"Finanz-Sync: Wechselkurs aktualisiert ({rate}).")
    except Exception as e:
        _log(f"FEHLER beim Finanz-Sync (Wechselkurs): {_exc(e)}")
        rate = 0.86  # grober Fallback nur fuer den Payout-Sync unten, falls die FX-API selbst ausfiel

    try:
        new_payouts = await finance_tracker.sync_fanplace_payouts(rate)
        if new_payouts:
            summary = "; ".join(f"{p['period']}: {p['net_eur']:.2f} €" for p in new_payouts)
            await _alert(config, f"Jarvis Update (Fanplace): {len(new_payouts)} neue Auszahlung(en) eingetragen — {summary}")
            _log(f"Finanz-Sync: {len(new_payouts)} neue Fanplace-Payouts eingetragen.")
    except Exception as e:
        _log(f"FEHLER beim Finanz-Sync (Fanplace-Payouts): {_exc(e)}")

    try:
        summary = await finance_tracker.get_recent_month_summary()
        if summary and summary.get("previous_total_cost"):
            cost_change = (summary["current_total_cost"] - summary["previous_total_cost"]) / summary["previous_total_cost"]
            netto_now = summary.get("current_netto")
            netto_prev = summary.get("previous_netto")
            if cost_change > 0.20:
                await _alert(config, f"Jarvis Update (Finanzen): Gesamtkosten in {summary['current_month']} liegen {cost_change:.0%} über {summary['previous_month']} — lohnt sich ein Blick ins Sheet.")
            elif netto_now is not None and netto_prev is not None and netto_now < 0 and netto_prev < 0:
                await _alert(config, f"Jarvis Update (Finanzen): {summary['previous_month']} UND {summary['current_month']} beide mit negativem Netto — zwei Monate in Folge, lohnt sich ein genauerer Blick.")
    except Exception as e:
        _log(f"FEHLER beim Finanz-Sync (Trend-Check): {_exc(e)}")


DEEP_ANALYSIS_INTERVAL_SECONDS = 7 * 24 * 60 * 60  # weekly — multi-frame Vision is meaningfully
                                                     # more expensive per video than the single-frame checks
DEEP_ANALYSIS_OWN_COUNT = 3
DEEP_ANALYSIS_COMPETITOR_COUNT = 3
DEEP_ANALYSIS_DONE_PATH = os.path.join(os.path.dirname(__file__), "memory", "deep_analysis_done.json")


def _load_deep_analysis_done() -> set:
    if not os.path.exists(DEEP_ANALYSIS_DONE_PATH):
        return set()
    try:
        with open(DEEP_ANALYSIS_DONE_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_deep_analysis_done(url: str):
    done = _load_deep_analysis_done()
    done.add(url)
    os.makedirs(os.path.dirname(DEEP_ANALYSIS_DONE_PATH), exist_ok=True)
    with open(DEEP_ANALYSIS_DONE_PATH, "w") as f:
        json.dump(sorted(done), f)


def _pick_top_videos_from_analysis(handles: set, top_n: int) -> list:
    """Ranks videos already seen by video_analysis_pass (from video_analysis.jsonl)
    by likes (the most reliably visible stat — views are frequently hidden by
    Instagram's layout) and returns the top_n NOT-yet-deep-analyzed ones for the
    given handles. Deliberately reuses data already collected instead of
    re-scraping — the weekly pass only adds the multi-frame Vision step on top."""
    if not os.path.exists(instagram_tools.VIDEO_ANALYSIS_PATH):
        return []
    done = _load_deep_analysis_done()
    best_by_url = {}
    with open(instagram_tools.VIDEO_ANALYSIS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle, url = entry.get("handle"), entry.get("url")
            if not handle or not url or handle not in handles or url in done or "raw" not in entry:
                continue
            stats = _parse_video_raw_stats(entry["raw"])
            if stats.get("likes") is None:
                continue
            best_by_url[url] = {"handle": handle, "url": url, "likes": stats["likes"]}
    ranked = sorted(best_by_url.values(), key=lambda v: v["likes"], reverse=True)
    return ranked[:top_n]


async def _synthesize_pattern_findings(
    client: anthropic.AsyncAnthropic, own_analyses: list, competitor_analyses: list
) -> str:
    """One follow-up Claude call over ALL this week's structural analyses
    together (own + competitor) — the point isn't any single video's
    structure, it's what's CONSISTENT across several successful videos, and
    where Luna Vale's own structure differs from what's currently working
    for competitors. Output in German (goes into the shared knowledge base,
    same language as the rest of it)."""
    def _fmt(analyses, label):
        if not analyses:
            return f"{label}: keine Daten diese Woche."
        lines = [
            f"- @{a['handle']} ({a['url']}): hook={a.get('hook_timing', '?')} | "
            f"transition={a.get('transition', '?')} | pacing={a.get('pacing', '?')} | "
            f"summary={a.get('structure_summary', '?')}"
            for a in analyses
        ]
        return f"{label}:\n" + "\n".join(lines)

    prompt = (
        "Hier ist die strukturelle Video-Analyse (Hook-Timing, Uebergaenge, Pacing) der "
        "erfolgreichsten Videos dieser Woche — sowohl von Luna Vales eigenen Accounts als "
        "auch von beobachteter Konkurrenz:\n\n"
        f"{_fmt(own_analyses, 'EIGENE Videos')}\n\n"
        f"{_fmt(competitor_analyses, 'KONKURRENZ Videos')}\n\n"
        "Identifiziere: 1) wiederkehrende STRUKTURELLE Muster (nicht Thema/Content, sondern "
        "Aufbau/Timing/Schnitt) ueber mehrere erfolgreiche Videos hinweg, 2) falls erkennbar: "
        "worin sich Luna Vales eigene Struktur von dem unterscheidet, was bei der Konkurrenz "
        "gerade gut funktioniert. Max. 4-5 knappe, konkret umsetzbare Saetze auf Deutsch. Wenn "
        "zu wenig Daten fuer ein klares Muster da sind, sag das ehrlich statt zu raten."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


async def deep_pattern_analysis_pass(config: dict):
    """Weekly, more expensive pass Ahmad approved ('yes klingt mega lets go',
    2026-08-06): multi-frame structural Vision analysis (hook timing,
    transition point, pacing — not just a single static frame) of this
    week's best-performing videos, BOTH Luna Vale's own accounts and
    watched competitors, synthesized into cross-video pattern findings and
    fed into the shared knowledge base so Jerome-chat, trial-reel reasoning,
    and the daily content brief all benefit from it — not just a one-off
    report Ahmad has to go read somewhere."""
    own_handles = set(config.get("luna_vale_accounts", []))
    competitor_handles = set(config.get("competitor_accounts", []))
    if not own_handles and not competitor_handles:
        return

    own_candidates = _pick_top_videos_from_analysis(own_handles, DEEP_ANALYSIS_OWN_COUNT)
    competitor_candidates = _pick_top_videos_from_analysis(competitor_handles, DEEP_ANALYSIS_COMPETITOR_COUNT)

    if not own_candidates and not competitor_candidates:
        _log("Tiefen-Analyse: keine neuen Video-Kandidaten (noch keine Video-Analyse-Daten oder alles schon analysiert).")
        return

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    own_analyses, competitor_analyses = [], []
    try:
        for c in own_candidates:
            try:
                result = await instagram_tools.analyze_video_deep(c["url"], client)
                if "error" not in result:
                    result["handle"] = c["handle"]
                    own_analyses.append(result)
                    _mark_deep_analysis_done(c["url"])
                else:
                    _log(f"Tiefen-Analyse (eigen) @{c['handle']} {c['url']}: {result['error']}")
            except Exception as e:
                _log(f"FEHLER bei Tiefen-Analyse (eigen) @{c['handle']}: {_exc(e)}")
            await asyncio.sleep(2)

        for c in competitor_candidates:
            try:
                result = await instagram_tools.analyze_video_deep(c["url"], client)
                if "error" not in result:
                    result["handle"] = c["handle"]
                    competitor_analyses.append(result)
                    _mark_deep_analysis_done(c["url"])
                else:
                    _log(f"Tiefen-Analyse (Konkurrenz) @{c['handle']} {c['url']}: {result['error']}")
            except Exception as e:
                _log(f"FEHLER bei Tiefen-Analyse (Konkurrenz) @{c['handle']}: {_exc(e)}")
            await asyncio.sleep(2)

        if not own_analyses and not competitor_analyses:
            _log("Tiefen-Analyse: alle Kandidaten sind fehlgeschlagen, keine Muster-Synthese moeglich.")
            return

        findings = await _synthesize_pattern_findings(client, own_analyses, competitor_analyses)
    finally:
        await client.close()

    if not findings:
        return

    # remember() (NOT add_knowledge()) — deliberately: add_knowledge() writes to
    # a SEPARATE flat research file that _get_luna_vale_knowledge() never reads.
    # remember(category="business") lands in the categorized profile that
    # memory.get_category("business") reads, which is the ONLY path that
    # actually reaches Jerome-chat/trial-reel reasoning (caught live, 2026-08-07:
    # the first test run "succeeded" but get_category("business") stayed empty).
    memory.remember(
        f"Woechentliche Struktur-Analyse (eigene + Konkurrenz-Videos):\n{findings}", category="business"
    )
    _log(f"Tiefen-Analyse fertig: {len(own_analyses)} eigene, {len(competitor_analyses)} Konkurrenz-Videos analysiert.")
    await _alert(config, f"🔬 Jarvis Struktur-Analyse (woechentlich):\n\n{findings}")


INSIGHTS_INBOX_INTERVAL_SECONDS = 3 * 60  # Ahmad (2026-08-07): "muss zuegiger gehen" — no live
                                            # trigger needed anymore, so the only latency IS this interval.


async def insights_inbox_pass(config: dict):
    """Ahmad's replacement (2026-08-07) for the WhatsApp-screenshot Insights
    workflow, which broke repeatedly in production — reading numbers AND
    the post URL off a phone screenshot via Vision proved unreliable
    (WhatsApp's link-preview card often doesn't show a readable URL at all,
    and multiple sequences close together risked cross-attribution). A
    plain Sheet row ('Insights Eingang': Link | US Audience % | Reach |
    Views | Status) is typed text Ahmad enters directly off the same
    screenshot he was already looking at — nothing left to misread. Checked
    fully autonomously on its own fast interval; Ahmad never has to say
    'ich hab's geschickt' at all anymore."""
    await sheets_tools.ensure_insights_inbox_tab()
    pending = await sheets_tools.read_pending_insights_inbox()
    if not pending:
        return

    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        for item in pending:
            try:
                us_audience_pct = _parse_inbox_percent(item["us_audience_raw"])
                reach = _parse_count(str(item["reach_raw"])) if item["reach_raw"] else None
                views = _parse_count(str(item["views_raw"])) if item["views_raw"] else None
                result = await jerome_comm.handle_insight_data(
                    item["link"], us_audience_pct, reach, views, client, config
                )
                _log(f"Insights-Eingang Zeile {item['row']}: {result}")
                status = result[:200]
            except Exception as e:
                status = f"FEHLER: {_exc(e)}"
                _log(f"FEHLER bei Insights-Eingang Zeile {item['row']}: {_exc(e)}")
            await sheets_tools.mark_insights_inbox_row(item["row"], status)
    finally:
        await client.close()


LINK_FUNNEL_INTERVAL_SECONDS = 60 * 60  # Ahmad only adds a row ~weekly, hourly is plenty
LINK_FUNNEL_BENCHMARK = 0.29  # matches the sheet's own "Benchmark 29%" column
LINK_FUNNEL_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "memory", "link_funnel_snapshot.json")


def _load_link_funnel_snapshot() -> dict:
    if not os.path.exists(LINK_FUNNEL_SNAPSHOT_PATH):
        return {}
    try:
        with open(LINK_FUNNEL_SNAPSHOT_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_link_funnel_snapshot(snapshot: dict):
    os.makedirs(os.path.dirname(LINK_FUNNEL_SNAPSHOT_PATH), exist_ok=True)
    with open(LINK_FUNNEL_SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f)


def _last_completed_week_range() -> tuple:
    """Monday-Sunday date range for 'the week that just ended' — Ahmad's own
    cadence is entering Profile Visits every Sunday for that same week."""
    today = datetime.now()
    this_monday = today - timedelta(days=today.weekday())
    if today.weekday() == 6:  # today IS Sunday -> count the week ending today
        monday, sunday = this_monday, today
    else:
        sunday = this_monday - timedelta(days=1)
        monday = sunday - timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


async def link_funnel_pass(config: dict):
    """Ahmad enters Week/Account/Profile Visits himself every Sunday (2026-
    08-07) — Instagram's own Profile-Visits stat has no API Jarvis can read.
    Fills in everything computable/fetchable: Link Clicks for the just-
    completed week from SLT.bio (exact per-day data), New Subs from
    Fanplace's affiliate/links dashboard — the ONLY place Fanplace
    attributes subs to a specific account instead of one combined number,
    but only as a running CUMULATIVE total, so tracked via a persisted
    week-over-week snapshot diff (same principle as the existing Fanplace
    subscriber-drop alert) rather than driving Fanplace's own fragile
    date-range picker UI. The two ratio columns are pure math off the
    sheet's own 29% benchmark."""
    pending = await sheets_tools.read_pending_link_funnel()
    if not pending:
        return

    try:
        links = await fanplace.get_affiliate_links()
        fanplace_totals = fanplace.get_totals_by_account(links)
    except Exception as e:
        _log(f"FEHLER beim Lesen der Fanplace-Affiliate-Links: {_exc(e)}")
        fanplace_totals = {}

    snapshot = _load_link_funnel_snapshot()
    monday, sunday = _last_completed_week_range()

    for item in pending:
        account = item["account"].lstrip("@").strip().lower()
        try:
            profile_visits = float(str(item["profile_visits"]).replace(".", "").replace(",", "."))
        except ValueError:
            profile_visits = None

        link_clicks = None
        try:
            summary = await slt_bio_tools.get_summary(from_date=monday, to_date=sunday)
            if "error" not in summary:
                link_clicks = sum(
                    entry.get("count", 0) or 0 for entry in summary.get("summary", [])
                    if entry.get("page_slug") == account and entry.get("event_type") in ("link_clicked", "poplink_click")
                )
        except Exception as e:
            _log(f"FEHLER bei SLT.bio-Abfrage fuer Link Funnel Zeile {item['row']}: {_exc(e)}")

        new_subs = None
        current = fanplace_totals.get(account)
        if current is not None:
            previous = snapshot.get(account)
            if previous is not None:
                new_subs = max(0, current["subs"] - previous["subs"])
            snapshot[account] = {"clicks": current["clicks"], "subs": current["subs"], "as_of": sunday}

        values = {}
        if link_clicks is not None:
            values["link_clicks"] = link_clicks
        if profile_visits and link_clicks is not None and profile_visits > 0:
            click_rate = link_clicks / profile_visits
            values["click_rate_pct"] = f"{click_rate * 100:.1f}%"
            values["benchmark_diff"] = f"{(click_rate - LINK_FUNNEL_BENCHMARK) * 100:+.1f}%"
        if new_subs is not None:
            values["new_subs"] = new_subs
            if profile_visits and profile_visits > 0:
                values["conversion_pct"] = f"{(new_subs / profile_visits) * 100:.2f}%"

        if values:
            await sheets_tools.write_link_funnel_row(item["row"], values)
            _log(f"Link Funnel Zeile {item['row']} (@{account}, Woche {monday} bis {sunday}) ausgefuellt: {values}")
        else:
            _log(f"Link Funnel Zeile {item['row']} (@{account}): noch keine Daten verfuegbar (evtl. erste Woche, kein Fanplace-Vergleichswert).")

    _save_link_funnel_snapshot(snapshot)


TARGET_CREATOR_INTERVAL_SECONDS = 6 * 60 * 60  # multi-frame deep analysis is expensive — a few times/day is enough
TARGET_CREATOR_PER_PASS = 2  # bounded on purpose — never burn through the whole backlog in one tick


async def _analyze_one_target_creator(client, handle: str, business_knowledge: str) -> dict:
    """Gathers real recent-post stats + one multi-frame structural deep-
    analysis (same capability built for Luna Vale's own winners), then asks
    Claude for ONE holistic judgment: is this genuinely a 1:1 niche/style
    match worth tracking, or just loosely-related? Labeled-line output, not
    JSON — free-text notes/hooks from real accounts routinely contain
    quotes that break strict JSON (the exact bug fixed earlier today)."""
    result = await instagram_tools.analyze_recent_videos(handle, client, count=5)
    videos = [v for v in result.get("videos", []) if "raw" in v]
    if not videos:
        return {"match": False, "reason": "keine auswertbaren Videos gefunden"}

    stats_lines = []
    best_url, best_likes = None, -1
    for v in videos:
        stats = _parse_video_raw_stats(v["raw"])
        stats_lines.append(f"{v['url']}: {v['raw']}")
        likes = stats.get("likes")
        if likes is not None and likes > best_likes:
            best_url, best_likes = v["url"], likes

    deep = None
    if best_url:
        try:
            deep = await instagram_tools.analyze_video_deep(best_url, client)
        except Exception:
            deep = None
    deep_block = "(keine Tiefen-Analyse verfuegbar)"
    if deep and "error" not in deep:
        deep_block = (
            f"Hook: {deep.get('hook_timing', '?')} | Uebergang: {deep.get('transition', '?')} | "
            f"Pacing: {deep.get('pacing', '?')} | Struktur: {deep.get('structure_summary', '?')}"
        )

    prompt = (
        f"Luna Vales eigenes Business-Wissen (was bei UNS funktioniert):\n{business_knowledge}\n\n"
        f"Account @{handle} — letzte {len(videos)} Posts (Rohdaten views/likes/comments/audience):\n"
        + "\n".join(stats_lines) +
        f"\n\nStruktur-Analyse des staerksten Videos ({best_url}):\n{deep_block}\n\n"
        "Beurteile ehrlich: ist das WIRKLICH ein 1:1-Konkurrent — gleiche Nische UND sehr aehnlicher "
        "Content-Stil/Format zu Luna Vale, nicht nur grob verwandt? Antworte in GENAU diesem Format, "
        "eine Zeile pro Punkt, keine Anfuehrungszeichen, kein JSON:\n"
        "MATCH: yes oder no\n"
        "BODY_MATCH: yes oder no (aehnlicher Koerpertyp/Aesthetic wie Luna Vale, fuer realistische "
        "Nachbaubarkeit)\n"
        "TREND: rising oder falling (Views/Likes-Verlauf der letzten Posts)\n"
        "HOOK_QUALITY: Zahl 1-5\n"
        "COMMENT_VOLUME: Zahl 1-5\n"
        "OVERALL_RANKING: Zahl 1-5\n"
        "NOTES: ein bis zwei knappe Saetze auf Deutsch, warum (nicht) — konkret, kein Fuellsatz"
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    fields = {}
    for line in raw.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        fields[label.strip().upper()] = value.strip()

    return {
        "match": fields.get("MATCH", "").lower().startswith("y"),
        "body_match": fields.get("BODY_MATCH", ""),
        "trend": fields.get("TREND", ""),
        "hook_quality": fields.get("HOOK_QUALITY", ""),
        "comment_volume": fields.get("COMMENT_VOLUME", ""),
        "overall_ranking": fields.get("OVERALL_RANKING", ""),
        "notes": fields.get("NOTES", ""),
        "best_url": best_url,
    }


async def target_creator_analysis_pass(config: dict):
    """Ahmad (2026-08-07): auto-discovered accounts sat in Target Creator
    List with every analysis column empty forever. This actually judges each
    one with the multi-frame deep-analysis capability, fills in the real
    columns for genuine 1:1 matches, and stops tracking ones that turn out
    to only be loosely related instead of leaving them as unrated clutter.
    A confirmed strong match's best video also goes to Jerome directly as
    inspiration (his own ask, 2026-08-07) — not just tracked internally
    where he'd never see it."""
    pending = await sheets_tools.read_target_creator_pending()
    if not pending:
        return

    business_knowledge = _get_luna_vale_knowledge()
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        for item in pending[:TARGET_CREATOR_PER_PASS]:
            handle = item["handle"]
            try:
                verdict = await _analyze_one_target_creator(client, handle, business_knowledge)
            except Exception as e:
                _log(f"FEHLER bei Target-Creator-Analyse @{handle}: {_exc(e)}")
                continue

            if not verdict["match"]:
                instagram_tools.remove_competitor_account(handle)
                await sheets_tools.write_target_creator_row(item["row"], {
                    "notes": f"NICHT 1:1 passend ({time.strftime('%Y-%m-%d')}): {verdict.get('reason') or verdict.get('notes', '')} — aus aktiver Beobachtung entfernt.",
                    "overall_ranking": "0",
                })
                _log(f"Target Creator @{handle}: kein 1:1-Match, aus Beobachtung entfernt.")
                continue

            await sheets_tools.write_target_creator_row(item["row"], {
                "body_match": verdict["body_match"],
                "trend": verdict["trend"],
                "hook_quality": verdict["hook_quality"],
                "comment_volume": verdict["comment_volume"],
                "overall_ranking": verdict["overall_ranking"],
                "notes": verdict["notes"],
            })
            _log(f"Target Creator @{handle}: bestaetigter Match, Ranking {verdict['overall_ranking']}.")

            try:
                ranking = float(verdict["overall_ranking"])
            except (TypeError, ValueError):
                ranking = 0
            if ranking >= 4 and verdict.get("best_url"):
                sent = await jerome_comm.send_raw_message(
                    f"Found a strong reference from @{handle} — worth a look for inspiration: "
                    f"{verdict['best_url']}\n{verdict['notes']}"
                )
                _log(f"Inspiration an Jerome geschickt (@{handle}): {sent}")
    finally:
        await client.close()


async def jerome_reply_pass(config: dict):
    """Checks Jerome's WhatsApp chat for a new reply and, if there is one,
    actually TALKS BACK to him (Ahmad's explicit ask, 2026-08-06) — content/
    Instagram/marketing questions get a real, data-grounded answer straight
    away; anything only Ahmad can decide (payment/contract/personal/real
    business calls) gets a holding reply to Jerome plus a note to Ahmad.
    Routine exchanges stay silent to Ahmad by his own choice — he only wants
    to hear about genuine news or things he needs to decide."""
    if not config.get("jerome_contact", "").strip():
        return
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        reply = await jerome_comm.check_jerome_replies(client)
        if not reply:
            return
        await jerome_comm.handle_jerome_message(
            client, reply, config, notify_ahmad_fn=lambda text: _alert(config, text)
        )
    finally:
        await client.close()


CONTENT_BRIEF_RETRY_SECONDS = 60 * 60  # on failure, retry hourly — NOT every tick (each run is ~20+ Vision calls)


async def content_brief_pass(config: dict):
    """Once per calendar day, no earlier than CONTENT_BRIEF_HOUR — the daily
    'content research machine' run Ahmad asked for (2026-08-06): scans every
    Luna Vale account's own profile, known competitors, and fresh Instagram
    hashtag trends, filters for real brand fit, and sends Jerome ONE
    complete brief with real links. Always via content_strategy.py's atomic
    pipeline, never composed ad-hoc — that's exactly what broke live earlier
    the same day (see content_strategy.py's module docstring).

    Only marks the day 'done' on an actual successful send — a transient
    failure (caught live: Contacts.app not running yet on a cold boot) used
    to get marked done anyway, silently skipping Jerome's brief for the
    whole rest of the day even after the underlying issue was fixed."""
    if memory.has_content_brief_today():
        return
    if int(time.strftime("%H")) < CONTENT_BRIEF_HOUR:
        return
    if not config.get("luna_vale_accounts") or not config.get("jerome_contact", "").strip():
        return

    timers = _load_timers()
    if time.time() - timers.get("content_brief_attempt", 0) < CONTENT_BRIEF_RETRY_SECONDS:
        return
    _save_timer("content_brief_attempt", time.time())

    _log("Taeglicher Content-Brief startet...")
    client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])
    try:
        result = await content_strategy.send_daily_content_brief(client)
    except Exception as e:
        _log(f"FEHLER beim Content-Brief: {_exc(e)}")
        return
    finally:
        await client.close()

    if result.startswith("ERROR"):
        _log(f"Content-Brief fehlgeschlagen, naechster Versuch in ca. 1h: {result}")
        return

    memory.mark_content_brief_done()
    await _alert(config, f"Jarvis: heutiger Content-Brief an Jerome raus — {result}")
    _log(f"Content-Brief gesendet: {result}")


async def morning_briefing_pass(config: dict):
    """Roadmap Punkt 6 — Gegenstueck zu daily_summary_pass, aber morgens und
    komplett selbststaendig (kein Doppelklatschen noetig, das war bisher die
    einzige Quelle fuer ein 'Morgen-Briefing", ueber handle_activate). Once
    per calendar day, no earlier than MORNING_SUMMARY_HOUR."""
    if memory.has_morning_briefing_today():
        return
    if int(time.strftime("%H")) < MORNING_SUMMARY_HOUR:
        return

    open_qs = memory.get_open_questions()
    self_improve_note = memory.format_recent_self_improve_summary(hours=24)
    skill_growth_note = memory.format_recent_skill_growth_summary(hours=24)

    parts = ["Guten Morgen! Kurzer Ueberblick zum Start:"]
    if open_qs:
        parts.append(f"{len(open_qs)} offene Frage(n) warten, allen voran: {open_qs[0]['question']}")
    else:
        parts.append("Keine offenen Fragen gerade.")
    if self_improve_note:
        parts.append(self_improve_note)
    if skill_growth_note:
        parts.append(skill_growth_note)
    message = " ".join(parts)

    memory.mark_morning_briefing_done()
    await _alert(config, message)
    _log(f"Morgen-Briefing gesendet: {message[:200]}")


def _sd_notify(state: str):
    """Roadmap Punkt 21 — minimales sd_notify ohne Zusatzpaket (systemd-
    python/sdnotify sind auf dem Server nicht installiert, das Protokoll ist
    simpel genug fuer ein paar Zeilen): liest den vom systemd-Unit gesetzten
    NOTIFY_SOCKET-Pfad und schickt ein AF_UNIX-Datagram. Kein Effekt/Fehler
    wenn NOTIFY_SOCKET fehlt (z.B. lokal auf dem Mac ohne systemd, oder
    Type=notify noch nicht in der Unit gesetzt) — rein best effort."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(state.encode())
        sock.close()
    except OSError:
        pass


_BROWSER_RESET_PASSES = {
    "Instagram-Check", "Fanplace-Check", "Video-Analyse", "Account-Discovery", "Tiefen-Analyse",
}


async def _run_pass_safely(label: str, config: dict, coro, timeout: int = PASS_TIMEOUT_SECONDS):
    """Roadmap Punkt 21 — Ersatz fuer das nackte try/except um jeden Pass-
    Aufruf in main(): faengt zusaetzlich einen ECHTEN Haenger ab (kein
    raised Exception, die Coroutine wird einfach nie fertig), den das
    urspruengliche try/except strukturell nicht sehen konnte. Gibt den
    Rueckgabewert des Passes zurueck (fuer Passes wie video_analysis_pass,
    die einen Zustand zurueckreichen), oder None bei Fehler/Timeout.

    Schickt zusaetzlich NACH JEDEM Pass einen systemd-Watchdog-Heartbeat
    (2026-08-11, echter Vorfall): main() schickte den Heartbeat bisher nur
    EINMAL pro komplettem Tick, ganz am Ende. self_improve_pass/
    skill_growth_pass duerfen aber bis zu 650s dauern (timeout=650), laenger
    als WatchdogSec (300s) -- der systemd-Watchdog hat deswegen wiederholt
    den KOMPLETTEN gesunden Prozess (samt laufendem Claude-Code-Unterprozess,
    mitten in einer Datei-Aenderung) mit SIGABRT gekillt, obwohl gar kein
    echter Haenger vorlag. Jetzt hier statt nur am Tick-Ende, damit ein
    einzelner langsamer Pass den naechsten nicht verpasst."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        _log(f"WATCHDOG: '{label}' hat nach {timeout}s nicht reagiert, abgebrochen.")
        if label in _BROWSER_RESET_PASSES:
            try:
                instagram_tools.reset_browser()
            except Exception:
                pass
            try:
                fanplace.reset_browser()
            except Exception:
                pass
            _log(f"WATCHDOG: Browser-Sitzungen wegen '{label}' zurueckgesetzt.")
        await _alert(config, f"⚠️ Jarvis: Hintergrund-Aufgabe '{label}' hat nicht reagiert und wurde abgebrochen.")
    except Exception as e:
        _log(f"FEHLER bei {label}: {_exc(e)}")
    finally:
        _sd_notify("WATCHDOG=1")
    return None


async def main():
    _log("Background Brain gestartet.")
    # Type=notify erwartet READY=1, sobald der Dienst wirklich laeuft, sonst
    # wertet systemd den Start selbst nach TimeoutStartSec als fehlgeschlagen.
    _sd_notify("READY=1")
    # Wall-clock, persisted to disk — see _load_timers() for why. 0 (epoch)
    # for any gate never run before, so a genuinely fresh install still runs
    # everything once immediately, same as the old -inf behavior.
    timers = _load_timers()
    last_instagram = timers.get("instagram", 0)
    last_business = timers.get("business", 0)
    last_research = timers.get("research", 0)
    last_self_improve = timers.get("self_improve", 0)
    last_skill_growth = timers.get("skill_growth", 0)
    last_jerome = timers.get("jerome", 0)
    last_deep_analysis = timers.get("deep_analysis", 0)
    last_insights_inbox = timers.get("insights_inbox", 0)
    last_link_funnel = timers.get("link_funnel", 0)
    last_target_creator = timers.get("target_creator", 0)
    last_calendar_check = timers.get("calendar_check", 0)
    last_meeting_reminder = timers.get("meeting_reminder", 0)
    last_screen_awareness = timers.get("screen_awareness", 0)
    last_finance_sync = timers.get("finance_sync", 0)
    last_gmail_reply = timers.get("gmail_reply", 0)
    last_goal_check = timers.get("goal_check", 0)
    last_memory_consolidation = timers.get("memory_consolidation", 0)
    video_rotation_index = 0

    while True:
        config = _load_config()
        now = time.time()

        # Roadmap Punkt 21 — jeder Pass-Aufruf lief bisher in einem nackten
        # try/except, das eine RAISED Exception faengt, aber einen echten
        # Haenger (Coroutine wird einfach nie fertig) nicht sieht und die
        # ganze Schleife dauerhaft einfrieren wuerde. _run_pass_safely()
        # ersetzt das ueberall durch asyncio.wait_for mit PASS_TIMEOUT_SECONDS.

        if now - last_instagram >= INSTAGRAM_INTERVAL_SECONDS:
            await _run_pass_safely("Instagram-Check", config, instagram_pass(config))
            await _run_pass_safely("Fanplace-Check", config, fanplace_pass(config))
            last_instagram = now
            _save_timer("instagram", now)

        # Eigener, sehr schneller Takt (Ahmad, 2026-08-07: "muss zuegiger gehen") —
        # unabhaengig von allen anderen Zyklen, laeuft immer, keine Arbeitszeit-Gate.
        if now - last_insights_inbox >= INSIGHTS_INBOX_INTERVAL_SECONDS:
            await _run_pass_safely("Insights-Eingang", config, insights_inbox_pass(config))
            last_insights_inbox = now
            _save_timer("insights_inbox", now)

        # Eigener Takt, stuendlich reicht (Ahmad traegt Profile Visits nur woechentlich ein).
        if now - last_link_funnel >= LINK_FUNNEL_INTERVAL_SECONDS:
            await _run_pass_safely("Link Funnel", config, link_funnel_pass(config))
            last_link_funnel = now
            _save_timer("link_funnel", now)

        # Eigener Takt, siehe CALENDAR_CHECK_INTERVAL_SECONDS oben — aendert
        # nichts am Kalender, meldet nur echte Ueberschneidungen.
        if now - last_calendar_check >= CALENDAR_CHECK_INTERVAL_SECONDS:
            await _run_pass_safely("Kalender-Konfliktcheck", config, calendar_conflict_pass(config))
            last_calendar_check = now
            _save_timer("calendar_check", now)

        # Eigener schneller Takt, siehe MEETING_REMINDER_INTERVAL_SECONDS oben —
        # zeitkritisch, keine Arbeitszeit-Gate, aendert nichts am Kalender.
        if now - last_meeting_reminder >= MEETING_REMINDER_INTERVAL_SECONDS:
            await _run_pass_safely("Meeting-Reminder", config, meeting_reminder_pass(config))
            last_meeting_reminder = now
            _save_timer("meeting_reminder", now)

        # Eigener Takt, siehe SCREEN_AWARENESS_INTERVAL_SECONDS oben —
        # niedrigfrequent, rein passiv, Roadmap Punkt 19.
        if now - last_screen_awareness >= SCREEN_AWARENESS_INTERVAL_SECONDS:
            await _run_pass_safely("Screen-Awareness", config, screen_awareness_pass(config))
            last_screen_awareness = now
            _save_timer("screen_awareness", now)

        # Eigener Takt, siehe FINANCE_SYNC_INTERVAL_SECONDS oben — Kurs +
        # Fanplace-Payouts + zurueckhaltender Trend-Check.
        if now - last_finance_sync >= FINANCE_SYNC_INTERVAL_SECONDS:
            await _run_pass_safely("Finanz-Sync", config, finance_sync_pass(config))
            last_finance_sync = now
            _save_timer("finance_sync", now)

        # Eigener Takt, siehe GMAIL_REPLY_INTERVAL_SECONDS oben — legt nur
        # Entwuerfe an, sendet nie selbst.
        if now - last_gmail_reply >= GMAIL_REPLY_INTERVAL_SECONDS:
            await _run_pass_safely("Gmail-Reply-Check", config, gmail_reply_pass(config))
            last_gmail_reply = now
            _save_timer("gmail_reply", now)

        # Eigener Takt, siehe GOAL_CHECK_INTERVAL_SECONDS oben — aendert
        # keine Ziele, meldet nur bei Stillstand.
        if now - last_goal_check >= GOAL_CHECK_INTERVAL_SECONDS:
            await _run_pass_safely("Ziel-Check", config, goal_progress_pass(config))
            last_goal_check = now
            _save_timer("goal_check", now)

        # Eigener, langsamer Takt (einmal taeglich reicht, Wartungs-Charakter) —
        # siehe MEMORY_CONSOLIDATION_INTERVAL_SECONDS oben, mergt Duplikate im
        # Langzeitgedaechtnis und archiviert alten Gespraechsverlauf, loescht nie destruktiv.
        if now - last_memory_consolidation >= MEMORY_CONSOLIDATION_INTERVAL_SECONDS:
            await _run_pass_safely("Gedaechtnis-Konsolidierung", config, memory_consolidation_pass(config))
            last_memory_consolidation = now
            _save_timer("memory_consolidation", now)

        # Eigener, langsamer Takt — Multi-Frame-Tiefenanalyse ist teuer, ein paar Mal am Tag reicht.
        if now - last_target_creator >= TARGET_CREATOR_INTERVAL_SECONDS:
            await _run_pass_safely("Target-Creator-Analyse", config, target_creator_analysis_pass(config))
            last_target_creator = now
            _save_timer("target_creator", now)

        # Eigener, schneller Takt (Ahmad, 2026-08-06: "kuerzer als 90 Minuten,
        # damit wir schneller reagieren") — nur innerhalb Jeromes tatsaechlicher
        # Arbeitszeit (Philippinen, ~4h/Tag ab meistens 11-12 Uhr deutscher Zeit).
        current_hour = int(time.strftime("%H"))
        in_jerome_hours = JEROME_WORK_HOUR_START <= current_hour < JEROME_WORK_HOUR_END
        if in_jerome_hours and now - last_jerome >= JEROME_INTERVAL_SECONDS:
            await _run_pass_safely("Jerome-Antwort-Check", config, jerome_reply_pass(config))
            last_jerome = now
            _save_timer("jerome", now)

        await _run_pass_safely("Tages-Zusammenfassung", config, daily_summary_pass(config))
        await _run_pass_safely("Morgen-Briefing", config, morning_briefing_pass(config))
        await _run_pass_safely("Content-Brief", config, content_brief_pass(config))

        # Recherche laeuft bewusst auf ihrem EIGENEN, langsameren Takt (1-2x/Tag,
        # Ahmads Wunsch) — losgeloest vom Business-Zyklus unten, der geschaefts-
        # kritische Dinge (virale Videos, Jerome-Trial-Reels) weiter zuegig macht.
        if now - last_research >= RESEARCH_INTERVAL_SECONDS:
            await _run_pass_safely("Recherche", config, research_pass(config))
            last_research = now
            _save_timer("research", now)

        if now - last_business >= BUSINESS_CYCLE_INTERVAL_SECONDS:
            await _run_pass_safely("Account-Discovery", config, discovery_pass(config))

            config = _load_config()  # discovery_pass may have grown competitor_accounts

            await _run_pass_safely("Sheet-Sync", config, sheets_sync_pass(config))

            result = await _run_pass_safely(
                "Video-Analyse", config, video_analysis_pass(config, video_rotation_index)
            )
            if result is not None:
                video_rotation_index = result

            await _run_pass_safely("Trial-Reel-Scan", config, trial_reel_pass(config))
            await _run_pass_safely("Trial-Reel-Nachfrage", config, trial_wave_nudge_pass(config))

            last_business = now
            _save_timer("business", now)

        # Eigener, langsamer woechentlicher Takt (Ahmad, 2026-08-06: "yes klingt
        # mega lets go") — teurer als die anderen Passes (Multi-Frame Vision pro
        # Video), deshalb bewusst selten und von den anderen Zyklen entkoppelt.
        if now - last_deep_analysis >= DEEP_ANALYSIS_INTERVAL_SECONDS:
            await _run_pass_safely("Tiefen-Analyse", config, deep_pattern_analysis_pass(config))
            last_deep_analysis = now
            _save_timer("deep_analysis", now)

        # Eigener, schneller Takt (Ahmad, 2026-08-06: "er soll IMMER auf
        # Fehlersuche gehen") — losgeloest vom langsameren Business-Zyklus,
        # damit ein neuer Fehler nicht erst Stunden spaeter auffaellt.
        if now - last_self_improve >= SELF_IMPROVE_INTERVAL_SECONDS:
            # Laengerer Timeout als der Standard-Watchdog-Deckel (180s): dieser
            # Pass ruft claude_code_tool.run_claude_code_with_commit() auf,
            # dessen EIGENES Timeout bei 600s liegt. Mit dem Standard-Deckel
            # wuerde der aeussere Watchdog per CancelledError abbrechen, BEVOR
            # run_claude_code's eigener except-Block (der den Subprozess
            # sauber killt) je greift -- der claude-Unterprozess koennte
            # dadurch verwaist im Hintergrund weiterlaufen, moeglicherweise
            # mitten in einem Git-Commit. 650s liegt bewusst ueber den 600s,
            # damit IMMER der innere Timeout zuerst feuert.
            await _run_pass_safely("Self-Improve", config, self_improve_pass(config), timeout=650)
            last_self_improve = now
            _save_timer("self_improve", now)

        # Ahmad (2026-08-10): "ich brauche es, damit er eigenstaendiger wird" —
        # gleicher Takt und derselbe laengere Timeout-Grund wie Self-Improve
        # direkt darueber (ruft ebenfalls claude_code_tool auf).
        if now - last_skill_growth >= SKILL_GROWTH_INTERVAL_SECONDS:
            await _run_pass_safely("Skill-Growth", config, skill_growth_pass(config), timeout=650)
            last_skill_growth = now
            _save_timer("skill_growth", now)

        # Credit/billing-error alert — deduped so a genuinely exhausted key
        # (which would otherwise fail EVERY pass, EVERY tick) sends ONE clear
        # heads-up instead of spamming, but still repeats occasionally in
        # case the first alert gets missed.
        global _credit_issue_seen_at
        if _credit_issue_seen_at is not None:
            timers_now = _load_timers()
            if now - timers_now.get("credit_alert", 0) > CREDIT_ALERT_COOLDOWN_SECONDS:
                await _alert(
                    config,
                    "⚠️ Jarvis: Mehrere Hintergrund-Aufgaben sind zuletzt an einem Anthropic-API-Fehler "
                    "gescheitert, der nach Guthaben/Abrechnung aussieht. Bitte kurz das Anthropic-Konto "
                    "pruefen — sonst laufen Recherche, Jerome-Chat & Co. im Hintergrund leer.",
                )
                _save_timer("credit_alert", now)
            _credit_issue_seen_at = None

        # Roadmap Punkt 21 — sagt systemd "dieser Tick ist komplett durchgelaufen,
        # ich haenge nicht". Bleibt das aus (WatchdogSec in jarvis-brain.service),
        # killt und startet systemd den Dienst selbst neu — das aeussere Netz
        # fuer alles, was selbst ein wait_for-Timeout oben nicht auffangen konnte.
        _sd_notify("WATCHDOG=1")

        await asyncio.sleep(LOOP_TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
