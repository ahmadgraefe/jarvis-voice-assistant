import asyncio
import json
import sys

sys.path.insert(0, "/opt/jarvis")
import background_brain as bb
import memory

with open("/opt/jarvis/config.json") as f:
    config = json.load(f)

TEST_GAP = (
    "Ahmad wollte wissen wie ausgelastet der Hetzner-Server gerade ist "
    "(freier Speicherplatz auf der Festplatte und freier Arbeitsspeicher), "
    "Jarvis hatte dafuer kein Werkzeug."
)


async def _fake_gap(config, excerpt):
    return TEST_GAP


async def main():
    print(f"Vorher: builds_today={memory.get_skill_builds_today()}")
    print(f"Vorher: letzter skill_growth Eintrag vorhanden? {bool(memory.get_recent_skill_growth_entries(hours=999999))}")

    # Deterministischer Test: echte Luecken-Erkennung durch eine feste
    # Test-Luecke ersetzt (kein Rateen ob gerade zufaellig was im echten
    # Gespraechsverlauf steht), ALLES danach ist der echte, unveraenderte
    # Code-Pfad von skill_growth_pass.
    bb._find_capability_gap = _fake_gap

    await bb.skill_growth_pass(config)

    print(f"\nNachher: builds_today={memory.get_skill_builds_today()}")
    entries = memory.get_recent_skill_growth_entries(hours=1)
    print(f"Nachher: {len(entries)} frischer Changelog-Eintrag/Eintraege")
    for e in entries:
        print(f"  [{e['timestamp']}] commit={e.get('commit_hash')}: {e['result'][:300]}")


asyncio.run(main())
