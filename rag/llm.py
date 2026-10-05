"""
LLM wrapper - local only, NO API keys.

Uses an open-source model served by Ollama on this computer (default: mistral, at http://127.0.0.1:11434).
If Ollama is not running, available() is False and the engine answers in "extractive" mode
(a template filled with the same SQL numbers and news citations), so the app always works.

    ollama pull mistral        # once
    (Ollama runs in the background after install; or start it with:  ollama serve)

Settings (optional, in .env):  OLLAMA_URL=http://127.0.0.1:11434   OLLAMA_MODEL=mistral
"""

import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("llm")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")
_status = {"checked": 0.0, "ok": False}


def available():
    """True if Ollama is up and has the model. Re-checked at most every 60 s."""
    if time.time() - _status["checked"] > 60:
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2).json()
            names = {m["name"].split(":")[0] for m in tags.get("models", [])}
            _status["ok"] = OLLAMA_MODEL.split(":")[0] in names
        except Exception:
            _status["ok"] = False
        _status["checked"] = time.time()
    return _status["ok"]


def describe():
    return f"local {OLLAMA_MODEL} via Ollama (no API key)" if available() else \
        "extractive mode (start Ollama for written answers)"


def chat(system, user, max_tokens=900):
    """One system + user message -> reply text ('' on failure, so the caller falls back)."""
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=300, json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.1, "num_predict": max_tokens, "num_ctx": 8192},
        })
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception as e:
        log.warning("Ollama call failed: %s", e)
        return ""
