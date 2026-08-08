"""
Jarvis V2 — Semantische Suche im Langzeitgedaechtnis (Tier 2, Punkt 9).

Ersetzt das feste Kategorien-Budget aus memory.get_longterm_memory() fuer den
Fall, dass eine echte Nutzerfrage vorliegt: statt jeder Kategorie einen festen
Zeichenanteil zu geben (unabhaengig davon ob sie gerade relevant ist), werden
alle Fakten gegen die aktuelle Frage per Embedding-Aehnlichkeit sortiert.

Laeuft komplett lokal (sentence-transformers, kein API-Key, keine Kosten,
Ahmads Daten verlassen nie den Mac — 2026-08-08, Ahmads ausdrueckliche Wahl
gegenueber einer Cloud-Embedding-API). Der Import von sentence_transformers
passiert bewusst NUR lazy innerhalb der Funktionen, nicht auf Modulebene —
falls Modell/Installation je Probleme machen, darf server.py trotzdem
starten und antworten, siehe search_longterm_memory()'s Fallback.
"""

import hashlib
import json
import os

import memory

EMBEDDING_CACHE_PATH = os.path.join(memory.MEMORY_DIR, "embedding_cache.json")
# Mehrsprachig statt all-MiniLM-L6-v2 (rein englisch trainiert) — Ahmads
# Langzeitgedaechtnis-Fakten sind fast ausschliesslich Deutsch. Direkt
# getestet: bei all-MiniLM-L6-v2 lagen alle Cosine-Similarities fuer
# deutsche Saetze eng beieinander (0.32-0.47, kaum Trennschaerfe, ein
# voellig unpassender Fakt landete fast gleichauf mit dem relevanten).
# paraphrase-multilingual-MiniLM-L12-v2 zeigte im selben Test klare
# Trennung zwischen relevant und irrelevant.
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

_model = None
_cache = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(EMBEDDING_CACHE_PATH):
        try:
            with open(EMBEDDING_CACHE_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
                return _cache
        except (json.JSONDecodeError, OSError):
            pass
    _cache = {}
    return _cache


def _save_cache():
    with open(EMBEDDING_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache, f)


def _embed(text: str) -> list:
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache = _load_cache()
    if key in cache:
        return cache[key]
    vec = _get_model().encode(text).tolist()
    cache[key] = vec
    _save_cache()
    return vec


def _cosine_sim(a: list, b: list) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def search_longterm_memory(query: str, max_chars: int = 6000) -> str:
    """Relevanz-sortierte Fakten ueber ALLE Kategorien hinweg statt eines
    festen Budgets pro Kategorie. Ohne echte Frage (leer/whitespace, z.B.
    das volle 'lets go'-Briefing) oder bei irgendeinem Fehler (Modell laedt
    nicht, Cache kaputt, etc.) faellt das auf die alte, rezenz-basierte
    memory.get_longterm_memory() zurueck — eine kaputte Embedding-Pipeline
    darf niemals die Kernantwort-Schleife brechen."""
    if not query or not query.strip():
        return memory.get_longterm_memory(max_chars)

    try:
        entries = memory.get_all_profile_entries()
        if not entries:
            return ""

        query_vec = _embed(query)
        scored = []
        for e in entries:
            sim = _cosine_sim(query_vec, _embed(e["text"]))
            scored.append((sim, e))
        scored.sort(key=lambda x: x[0], reverse=True)

        lines = []
        total = 0
        for sim, e in scored:
            label = memory.CATEGORY_LABELS.get(e["category"], e["category"])
            line = f"- [{e['added']}] ({label}) {e['text']}"
            total += len(line) + 1
            if total > max_chars and lines:
                break
            lines.append(line)

        return "\n".join(lines)
    except Exception as ex:
        print(f"  semantic_memory: Fallback auf get_longterm_memory ({ex})", flush=True)
        return memory.get_longterm_memory(max_chars)
