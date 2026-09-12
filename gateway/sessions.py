"""Memoria conversacional por chat de Telegram.

agy devuelve un `conversation_id` y lo reanuda con --conversation. Guardamos el
mapa chat_id -> conversation_id: eso reemplaza al Window Buffer Memory de n8n.
Como el input crece en cada turno y no hay cache, reciclamos la conversación
cuando se hace larga.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config

_FILE: Path = config.STATE_DIR / "sessions.json"
_lock = threading.Lock()


def _read() -> dict:
    if not _FILE.exists():
        return {}
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write(data: dict) -> None:
    tmp = _FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_FILE)


def get(chat_id: str) -> dict:
    with _lock:
        return _read().get(str(chat_id), {"conversation_id": None, "turns": 0})


def remember(chat_id: str, conversation_id: str) -> None:
    with _lock:
        data = _read()
        key = str(chat_id)
        entry = data.get(key, {"turns": 0})
        if entry.get("conversation_id") != conversation_id:
            entry["turns"] = 0
        entry["conversation_id"] = conversation_id
        entry["turns"] = entry.get("turns", 0) + 1
        data[key] = entry
        _write(data)


def reset(chat_id: str) -> None:
    with _lock:
        data = _read()
        data.pop(str(chat_id), None)
        _write(data)


def should_recycle(chat_id: str) -> bool:
    return get(chat_id).get("turns", 0) >= config.ORCHESTRATOR_MAX_TURNS
