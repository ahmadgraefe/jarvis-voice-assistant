"""
Jarvis V2 — Autonomous Research
Uses Claude's built-in web_search tool to periodically look into social-media
algorithm changes and content-niche trends relevant to Luna Vale, then saves
whatever's actually new/useful into the persistent knowledge base (memory.py).
Designed to be called from background_brain.py on a schedule, independent of
whether Ahmad is actively talking to Jarvis.
"""

import json
import os
import re
import time

import anthropic

import memory

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/jarvis-research.log")

RESEARCH_TOPICS = [
    "Instagram Reels Algorithmus Aenderungen 2026 was funktioniert aktuell fuer Wachstum",
    "TikTok Algorithmus aktuelle Trends Strategien fuer Content Creator",
    "OnlyFans Fanplace Marketing Wachstumsstrategien aktuelle Taktiken",
    "Instagram Reels Hook und Storytelling Strategien die aktuell viral gehen",
    "Goth und alternative Aesthetic Content Creator Nische aktuelle Trends Social Media",
    "Welche Reels/TikToks gehen aktuell in der Goth/alternative/Cosplay Nische am meisten viral, welche Formate/Sounds/Hooks",
]


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


async def research_topic(client: anthropic.AsyncAnthropic, topic: str) -> str:
    """One web-search-backed research pass on a single topic. Returns Claude's summary text."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[{
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": 4,
            "allowed_callers": ["direct"],
        }],
        messages=[{
            "role": "user",
            "content": (
                f"Recherchiere aktuell im Internet zu: {topic}\n\n"
                "Fasse die 2-3 wichtigsten, konkret umsetzbaren Erkenntnisse auf Deutsch in "
                "kurzen Stichpunkten zusammen. Nur echte, neue, konkrete Informationen — keine "
                "Allgemeinplaetze wie 'poste regelmaessig'. Wenn du nichts Neues oder Relevantes "
                "findest, antworte exakt mit 'NICHTS_NEUES'."
            ),
        }],
    )
    # With the hosted web_search tool, response.content is: a short "let me
    # search for X" announcement, then the search tool blocks, then the REAL
    # answer — which itself arrives as MANY separate text blocks (the API
    # segments cited prose per-claim for citation attribution, confirmed by
    # inspecting a live response: blocks 3-9 were all one continuous answer).
    # So the fix isn't "keep only the last block" (that chopped the answer
    # mid-sentence) — it's "drop the announcement before the first tool call,
    # keep + join everything after it". Falls back to joining everything if
    # no tool was ever called (e.g. model answered from its own knowledge).
    any_tool_called = any(b.type in ("server_tool_use", "tool_use") for b in response.content)
    saw_tool = False
    text_parts = []
    for block in response.content:
        if block.type in ("server_tool_use", "tool_use"):
            saw_tool = True
        elif block.type == "text" and (saw_tool or not any_tool_called):
            text_parts.append(block.text)
    return "".join(text_parts).strip()


async def generate_fresh_topics(client: anthropic.AsyncAnthropic, existing_knowledge: str, count: int = 3) -> list:
    """Ask Claude to propose NEW, specific research angles not yet covered
    by the knowledge base — so each cycle actually grows the brain instead
    of re-summarizing the same fixed questions forever."""
    prompt = (
        "Hier ist ein Auszug aus dem bisher gesammelten Wissen ueber Social-Media-Algorithmen, "
        "Content-Nischen-Trends (Goth/Cosplay/alternative Aesthetic) und OnlyFans/Fanplace-Marketing:\n\n"
        f"{existing_knowledge[-3000:] if existing_knowledge else '(noch nichts gesammelt)'}\n\n"
        f"Schlage {count} NEUE, konkrete Recherche-Fragen vor, die hier noch NICHT abgedeckt sind und "
        "fuer das Wachstum eines Content-Creator-Business (Instagram/TikTok, Fanplace/OnlyFans-Style) "
        "wirklich relevant waeren — z.B. zu Monetarisierung, Retention, neuen Plattform-Features, "
        "Community-Building, oder Wettbewerbsanalyse. Nur die Fragen, eine pro Zeile, keine "
        "Nummerierung, keine Erklaerung."
    )
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return []
    text = "\n".join(b.text for b in response.content if b.type == "text")
    topics = [line.strip("- ").strip() for line in text.splitlines() if line.strip()]
    return topics[:count]


async def run_research_cycle(client: anthropic.AsyncAnthropic = None) -> list:
    """Research the fixed baseline topics PLUS self-generated fresh angles
    based on existing knowledge, save genuinely new findings to the
    knowledge base. Returns the list of findings saved this cycle (for alerting)."""
    config = _load_config()
    own_client = client is None
    if own_client:
        client = anthropic.AsyncAnthropic(api_key=config["anthropic_api_key"])

    findings = []
    try:
        fresh_topics = await generate_fresh_topics(client, memory.get_knowledge())
        if fresh_topics:
            _log(f"Neue Recherche-Themen selbst generiert: {fresh_topics}")

        for topic in RESEARCH_TOPICS + fresh_topics:
            try:
                summary = await research_topic(client, topic)
                if summary and "NICHTS_NEUES" not in summary:
                    memory.add_knowledge(summary, category="research")
                    findings.append({"topic": topic, "summary": summary})
                    _log(f"NEU: {topic} -> {summary[:150]}")
                else:
                    _log(f"nichts Neues: {topic}")
            except Exception as e:
                _log(f"ERROR bei '{topic}': {e}")
    finally:
        if own_client:
            await client.close()

    return findings


async def summarize_for_whatsapp(client: anthropic.AsyncAnthropic, findings: list) -> str:
    """Ahmad's explicit ask: the WhatsApp ping should actually TELL him
    what was found, not just announce a count he then has to go ask about.
    One extra cheap non-search call over everything this cycle found."""
    if not findings:
        return ""
    combined = "\n\n".join(f"[{f['topic']}]\n{f['summary']}" for f in findings)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{
                "role": "user",
                "content": (
                    "Du bist Jarvis. Hier sind mehrere Recherche-Ergebnisse aus einem Durchlauf:\n\n"
                    f"{combined}\n\n"
                    "Schreib daraus eine kurze WhatsApp-Nachricht an Ahmad (max. 4-5 Zeilen, "
                    "die 2-3 wirklich wichtigsten/umsetzbarsten Punkte als kurze Stichpunkte, "
                    "keine Einleitung, kein 'ich habe recherchiert'). Deutsch, Jarvis-Stil "
                    "(trocken, praezise, per Sie)."
                ),
            }],
        )
        return "\n".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:
        _log(f"FEHLER bei WhatsApp-Digest: {e}")
        return f"{len(findings)} neue Erkenntnisse recherchiert (Zusammenfassung fehlgeschlagen, siehe Wissensbasis)."


async def discover_new_accounts(
    client: anthropic.AsyncAnthropic, known_handles: list,
    niche: str = "Goth/Cosplay/alternative Aesthetic",
) -> list:
    """Ask Claude (with web_search) for promising new accounts in the given
    niche that aren't already tracked. Returns candidate handles (no '@'),
    UNVALIDATED — the caller must verify each actually exists and has a sane
    follower count before adding it permanently.

    niche is a parameter, not hardcoded (2026-08-06 fix) — it used to always
    say "Goth/Cosplay/alternative Aesthetic" regardless of caller, so a
    single combined search skewed toward whichever niche dominates
    known_handles (all-goth at the time) and never surfaced Cowgirl
    competitors at all despite Luna Vale running 3 different niches.

    known_handles is ONLY for exclusion ('don't suggest these, already
    tracked'), deliberately NOT framed as 'similar to these' in the prompt —
    caught live: with known_handles all-goth, "similar to these" bled into
    a Cowgirl search and made the model look for 'Cowgirl/Country-GOTH'
    hybrids instead of plain Cowgirl accounts, returning nothing."""
    known = ", ".join(f"@{h}" for h in known_handles) or "(noch keine)"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        tools=[{
            "type": "web_search_20260318",
            "name": "web_search",
            "max_uses": 4,
            "allowed_callers": ["direct"],
        }],
        messages=[{
            "role": "user",
            "content": (
                f"Recherchiere im Internet nach aufstrebenden Instagram Content Creators in der "
                f"{niche} Nische, die viel Wachstumspotenzial haben — bevorzugt mit englisch-"
                "sprachigem Publikum (USA/Kanada/UK/Australien). Schlage KEINE Accounts vor, die "
                f"bereits beobachtet werden: {known}. Gib NUR eine kommagetrennte Liste von "
                "Instagram-Handles zurueck (mit @, ohne weitere Erklaerung), maximal 5 Stueck. "
                "Wenn du nichts Passendes findest, antworte exakt mit 'NICHTS_NEUES'."
            ),
        }],
    )
    text_parts = [b.text for b in response.content if b.type == "text"]
    text = "\n".join(text_parts).strip()
    if "NICHTS_NEUES" in text:
        return []
    found = [h.lower() for h in re.findall(r'@([A-Za-z0-9._]+)', text)]
    known_lower = {h.lower() for h in known_handles}
    return list(dict.fromkeys(h for h in found if h not in known_lower))
