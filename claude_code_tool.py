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
import time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/jarvis-claudecode.log")
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
WORKDIR = os.path.dirname(__file__)

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


async def run_claude_code(task: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run one non-interactive Claude Code task, scoped to this project's
    directory, with full tool access (file edits, shell commands). Returns
    its final text output, or 'ERROR: ...' on failure/timeout."""
    config = _load_config()
    env = {**os.environ, "ANTHROPIC_API_KEY": config["anthropic_api_key"]}

    _log(f"START: {task[:200]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "-p", task, "--dangerously-skip-permissions",
            cwd=WORKDIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        _log(f"TIMEOUT nach {timeout}s: {task[:200]}")
        return f"ERROR: Claude Code Aufgabe hat das Zeitlimit ({timeout}s) ueberschritten."
    except Exception as e:
        _log(f"FEHLER beim Start: {e}")
        return f"ERROR: Claude Code konnte nicht gestartet werden: {e}"

    output = stdout.decode(errors="replace").strip()
    error_output = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        _log(f"FEHLER (exit {proc.returncode}): {error_output[:300]}")
        return f"ERROR: Claude Code Aufgabe fehlgeschlagen: {error_output[:500] or 'unbekannter Fehler'}"

    _log(f"FERTIG: {output[:200]}")
    return output or "Claude Code hat die Aufgabe ohne Textausgabe abgeschlossen."
