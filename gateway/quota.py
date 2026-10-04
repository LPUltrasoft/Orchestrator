"""Cuota de agy: cuánto queda de cada ventana y cuándo se renueva.

`agy -p "/quota"` informa, por familia de modelos, la ventana de 5 horas y la
semanal: una línea por ventana, separada por tabs.

    Gemini Models	Five Hour Limit Remaining	98%	2026-10-04T08:22:38Z

Consultarla no consume cuota (verificado: tres consultas seguidas dieron 98, 99 y 98).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import config

log = logging.getLogger("orchestrator.quota")

# Una consulta por minuto alcanza: la cuota se mueve con los turnos, que duran minutos.
CACHE_SECONDS = 60
# Para pruebas: leer la salida de /quota de un archivo en lugar de llamar a agy.
_FAKE_FILE = os.environ.get("ORC_QUOTA_FILE")
_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


@dataclass(frozen=True)
class Window:
    family: str  # "Gemini Models"
    name: str  # "Five Hour Limit Remaining"
    remaining: int  # porcentaje que queda, 0 a 100
    resets_at: datetime

    @property
    def label(self) -> str:
        if "five hour" in self.name.lower():
            return "de 5 horas"
        if "weekly" in self.name.lower():
            return "semanal"
        return self.name

    @property
    def family_label(self) -> str:
        return self.family.removesuffix(" Models").removesuffix(" models")


_cache: tuple[float, list[Window]] | None = None


def family_for(model: str) -> str:
    return "Gemini Models" if model.startswith("gemini") else "Claude and GPT models"


def parse(text: str) -> list[Window]:
    windows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) != 4 or not parts[2].endswith("%"):
            continue
        try:
            windows.append(Window(
                family=parts[0],
                name=parts[1],
                remaining=int(parts[2].rstrip("%")),
                resets_at=datetime.fromisoformat(parts[3].replace("Z", "+00:00")),
            ))
        except ValueError:
            log.warning("línea de /quota que no entiendo: %r", line)
    return windows


async def read(force: bool = False) -> list[Window]:
    """Ventanas de cuota actuales. Si agy no responde, lista vacía: no bloquea."""
    global _cache
    if not force and _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    if _FAKE_FILE:
        with open(_FAKE_FILE, encoding="utf-8") as fh:
            text = fh.read()
    else:
        try:
            process = await asyncio.create_subprocess_exec(
                config.AGY_BIN, "-p", "/quota",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=60)
            text = raw.decode("utf-8", "replace")
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("no pude consultar la cuota de agy: %s", exc)
            return []
    windows = parse(text)
    if not windows:
        log.warning("/quota no devolvió ventanas: %r", text[:200])
    _cache = (time.monotonic(), windows)
    return windows


async def exhausted(models: list[str] | set[str], force: bool = False) -> list[Window]:
    """Ventanas agotadas (al umbral o menos) de las familias que usan esos modelos."""
    families = {family_for(m) for m in models}
    return [
        w for w in await read(force)
        if w.family in families and w.remaining <= config.QUOTA_MIN_REMAINING
    ]


def resume_at(windows: list[Window]) -> datetime:
    """Cuándo se puede seguir: la renovación más lejana entre las ventanas agotadas."""
    return max(w.resets_at for w in windows)


def when(moment: datetime) -> str:
    """'hoy a las 05:22', 'mañana a las 05:22' o 'el sábado 10/10 a las 19:22'."""
    local = moment.astimezone()
    today = datetime.now().astimezone().date()
    hour = local.strftime("%H:%M")
    if local.date() == today:
        return f"hoy a las {hour}"
    if local.date() == today + timedelta(days=1):
        return f"mañana a las {hour}"
    return f"el {_WEEKDAYS[local.weekday()]} {local:%d/%m} a las {hour}"


def describe(windows: list[Window]) -> str:
    """'la cuota de 5 horas de Gemini (queda 4%)'"""
    parts = [f"la cuota {w.label} de {w.family_label} (queda {w.remaining}%)" for w in windows]
    return " y ".join(parts)
