"""
Jarvis V2 — Instagram-Session neu erzeugen (manuell, lokal auf dem Mac)

Ahmads eigene Instagram-Session ist abgelaufen (2026-08-11 live vorgefunden:
instagram_session.json hatte kein sessionid/ds_user_id-Cookie mehr, alle
Follower-/Beitraege-Abrufe kamen seitdem leer zurueck). Instagram-Zugangsdaten
duerfen laut claude_app_status.md (Feste Grenzen, Punkt 3) NIEMALS von Jarvis
selbst eingegeben werden -- das muss Ahmad persoenlich tun, in einem echten,
sichtbaren Browser (der Server laeuft headless via Xvfb, kein Bildschirm zum
manuell Einloggen).

Ausfuehren (lokal auf dem Mac, NICHT auf dem Server):
    cd jarvis-voice-assistant
    source venv/bin/activate
    python3 scripts/instagram_login.py

Es oeffnet ein echtes, sichtbares Chrome-Fenster mit der Instagram-Login-Seite.
Ahmad meldet sich dort ganz normal an (inkl. 2FA falls noetig), druesckt dann
in diesem Terminal Enter. Das Skript speichert die eingeloggte Session als
instagram_session.json im Projekt-Root -- genau die Datei, die
instagram_tools.py wiederverwendet. Danach die Datei auf den Server kopieren:

    scp -i ~/.ssh/hetzner_jarvis instagram_session.json root@100.116.28.82:/opt/jarvis/instagram_session.json
"""

import asyncio
import os

from playwright.async_api import async_playwright

SESSION_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "instagram_session.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=UA, locale="de-DE")
        page = await context.new_page()
        await page.goto("https://www.instagram.com/accounts/login/")

        print("\nBitte im geoeffneten Fenster ganz normal bei Instagram einloggen (inkl. 2FA falls noetig).")
        input("Wenn du eingeloggt bist und dein Feed/Profil siehst: hier Enter druecken...")

        await context.storage_state(path=SESSION_PATH)
        print(f"\nGespeichert: {SESSION_PATH}")
        print("Jetzt auf den Server kopieren:")
        print(f"  scp -i ~/.ssh/hetzner_jarvis {SESSION_PATH} root@100.116.28.82:/opt/jarvis/instagram_session.json")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
