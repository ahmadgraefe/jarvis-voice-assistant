#!/usr/bin/env python3
"""
Jarvis — "Hey Jarvis" Wake-Word Trigger
Ahmad, 2026-08-11: soll zusaetzlich zum Doppelklatschen auf "Hey Jarvis"
reagieren, nicht als Ersatz. Laeuft als eigener, unabhaengiger Dienst
neben clap-trigger.py, beide loesen dasselbe launch-session.sh aus.

Nutzt openWakeWord (komplett lokal, ONNX, kein API-Key, keine Kosten,
kein Audio verlaesst den Mac -- gleiche Philosophie wie das lokale
Embedding-Modell in semantic_memory.py) mit dessen mitgeliefertem,
vortrainiertem "hey_jarvis"-Modell -- kein eigenes Training noetig, der
Name passt zufaellig exakt.
"""

import sounddevice as sd
import numpy as np
import subprocess
import time
import os
import json

from openwakeword.model import Model

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

WORKSPACE_PATH = config["workspace_path"]
SCRIPT_PATH = os.path.join(WORKSPACE_PATH, "scripts", "launch-session.sh")

SAMPLE_RATE = 16000  # openWakeWord erwartet exakt 16kHz, anders als der 44.1kHz-Klatsch-Trigger
BLOCK_SIZE = 1280    # 80ms bei 16kHz, openWakeWord's empfohlene Chunk-Groesse
THRESHOLD = 0.5       # openWakeWord-Score ab dem ausgeloest wird (0 bis 1)
COOLDOWN = 3.0        # Sekunden nach einem Trigger ignorieren, gleiches Prinzip wie beim Klatsch-Trigger

oww_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
cooldown_until = 0.0


def audio_callback(indata, frames, time_info, status):
    global cooldown_until
    now = time.time()
    if now < cooldown_until:
        return

    audio = (indata[:, 0] * 32767).astype(np.int16)
    prediction = oww_model.predict(audio)
    score = prediction.get("hey_jarvis", 0.0)

    if score > THRESHOLD:
        print(f"[jarvis] 'Hey Jarvis' erkannt! (score={score:.3f}) Firing launch script.", flush=True)
        cooldown_until = now + COOLDOWN
        subprocess.Popen(["/bin/bash", SCRIPT_PATH])


with sd.InputStream(
    samplerate=SAMPLE_RATE,
    blocksize=BLOCK_SIZE,
    channels=1,
    dtype="float32",
    callback=audio_callback,
):
    print("[jarvis] Listening for 'Hey Jarvis'...", flush=True)
    while True:
        time.sleep(0.1)
