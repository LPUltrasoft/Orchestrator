"""Cola de trabajos en background.

Un turno del orquestador tarda minutos: Telegram y el nodo HTTP de n8n no
esperan tanto. Devolvemos job_id al instante y avisamos por callback al final.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

_jobs: dict[str, dict] = {}
_LOG = config.STATE_DIR / "jobs.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(kind: str, payload: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "id": job_id,
        "kind": kind,
        "status": "running",
        "payload": payload,
        "created_at": _now(),
        "result": None,
        "error": None,
    }
    return job_id


def finish(job_id: str, result: Any = None, error: str | None = None) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {}
    job["status"] = "error" if error else "done"
    job["result"] = result
    job["error"] = error
    job["finished_at"] = _now()
    with _LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(job, ensure_ascii=False) + "\n")
    return job


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def recent(limit: int = 20) -> list[dict]:
    return list(_jobs.values())[-limit:]
