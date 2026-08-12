"""
Jarvis V2 — Jerome Communication
Autonomous WhatsApp coordination with Jerome (Luna Vale's content editor).
Ahmad explicitly authorized this without needing to ask him first each
time — WhatsApp send access was already fully granted earlier. Messages
are ENGLISH (Jerome doesn't speak German — documented in the Tracking-
Workbook's own notes), signed, and deliberately kept SHORT: Ahmad's own
words were "he needs a good explanation, but not too long, he gets
confused easily."
"""

import json
import os
import re
import time

import app_control
import instagram_tools
import memory
import sheets_tools
import whatsapp_tools

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LUNA_VALE_STATUS_PATH = os.path.join(os.path.dirname(__file__), "claude_app_status.md")
# Server (2026-08-10): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-jerome.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-jerome.log")
)
NOTIFIED_PATH = os.path.join(os.path.dirname(__file__), "memory", "jerome_notified_videos.json")
LAST_REPLY_PATH = os.path.join(os.path.dirname(__file__), "memory", "jerome_last_reply.json")
WAVE_CREATED_PATH = os.path.join(os.path.dirname(__file__), "memory", "trial_wave_created_at.json")

SIGNATURE = "\n\n— Jarvis (Ahmad's AI assistant)"


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _load_notified() -> set:
    if not os.path.exists(NOTIFIED_PATH):
        return set()
    try:
        with open(NOTIFIED_PATH) as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_notified(video_link: str):
    notified = _load_notified()
    notified.add(video_link)
    os.makedirs(os.path.dirname(NOTIFIED_PATH), exist_ok=True)
    with open(NOTIFIED_PATH, "w") as f:
        json.dump(sorted(notified), f)


def already_notified(video_link: str) -> bool:
    return video_link in _load_notified()


