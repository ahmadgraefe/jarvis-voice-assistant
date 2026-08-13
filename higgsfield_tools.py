"""
Jarvis V2 — Higgsfield AI-Generierung (Luna-Vale-Content)

Wrappt das offizielle `higgsfield` CLI (https://github.com/higgsfield-ai/cli),
NICHT die rohe REST-API direkt -- das CLI uebernimmt Auth, Retries, Polling
und Schema-Validierung selbst (siehe higgsfield-ai/skills CLAUDE.md: "Do not
call api.higgsfield.ai directly with curl. Skipping it bypasses critical
behavior."). Muss auf jeder Maschine, die dieses Modul nutzt, installiert
sein: `curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh`.

Auth laeuft NICHT ueber die Umgebungsvariablen HF_API_KEY_ID/HF_API_KEY_SECRET
-- das sieht in der Doku nach dem richtigen headless-Weg aus, ist aber im
CLI (Version 1.1.23, Stand 2026-08-13) nachweislich kaputt: `account status`
und `generate create` schlagen damit reproduzierbar fehl ("request failed,
no response received" bzw. "Not authenticated"), und zwar auf BEIDEN
Plattformen (Mac UND Server, in sauber isolierten $HOME-Verzeichnissen
verifiziert) -- kein Server-/DNS-/Netzwerk-Problem, ein echter CLI-Bug bei
reinem API-Key-Login.

Stattdessen: die interaktive Browser-Login-Sitzung (`higgsfield auth login`,
einmalig auf dem Mac gemacht) einfach auf den Server kopieren --
`~/.config/higgsfield/credentials.json` UND `~/.config/higgsfield/config.json`
(Workspace-Auswahl). Gleiches Prinzip wie bei sheets_token.json fuer Google
Sheets: das CLI benutzt dann ganz normal die kopierte Session, kein
Unterschied zu einem echten `auth login` auf der Zielmaschine. Enthaelt
einen `refresh_token`, sollte sich also selbst erneuern; falls die Session
trotzdem irgendwann ablaeuft (Symptom: "Session expired" oder wieder "Not
authenticated"), auf dem Mac neu `higgsfield auth login` ausfuehren und
beide Dateien erneut auf den Server kopieren.

Pipeline fuer Outfit-Transition-Videos (2026-08-13 aus der echten Job-
Historie rekonstruiert, siehe claude_app_status.md "Higgsfield-Produktion"):
1. Standbild von Luna im Zieloutfit (separat erzeugt, z.B. Nano Banana Pro,
   hier NICHT abgedeckt -- dieses Modul deckt bisher nur den zweiten,
   produktiv bestaetigten Schritt ab).
2. generate_transition_video(): dieses Standbild + ein fremdes
   Bewegungsvideo (nur die Choreo, nicht das Aussehen) durch
   kling3_0_motion_control -- Luna fuehrt die Bewegung im Zieloutfit aus.

Bewusst OHNE Soul-Character-Nutzung: live getestet und verworfen (siehe
claude_app_status.md) -- die produktiv bewaehrte Methode ist die Bild+Video-
Pipeline oben, kein trainiertes Soul-Modell.

Bewusst NIE sexualisierende Anweisungssprache in eigenen Prompts (Ahmad,
2026-08-12/13, mehrfach bestaetigt: "niemals NSFW auf Higgsfield").
"""

import asyncio
import json
import os
import time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
# Server (2026-08-13): ~/Library/Logs existiert auf dem Linux-Server nicht.
LOG_PATH = (
    "/var/log/jarvis-higgsfield.log" if os.environ.get("JARVIS_ROLE") == "server"
    else os.path.expanduser("~/Library/Logs/jarvis-higgsfield.log")
)

CLI_TIMEOUT_SECONDS = 650  # etwas ueber dem CLI-eigenen --wait-timeout von 10m


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


HIGGSFIELD_CREDENTIALS_PATH = os.path.expanduser("~/.config/higgsfield/credentials.json")


