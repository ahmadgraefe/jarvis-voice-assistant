"""
Jarvis V2 — Claude Code Execution
Lets Jarvis outsource programming/research tasks to Claude Code itself:
non-interactive, full tool access (--dangerously-skip-permissions), scoped to
this project's directory. Ahmad explicitly authorized full autonomous access
(including unsupervised background use) after being walked through the risk
tradeoff — every invocation is logged, and background-triggered runs also
get a WhatsApp notification so he stays informed even when he wasn't there
to watch it happen.
"""

import asyncio
import json
import os
import shutil
import time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
WORKDIR = os.path.dirname(__file__)

# Server (Linux, laeuft als root): CLI-Aufruf muss auf einen eingeschraenkten
# User ausweichen, --dangerously-skip-permissions verweigert sich sonst
# (2026-08-09, "cannot be used with root/sudo privileges"). Mac: normaler
# User, kein sudo-Umweg noetig. CLAUDE_BIN per PATH-Lookup statt hartem Pfad,
# da Server (npm global, /usr/bin/claude) und Mac (~/.local/bin/claude)
# unterschiedliche Installationsorte haben.
IS_SERVER = os.environ.get("JARVIS_ROLE") == "server"
CLAUDE_CODE_USER = "jarviscode"
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
LOG_PATH = (
    os.path.join(WORKDIR, "jarvis-claudecode.log") if IS_SERVER
    else os.path.expanduser("~/Library/Logs/jarvis-claudecode.log")
)

DEFAULT_TIMEOUT = 600  # 10 min — real coding/research tasks take real time


def _load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _log(msg: str):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


async def _git_sync_server_changes(task: str) -> str:
    """Server-seitig (2026-08-10): Claude Code arbeitet auf /opt/jarvis, einer
    vom Mac getrennten Projekt-Kopie. Ohne das hier wuerden Aenderungen dort
    still liegen bleiben und von Ahmads naechstem Deploy ueberschrieben werden
    koennen. Committed und pusht automatisch zum 'origin'-Remote (Deploy-Key
    braucht Schreibzugriff, siehe README/UEBERGABE), damit Ahmad sie per
    'git pull' auf dem Mac abholen kann. Kein Push noetig/sinnvoll wenn nichts
    geaendert wurde. Gibt den neuen Commit-Hash zurueck (Roadmap Punkt 22,
    fuers Self-Improve-Changelog), oder None wenn nichts committet wurde."""
    try:
        status = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain", cwd=WORKDIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        status_out, _ = await status.communicate()
        if not status_out.strip():
            _log("GIT-SYNC: keine Aenderungen, nichts zu committen.")
            return None

        commit_msg = f"Claude Code (Server): {task[:100]}"
        for cmd in (
            ["git", "add", "-A"],
            ["git", "commit", "-m", commit_msg],
            ["git", "push", "origin", "master"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=WORKDIR,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                _log(f"GIT-SYNC FEHLER bei '{' '.join(cmd)}': {err.decode(errors='replace')[:300]}")
                return None

        rev = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD", cwd=WORKDIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        rev_out, _ = await rev.communicate()
        commit_hash = rev_out.decode().strip() or None
        _log(f"GIT-SYNC: Aenderungen committed und gepusht ({commit_hash}).")
        return commit_hash
    except Exception as e:
        _log(f"GIT-SYNC FEHLER: {e}")
        return None


async def run_claude_code_with_commit(task: str, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Wie run_claude_code(), gibt zusaetzlich den Commit-Hash zurueck (str
    oder None), falls die Aufgabe server-seitig etwas committet+gepusht hat —
    fuer Roadmap Punkt 22 (Self-Improve-Changelog). Enthaelt die volle Logik,
    run_claude_code() ist nur noch ein duenner Wrapper darum, damit bestehende
    Aufrufer (z.B. server.py's claude_code_exec-Tool) unveraendert bleiben."""
    config = _load_config()

    if IS_SERVER:
        cmd = [
            "sudo", "-u", CLAUDE_CODE_USER, "-H",
            "env", f"ANTHROPIC_API_KEY={config['anthropic_api_key']}",
            CLAUDE_BIN, "-p", task, "--dangerously-skip-permissions",
        ]
        env = os.environ.copy()
    else:
        cmd = [CLAUDE_BIN, "-p", task, "--dangerously-skip-permissions"]
        env = {**os.environ, "ANTHROPIC_API_KEY": config["anthropic_api_key"]}

    _log(f"START: {task[:200]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=WORKDIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        _log(f"TIMEOUT nach {timeout}s: {task[:200]}")
        return f"ERROR: Claude Code Aufgabe hat das Zeitlimit ({timeout}s) ueberschritten.", None
    except Exception as e:
        _log(f"FEHLER beim Start: {e}")
        return f"ERROR: Claude Code konnte nicht gestartet werden: {e}", None

    output = stdout.decode(errors="replace").strip()
    error_output = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        _log(f"FEHLER (exit {proc.returncode}): {error_output[:300]}")
        return f"ERROR: Claude Code Aufgabe fehlgeschlagen: {error_output[:500] or 'unbekannter Fehler'}", None

    _log(f"FERTIG: {output[:200]}")

    commit_hash = None
    if IS_SERVER:
        commit_hash = await _git_sync_server_changes(task)

    return output or "Claude Code hat die Aufgabe ohne Textausgabe abgeschlossen.", commit_hash


async def run_claude_code(task: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run one non-interactive Claude Code task, scoped to this project's
    directory, with full tool access (file edits, shell commands). Returns
    its final text output, or 'ERROR: ...' on failure/timeout."""
    output, _ = await run_claude_code_with_commit(task, timeout)
    return output
