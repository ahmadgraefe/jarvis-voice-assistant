#!/usr/bin/env python3
"""
Jarvis — Double Clap Trigger
Listens to mic. Detects two claps within 1.2s, min 0.1s apart.
On trigger: runs scripts/launch-session.sh, then keeps listening for the next one.
"""

import sounddevice as sd
import numpy as np
import subprocess
import time
import os
import json

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

WORKSPACE_PATH = config["workspace_path"]
SCRIPT_PATH = os.path.join(WORKSPACE_PATH, "scripts", "launch-session.sh")

SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
THRESHOLD = 0.15       # RMS volume spike threshold — lower = more sensitive
MIN_GAP = 0.1          # Minimum seconds between claps
MAX_GAP = 1.2          # Maximum seconds between claps — more time for second clap
COOLDOWN = 3.0         # Seconds to ignore after trigger fires

# Ahmad (2026-08-07): "manchmal öffnet er sich ohne das klatschen" — a bare
# RMS threshold can't tell a real clap from any other loud sound in the same
# volume range, including his own raised/sharp speech. A real clap is a
# short, impulsive transient — very high PEAK relative to the block's RMS
# ("spiky"). Sustained loud speech has a much flatter envelope even at the
# same average volume, so the peak/RMS ratio (crest factor) is a cheap,
# no-extra-dependency way to reject most voice false-positives without a
# real classifier. Ahmad should still test after restart and report back if
# it needs tuning further — can't physically clap to verify this myself.
CREST_FACTOR_MIN = 3.5

last_clap_time = 0.0
cooldown_until = 0.0

def audio_callback(indata, frames, time_info, status):
    global last_clap_time, cooldown_until

    now = time.time()
    if now < cooldown_until:
        return

    samples = indata[:, 0]
    rms = float(np.sqrt(np.mean(samples ** 2)))

    if rms > THRESHOLD:
        peak = float(np.max(np.abs(samples)))
        crest_factor = peak / (rms + 1e-9)
        if crest_factor < CREST_FACTOR_MIN:
            return  # loud, but not clap-shaped (e.g. sustained speech) — ignore

        gap = now - last_clap_time

        if gap >= MIN_GAP:
            if gap <= MAX_GAP and last_clap_time > 0:
                # Second clap — fire trigger, cool down, keep listening
                print(f"[jarvis] Double clap detected! (rms={rms:.3f}, crest={crest_factor:.1f}) Firing launch script.", flush=True)
                last_clap_time = 0.0
                cooldown_until = now + COOLDOWN
                subprocess.Popen(["/bin/bash", SCRIPT_PATH])
            else:
                # First clap
                print(f"[jarvis] First clap detected (rms={rms:.3f}, crest={crest_factor:.1f})", flush=True)
                last_clap_time = now

with sd.InputStream(
    samplerate=SAMPLE_RATE,
    blocksize=BLOCK_SIZE,
    channels=1,
    dtype="float32",
    callback=audio_callback,
):
    print("[jarvis] Listening for double clap...", flush=True)
    while True:
        time.sleep(0.1)