async def _run_cli(args: list, config: dict, timeout: int = CLI_TIMEOUT_SECONDS) -> dict:
    """Fuehrt `higgsfield <args> --json` aus, parst die JSON-Ausgabe.
    Rueckgabe immer ein dict: {"ok": True, "data": ...} oder
    {"ok": False, "error": "..."} -- Aufrufer muellen sich nie mit
    Subprocess-/JSON-Details herum. `config` wird hier (noch) nicht fuer
    Auth gebraucht -- die kommt aus der kopierten `auth login`-Sitzung, siehe
    Modul-Docstring -- bleibt als Parameter fuer eine spaetere echte
    API-Key-Unterstuetzung, sobald der CLI-Bug behoben ist."""
    if not os.path.exists(HIGGSFIELD_CREDENTIALS_PATH):
        return {
            "ok": False,
            "error": (
                f"Keine Higgsfield-Login-Sitzung gefunden ({HIGGSFIELD_CREDENTIALS_PATH}). "
                "Auf dem Mac `higgsfield auth login` ausfuehren und credentials.json + "
                "config.json aus ~/.config/higgsfield/ hierher kopieren."
            ),
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            "higgsfield", *args, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "higgsfield-CLI nicht installiert oder nicht auf PATH."}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": f"Timeout nach {timeout}s."}

    stdout_text, stderr_text = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    if proc.returncode != 0:
        detail = stderr_text.strip() or stdout_text.strip() or f"Exit-Code {proc.returncode}"
        _log(f"FEHLER higgsfield {' '.join(args)}: {detail}")
        return {"ok": False, "error": detail}

    try:
        return {"ok": True, "data": json.loads(stdout_text)}
    except json.JSONDecodeError:
        _log(f"FEHLER beim Parsen der CLI-Ausgabe fuer {' '.join(args)}: {stdout_text[:300]!r}")
        return {"ok": False, "error": f"Konnte CLI-Ausgabe nicht parsen: {stdout_text[:300]!r}"}


async def generate_transition_video(image_ref: str, motion_video_ref: str, mode: str = "std") -> dict:
    """Kling 3.0 Motion Control: EIN Standbild (Lunas Zieloutfit) + EIN
    Bewegungsvideo (fremde Person, nur die Choreo) -> Luna fuehrt dieselbe
    Bewegung im Zieloutfit aus. Beide Referenzen akzeptieren einen lokalen
    Dateipfad ODER eine bereits hochgeladene UUID (`higgsfield upload
    create`-Ergebnis oder eine fruehere Job-ID) -- das CLI laedt lokale
    Pfade automatisch hoch. `mode`: "std" (Standard) oder "pro" (teurer,
    hoehere Qualitaet)."""
    result = await _run_cli(
        [
            "generate", "create", "kling3_0_motion_control",
            "--image-references", image_ref,
            "--video-references", motion_video_ref,
            "--mode", mode,
            "--wait", "--wait-timeout", "10m",
        ],
        _load_config(),
    )
    if not result["ok"]:
        return result

    jobs = result["data"]
    job = jobs[0] if isinstance(jobs, list) and jobs else jobs
    if not isinstance(job, dict) or job.get("status") != "completed":
        status = job.get("status") if isinstance(job, dict) else "unbekannt"
        return {"ok": False, "error": f"Job nicht erfolgreich abgeschlossen (Status: {status})."}

    _log(f"Transition-Video erstellt: {job.get('id')} -> {job.get('result_url')}")
    return {"ok": True, "job_id": job.get("id"), "result_url": job.get("result_url")}


async def get_account_status() -> dict:
    """Fuer einen schnellen Gesundheitscheck (Auth ok? Workspace richtig?
    genug Guthaben?) -- z.B. bevor eine groessere Content-Pass startet."""
    result = await _run_cli(["account", "status"], _load_config(), timeout=30)
    if not result["ok"]:
        return result
    return {"ok": True, **result["data"]} if isinstance(result["data"], dict) else result