def _mark_wave_created(account: str, wave):
    """Precise creation timestamp for a Trial Reel Wave, kept OUTSIDE the
    sheet — the sheet's own Date column has no time component, so 'created
    at midnight' vs 'created at 3pm' looked identical there and made the
    nudge pass think a brand-new wave was already hours old (caught live,
    2026-08-06)."""
    data = {}
    if os.path.exists(WAVE_CREATED_PATH):
        try:
            with open(WAVE_CREATED_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    data[f"{account}|{wave}"] = time.time()
    os.makedirs(os.path.dirname(WAVE_CREATED_PATH), exist_ok=True)
    with open(WAVE_CREATED_PATH, "w") as f:
        json.dump(data, f)


def get_wave_created_at(account: str, wave):
    """Epoch timestamp a wave was created, or None if unknown (e.g. a wave
    that existed before this tracking was added — the nudge pass should
    just fall back to asking rather than silently never asking)."""
    if not os.path.exists(WAVE_CREATED_PATH):
        return None
    try:
        with open(WAVE_CREATED_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data.get(f"{account}|{wave}")


async def notify_jerome(account: str, video_link: str, virality_factor: str, next_step: str) -> str:
    """Short, clear Trial Reel instruction — English, signed, deliberately
    concise per Ahmad's explicit instruction."""
    config = _load_config()
    contact = config.get("jerome_contact", "").strip()
    if not contact:
        return "ERROR: Jerome's WhatsApp-Kontakt ist noch nicht in config.json hinterlegt (jerome_contact)."

    message = (
        f"Hey Jerome!\n\n"
        f"Strong performer for {account}:\n{video_link}\n\n"
        f"Why it worked:\n{virality_factor}\n\n"
        f"Trial Reel:\n{next_step}\n"
        f"(change ONE variable only, keep everything else the same)\n\n"
        f"Full details are in the Tracking Sheet as always — I keep it updated for you."
        f"{SIGNATURE}"
    )

    result = await app_control.send_whatsapp(contact, message)
    _log(f"Jerome benachrichtigt ueber {video_link}: {result}")
    # Handlungsprotokoll (2026-08-12): diese Nachricht geht ohne Ahmads Zutun
    # raus, ist aber eine echte, nach aussen sichtbare eigene Handlung. Ohne
    # Eintrag koennte Jarvis spaeter nicht belegen, dass/was er selbst getan
    # hat — genau die Luecke aus Ahmads Frage "war das ich oder Jerome?".
    memory.add_action_entry(
        "whatsapp_an_jerome_gesendet",
        target=f"Jerome ({account})",
        detail=f"Trial-Reel-Anweisung zu {video_link}",
        outcome="error" if str(result).startswith("ERROR") else "ok",
        initiator="autonom",
    )
    return result


async def send_raw_message(message_body: str) -> str:
    """Send an already-fully-composed message to Jerome as ONE atomic send
    (signature appended) — for flows like the daily content brief that
    build their own complete message rather than using notify_jerome's
    fixed trial-reel template. Never call this with a partial/in-progress
    draft — the whole point is exactly one complete message, never pieces."""
    config = _load_config()
    contact = config.get("jerome_contact", "").strip()
    if not contact:
        return "ERROR: Jerome's WhatsApp-Kontakt ist noch nicht in config.json hinterlegt (jerome_contact)."
    result = await app_control.send_whatsapp(contact, message_body + SIGNATURE)
    _log(f"Rohe Nachricht an Jerome gesendet: {result}")
    memory.add_action_entry(
        "whatsapp_an_jerome_gesendet",
        target="Jerome",
        detail=" ".join(message_body.split())[:200],
        outcome="error" if str(result).startswith("ERROR") else "ok",
        initiator="autonom",
    )
    return result


async def handle_trial_reel_candidate(
    account: str, video_link: str, virality_factor: str, next_step: str, notify_ahmad_fn=None
) -> str:
    """Full flow: log to Scaling Log, message Jerome, tell Ahmad what was
    sent. He's not asked first (per his own instruction), but he should
    never be surprised by something that went out in his name to a real
    employee — same transparency pattern used everywhere else in this
    background system."""
    if already_notified(video_link):
        return f"@{account} {video_link} wurde bereits an Jerome gemeldet — kein Doppel-Versand."

    sheet_result = await sheets_tools.add_scaling_log_entry({
        "account": account,
        "original_winner_link": video_link,
        "virality_factor": virality_factor,
        "next_step": next_step,
    })
    # Wave 1 in Trial Reel Waves — the other end of the loop (handle_wave_result
    # below) updates THIS row once Ahmad reports back what Jerome actually posted.
    wave_result = await sheets_tools.add_trial_reel_wave({
        "account": account, "wave": 1, "raw_file_source": video_link,
        "changed_variable": next_step, "kept_unchanged": virality_factor,
    })
    _mark_wave_created(account, 1)

    jerome_result = await notify_jerome(account, video_link, virality_factor, next_step)
    if not jerome_result.startswith("ERROR:"):
        _mark_notified(video_link)

    summary = (
        f"Trial-Reel-Kandidat @{account} bearbeitet: {sheet_result} | {wave_result}\n"
        f"Jerome benachrichtigt: {jerome_result}"
    )

    if notify_ahmad_fn:
        await notify_ahmad_fn(
            f"Jarvis Update: Habe Jerome wegen einem starken Video von @{account} kontaktiert "
            f"({video_link}). Grund: {virality_factor}. Vorschlag: {next_step}"
        )

    return summary


# ---------------------------------------------------------------------------
# Trial Reel Wave RESULTS — the other half of the loop Ahmad described
# (2026-08-06): handle_trial_reel_candidate above starts a wave, this closes
# it once Ahmad reports back what Jerome actually posted (same self-chat
# link+Insights flow as Winner Tracking). Confirmed/next-wave/stop against
# the Sheet's OWN 2x rule — never a fresh judgment call.
# ---------------------------------------------------------------------------

MAX_TRIAL_WAVES = 3  # matches the sheet's own "Wave (1/2/3)" column


async def _determine_next_wave_variable(client, account: str, wave_entry: dict) -> str:
    """Wave N didn't hit 2x — ask for exactly ONE new, DIFFERENT variable to
    try next, never the same one twice, never more than one at once."""
    prompt = (
        f"A trial reel for @{account} did NOT hit the 2x performance bar. Details of what "
        f"was already tried: {json.dumps(wave_entry, ensure_ascii=False)}\n\n"
        "Suggest ONE new, DIFFERENT single-variable change to test next — NOT the same "
        "variable already tried (see changed_variable above), NOT multiple changes at "
        "once, and keep whatever worked before (kept_unchanged) untouched. Answer with "
        "ONE clear sentence, in ENGLISH (goes straight to Jerome, he doesn't speak German)."
    )
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def notify_jerome_wave_confirmed(account: str, video_link: str, wave) -> str:
    message = (
        f"Wave {wave} for {account} CONFIRMED — it hit the 2x bar!\n\n"
        f"{video_link}\n\n"
        f"Keep making more like this one."
    )
    return await send_raw_message(message)


async def notify_jerome_next_wave(account: str, wave, next_variable: str) -> str:
    message = (
        f"Wave {wave - 1} for {account} didn't hit 2x — let's try Wave {wave}.\n\n"
        f"New variable to test: {next_variable}\n"
        f"(change ONLY this, keep everything else the same as last time)"
    )
    return await send_raw_message(message)


async def notify_jerome_waves_stopped(account: str) -> str:
    message = (
        f"We tried {MAX_TRIAL_WAVES} waves for {account} and none hit 2x — pausing on "
        f"this one for now. Let's move to a different winner as the next base."
    )
    return await send_raw_message(message)


async def handle_insight_data(
    video_link: str, us_audience_pct, reach, views, anthropic_client, config: dict = None
) -> str:
    """Per-video routing shared by every insights-entry path (2026-08-07:
    replaced the WhatsApp-screenshot workflow — Vision reading numbers off a
    phone screenshot proved unreliable in production, both for the numbers
    themselves and for the underlying post URL, which WhatsApp's link-
    preview card often doesn't show as readable text at all — with a plain
    Sheet inbox row Ahmad types directly, so there is nothing left to
    misread). Used by BOTH an on-demand check and the fully autonomous
    background pass (background_brain.py) — same logic, same guarantees,
    regardless of who triggered it: an OPEN Trial Reel Wave for the matched
    account means this is Jerome's posted result, otherwise it's a fresh
    Winner Tracking entry."""
    config = config or _load_config()
    fields = {}
    if views is not None:
        fields["views"] = views
    if us_audience_pct is not None:
        fields["us_audience_pct"] = us_audience_pct
    if reach is not None:
        fields["notes"] = f"Reach {reach} (aus Insights Eingang, {time.strftime('%Y-%m-%d')})"

    known_handles = config.get("luna_vale_accounts", []) + config.get("competitor_accounts", [])
    account = await instagram_tools.identify_post_account(video_link, known_handles, anthropic_client)

    if account:
        open_waves = await sheets_tools.read_open_trial_waves(account)
        if open_waves:
            if reach is None:
                return (
                    f"ERROR ({video_link}): Trial-Reel-Welle fuer @{account} ist offen, aber kein "
                    "Reach-Wert angegeben — wird fuer den 2x-Vergleich gebraucht."
                )
            return await handle_wave_result(account, video_link, reach, anthropic_client)

    existing_rows = await sheets_tools.read_winner_tracking()
    target_id = instagram_tools.normalize_video_url(video_link)
    already_tracked = any(
        instagram_tools.normalize_video_url(r.get("video_link") or "") == target_id
        for r in existing_rows if not r.get("error")
    )

    if not already_tracked:
        if not account:
            return (
                f"ERROR ({video_link}): Konnte den Account nicht sicher bestimmen — "
                "nicht automatisch anlegbar."
            )
        await sheets_tools.add_winner_tracking_entry({
            "account": account, "video_link": video_link, "views": views or 0,
        })
        await sheets_tools.compute_baseline_avg(account, video_link)

    update_result = await sheets_tools.update_winner_tracking_insights(video_link, fields)
    if us_audience_pct is None:
        update_result += " | HINWEIS: US-Audience-% wurde nicht angegeben."
    return update_result


async def handle_wave_result(
    account: str, video_link: str, views_after_3h, anthropic_client, notify_ahmad_fn=None
) -> str:
    """Takes a REPORTED trial-reel result (video_link + views Ahmad just
    sent), decides confirmed/next-wave/stop against the sheet's 2x rule,
    updates Trial Reel Waves, and messages Jerome — one atomic step, same
    'gather everything, THEN act' principle as content_strategy.py."""
    open_waves = await sheets_tools.read_open_trial_waves(account)
    if not open_waves:
        return f"ERROR: Keine offene Trial-Reel-Welle fuer @{account} gefunden."
    wave_entry = open_waves[0]  # oldest open wave first
    wave = wave_entry["wave"]
    try:
        wave_num = int(wave)
    except (TypeError, ValueError):
        wave_num = 1

    account_avg = await sheets_tools.compute_account_avg_views(account)

    # A newer account (e.g. cowgirllunavale/lunavalethegoth, confirmed live:
    # zero Winner Tracking history yet) has no baseline at all — treating
    # "no baseline" the same as "definitely didn't hit 2x" would silently
    # kill or redirect a wave based on NO real judgment, not a bad one.
    # Record the raw numbers and hand the call to Ahmad instead of guessing.
    if account_avg is None or account_avg <= 0:
        update_result = await sheets_tools.update_trial_reel_wave_result(
            account, wave, video_link, views_after_3h, account_avg,
            False, "No baseline yet — Ahmad needs to judge this one manually"
        )
        summary = (
            f"Wave {wave} @{account}: {views_after_3h} Views gemeldet, aber noch KEIN "
            f"Vergleichs-Durchschnitt fuer @{account} vorhanden (zu wenig Winner-Tracking-"
            f"Historie) — konnte den 2x-Vergleich nicht automatisch machen. Zahlen sind im "
            f"Sheet eingetragen, aber du musst selbst einschaetzen ob's ein Erfolg war. {update_result}"
        )
        _log(summary)
        if notify_ahmad_fn:
            await notify_ahmad_fn(f"Jarvis: {summary}")
        return summary

    hit_2x = float(views_after_3h) >= 2 * account_avg

    if hit_2x:
        update_result = await sheets_tools.update_trial_reel_wave_result(
            account, wave, video_link, views_after_3h, account_avg, True, "Confirmed — scale this variation"
        )
        jerome_result = await notify_jerome_wave_confirmed(account, video_link, wave)
        summary = f"Wave {wave} @{account} BESTAETIGT (2x geschafft, Schnitt war {account_avg}). {update_result}"
    elif wave_num >= MAX_TRIAL_WAVES:
        update_result = await sheets_tools.update_trial_reel_wave_result(
            account, wave, video_link, views_after_3h, account_avg, False,
            f"Did not hit 2x after {MAX_TRIAL_WAVES} waves — stopped"
        )
        jerome_result = await notify_jerome_waves_stopped(account)
        summary = f"Wave {wave} @{account} NICHT bestaetigt, {MAX_TRIAL_WAVES} Wellen erreicht — gestoppt. {update_result}"
    else:
        next_variable = await _determine_next_wave_variable(anthropic_client, account, wave_entry)
        next_wave_num = wave_num + 1
        update_result = await sheets_tools.update_trial_reel_wave_result(
            account, wave, video_link, views_after_3h, account_avg, False, f"Wave {next_wave_num}: {next_variable}"
        )
        new_wave_result = await sheets_tools.add_trial_reel_wave({
            "account": account, "wave": next_wave_num,
            "raw_file_source": wave_entry.get("raw_file_source") or video_link,
            "changed_variable": next_variable, "kept_unchanged": wave_entry.get("kept_unchanged") or "",
        })
        _mark_wave_created(account, next_wave_num)
        jerome_result = await notify_jerome_next_wave(account, next_wave_num, next_variable)
        summary = (
            f"Wave {wave} @{account} nicht bestaetigt -> Wave {next_wave_num} gestartet ({next_variable}). "
            f"{update_result} | {new_wave_result}"
        )

    _log(f"{summary} | Jerome: {jerome_result}")
    if notify_ahmad_fn:
        await notify_ahmad_fn(f"Jarvis: Trial-Reel-Ergebnis fuer @{account} verarbeitet — {summary}")
    return summary


def _normalize_for_dedup(text: str) -> str:
    """Vision re-reads of the exact same bubble aren't always byte-identical
    (caught live: 'Be safe always' vs 'Be safe always \U0001F64F' for the
    SAME message across two calls, likely emoji-transcription variance) —
    comparing on normalized (lowercased, emoji/punctuation-stripped, collapsed
    whitespace) text avoids false 'new reply' triggers from that noise."""
    stripped = re.sub(r'[^\w\s]', '', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', stripped).strip().lower()


def _load_last_reply() -> str:
    if not os.path.exists(LAST_REPLY_PATH):
        return ""
    try:
        with open(LAST_REPLY_PATH) as f:
            return json.load(f).get("normalized", "")
    except (json.JSONDecodeError, OSError):
        return ""


def _save_last_reply(text: str):
    os.makedirs(os.path.dirname(LAST_REPLY_PATH), exist_ok=True)
    with open(LAST_REPLY_PATH, "w") as f:
        json.dump({
            "text": text,
            "normalized": _normalize_for_dedup(text),
            "seen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, f)


async def check_jerome_replies(anthropic_client) -> str:
    """Opens Jerome's WhatsApp chat, reads the newest incoming bubble via
    Vision, and returns it ONLY if it's actually new since the last check
    (dedup'd via LAST_REPLY_PATH so the same reply doesn't get reported
    twice) AND still actually open — if the chat's last message overall is
    already OUTGOING (Ahmad replied himself, or Jarvis already did on an
    earlier pass), the conversation is closed and nothing more is due, even
    if Jerome's message text itself looks 'new' (e.g. he edited it after
    Ahmad already answered — confirmed live, 2026-08-07). Returns "" if
    there's nothing new/open — that's the normal case on most checks, not
    an error."""
    config = _load_config()
    contact = config.get("jerome_contact", "").strip()
    if not contact:
        return ""

    phone = await app_control.resolve_contact_phone(contact)
    if not phone:
        reason = app_control.last_contacts_error() or "kein Treffer in Contacts.app"
        _log(f"FEHLER: Konnte Jerome's Nummer nicht aufloesen (Kontakt '{contact}'): {reason}")
        return ""

    try:
        png_bytes = await whatsapp_tools.open_chat_and_screenshot(phone)
        result = await whatsapp_tools.read_latest_incoming_message(anthropic_client, png_bytes, contact)
    except Exception as e:
        _log(f"FEHLER beim Lesen von Jerome's Chat: {e}")
        return ""

    latest = result["text"]
    if not latest:
        return ""

    if result["already_answered"]:
        # Someone already replied after this — still record it so a later,
        # genuinely-new Jerome message doesn't get suppressed by a stale
        # dedup entry, but don't treat it as something needing OUR reply.
        _save_last_reply(latest)
        _log(f"Jerome-Nachricht bereits beantwortet (nicht von Jarvis' letztem Check): {latest}")
        return ""

    if _normalize_for_dedup(latest) == _load_last_reply():
        return ""  # already seen/reported this exact reply

    _save_last_reply(latest)
    _log(f"Neue Antwort von Jerome: {latest}")
    return latest


# ---------------------------------------------------------------------------
# Two-way Jerome chat — Ahmad's ask (2026-08-06): Jarvis shouldn't just relay
# Jerome's messages to Ahmad, it should actually HELP Jerome directly
# (Instagram/content/marketing questions, tips, what to work on) using
# everything it already knows about which of Luna Vale's own videos and
# hooks perform well. Confirmed scope with Ahmad: autonomous on anything
# content/Instagram/marketing-related; escalates (never guesses) on payment,
# contract, schedule/personal matters, or real business decisions. Ahmad
# only gets pinged for genuine news/decisions, not routine back-and-forth —
# his own explicit choice, so this stays useful without becoming noise.
# ---------------------------------------------------------------------------

def _get_luna_vale_knowledge() -> str:
    """Base playbook (claude_app_status.md) PLUS whatever live-Jarvis has
    learned mid-conversation and saved via REMEMBER with category=business
    (Ahmad, 2026-08-06: he should be able to update Jarvis's real knowledge
    just by telling it something, the same way Claude Code updates files —
    not only through the separate background research pipeline). Without
    this, a pattern Ahmad mentions live would sit in memory.py but never
    actually reach what Jarvis tells Jerome."""
    try:
        with open(LUNA_VALE_STATUS_PATH, "r", encoding="utf-8") as f:
            base = f.read().strip()
    except OSError:
        base = ""
    live_updates = memory.get_category("business")
    if live_updates:
        base += f"\n\n## Live von Ahmad ergaenzt (waehrend Gespraechen)\n{live_updates}"
    return base


async def _get_account_performance_summary(config: dict, max_chars: int = 3000) -> str:
    """What's actually working right now, per Luna Vale account — pulled
    from Winner Tracking's own Decision column (never a fresh guess), so
    Jarvis can coach Jerome with real numbers instead of generic advice."""
    accounts = config.get("luna_vale_accounts", [])
    sections = []
    for account in accounts:
        try:
            rows = await sheets_tools.read_winner_tracking(account=account, limit=15)
        except Exception:
            continue
        keeps = [r for r in rows if not r.get("error") and str(r.get("decision") or "").upper() == "KEEP"]
        cautions = [r for r in rows if not r.get("error") and str(r.get("decision") or "").upper() == "CAUTION"]
        if not keeps and not cautions:
            continue
        lines = [f"@{account}:"]
        for r in keeps[:5]:
            try:
                em_text = f"{float(str(r.get('engagement_multiplier')).replace(',', '.')):.1f}x Engagement"
            except (TypeError, ValueError):
                em_text = "Engagement noch offen"
            lines.append(f"  KEEP: {r.get('video_link')} ({r.get('views')} views, {em_text})")
        if cautions:
            lines.append(f"  {len(cautions)} weitere Videos im CAUTION-Bereich (unklar, nicht zwingend empfehlenswert)")
        sections.append("\n".join(lines))

    text = "\n\n".join(sections) if sections else "Noch keine bestaetigten Winner in Winner Tracking."
    return text[-max_chars:] if len(text) > max_chars else text


async def handle_jerome_message(
    anthropic_client, message: str, config: dict, notify_ahmad_fn=None
) -> dict:
    """The other half of jerome_reply_pass's read: given something Jerome
    just said, decide whether Jarvis can help directly (content/Instagram/
    marketing) or must escalate to Ahmad (payment/contract/personal/real
    decisions), reply to Jerome accordingly, and tell Ahmad ONLY if it's
    actually news or something he needs to decide — never for routine
    back-and-forth (Ahmad's explicit choice, keeps this from becoming noise).

    Returns {"category": "routine"|"notable"|"needs_ahmad", "reply": "...",
    "ahmad_note": "..." or None} so callers (background pass, live action)
    can log/react without re-deriving anything."""
    business_knowledge = _get_luna_vale_knowledge()
    performance_summary = await _get_account_performance_summary(config)

    prompt = (
        f"Business-Wissen (Deutsch, nur fuer deinen Kontext):\n{business_knowledge}\n\n"
        f"Was gerade bei unseren Accounts nachweislich funktioniert (echte Sheet-Daten):\n"
        f"{performance_summary}\n\n"
        f"Jerome (Luna Vale's Content-Editor, spricht KEIN Deutsch, wird von langen Nachrichten "
        f"schnell verwirrt) hat gerade das geschrieben: \"{message}\"\n\n"
        "Entscheide:\n"
        "- 'routine': einfacher Alltags-Austausch (Status-Frage, kleine Bestaetigung, kurzer Tipp "
        "den du direkt aus dem obigen Wissen beantworten kannst) — du antwortest ihm direkt, "
        "Ahmad wird NICHT informiert.\n"
        "- 'notable': du kannst antworten, aber es ist etwas, das Ahmad wirklich wissen sollte "
        "(z.B. ein starkes Ergebnis, eine echte Neuigkeit) — du antwortest Jerome UND Ahmad "
        "bekommt eine kurze Notiz.\n"
        "- 'needs_ahmad': etwas das NUR Ahmad entscheiden/beantworten kann (Bezahlung, Vertrag, "
        "Zeitplan/Persoenliches, eine echte Geschaeftsentscheidung) — du rätst NIEMALS, "
        "stattdessen eine kurze, freundliche Zwischenantwort an Jerome ('let me check with Ahmad "
        "and get back to you' im gleichen Sinne), UND eine Notiz an Ahmad was er beantworten muss.\n\n"
        "Deine Antwort an Jerome (reply): kurz (er wird von langen Nachrichten verwirrt), hilfreich, "
        "IMMER auf ENGLISCH, freundlich/kollegial. Nutze bei content/Instagram-Fragen konkret die "
        "Performance-Daten oben, keine generischen Tipps.\n"
        "ahmad_note: nur bei 'notable' oder 'needs_ahmad' — ein klarer Satz auf DEUTSCH was Ahmad "
        "wissen/entscheiden muss. Bei 'routine': null.\n\n"
        'Antworte NUR als JSON: {"category": "routine|notable|needs_ahmad", "reply": "...", "ahmad_note": "..." oder null}'
    )

    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        start, end = raw.index("{"), raw.rindex("}") + 1
        decision = json.loads(raw[start:end])
    except Exception as e:
        _log(f"FEHLER bei der Jerome-Antwort-Entscheidung: {e}")
        return {"category": "error", "reply": None, "ahmad_note": None}

    reply = (decision.get("reply") or "").strip()
    category = decision.get("category", "needs_ahmad")
    ahmad_note = decision.get("ahmad_note")

    if reply:
        send_result = await send_raw_message(reply)
        _log(f"Jerome-Chat [{category}]: '{message}' -> '{reply}' ({send_result})")
    else:
        send_result = "kein Reply generiert"
        _log(f"Jerome-Chat [{category}]: '{message}' -> KEIN Reply generiert")

    if category in ("notable", "needs_ahmad") and ahmad_note and notify_ahmad_fn:
        prefix = "Jarvis Update (Jerome-Chat)" if category == "notable" else "Jarvis braucht deine Antwort fuer Jerome"
        await notify_ahmad_fn(f"{prefix}: {ahmad_note}\n\nJerome schrieb: \"{message}\"")

    return {"category": category, "reply": reply, "ahmad_note": ahmad_note}


async def compose_and_send_message(anthropic_client, topic: str, config: dict = None) -> str:
    """For an open-ended message Ahmad wants Jerome to get (tips, an
    explanation, an announcement) — described briefly by Ahmad in a live
    conversation, composed in FULL here with its own generous token budget,
    then sent as ONE atomic message. Built 2026-08-06 after a live incident:
    Ahmad asked live-Jarvis to write Jerome a longer message (tips + explain
    new capabilities), and it tried to compose that inline inside the live
    turn's own reply-to-Ahmad — which is deliberately capped at 250 tokens
    to keep TTS costs down, so the message got cut off mid-sentence and sent
    incomplete. Composing it in a SEPARATE call, outside that budget, makes
    that specific failure structurally impossible, not just less likely."""
    config = config or _load_config()
    business_knowledge = _get_luna_vale_knowledge()
    performance_summary = await _get_account_performance_summary(config)

    prompt = (
        f"Business-Wissen (Deutsch, nur fuer deinen Kontext):\n{business_knowledge}\n\n"
        f"Was gerade bei unseren Accounts nachweislich funktioniert:\n{performance_summary}\n\n"
        f"Ahmad moechte, dass du Jerome (Content-Editor, spricht KEIN Deutsch, wird von langen "
        f"Nachrichten schnell verwirrt) jetzt folgendes mitteilst: \"{topic}\"\n\n"
        "Schreib die VOLLSTAENDIGE WhatsApp-Nachricht dafuer, auf ENGLISCH, freundlich/kollegial, "
        "so lang wie fuer den Inhalt noetig aber nicht laenger — nutze wo passend konkrete Zahlen/"
        "Muster aus dem Wissen oben statt generischer Aussagen. Gib NUR den fertigen Nachrichtentext "
        "zurueck, keine Meta-Kommentare, KEINE eigene Signatur (wird automatisch ergaenzt)."
    )
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        message_body = response.content[0].text.strip()
    except Exception as e:
        _log(f"FEHLER beim Komponieren einer Jerome-Nachricht ({topic!r}): {e}")
        return f"ERROR: Nachricht konnte nicht komponiert werden: {e}"

    result = await send_raw_message(message_body)
    _log(f"Freie Nachricht an Jerome ({topic!r}): {result}")
    return result
