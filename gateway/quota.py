"""Cuota de agy: cuánto queda de cada ventana y cuándo se renueva.

`agy -p "/quota"` informa, por familia de modelos, la ventana de 5 horas y la
semanal: una línea por ventana, separada por tabs.

    Gemini Models	Five Hour Limit Remaining	98%	2026-10-04T08:22:38Z

Consultarla no consume cuota (verificado: tres consultas seguidas dieron 98, 99 y 98).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("orchestrator.quota")

# Una consulta por minuto alcanza: la cuota se mueve con los turnos, que duran minutos.
CACHE_SECONDS = 60
# Para pruebas: leer la salida de /quota (agy) o /usage (claude) de un archivo.
_FAKE_FILE = os.environ.get("ORC_QUOTA_FILE")
_FAKE_CLAUDE_FILE = os.environ.get("ORC_CLAUDE_USAGE_FILE")
CLAUDE_FAMILY = "Claude Code"
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
# "Current session: 46% used · resets Oct 4, 6:10pm (America/Argentina/Buenos_Aires)"
# "Current week (all models): 22% used · resets Oct 8, 10pm (America/Argentina/Buenos_Aires)"
_CLAUDE_LINE = re.compile(
    r"Current (session|week)(?: \(([^)]*)\))?:\s*(\d+)% used"
    r"(?:\s*·\s*resets\s+([A-Z][a-z]{2}) (\d{1,2}),\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\))?"
)
_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


@dataclass(frozen=True)
class Window:
    family: str  # "Gemini Models"
    name: str  # "Five Hour Limit Remaining"
    remaining: int  # porcentaje que queda, 0 a 100
    resets_at: datetime

    @property
    def label(self) -> str:
        name = self.name.lower()
        if "five hour" in name:
            return "de 5 horas"
        if "weekly" in name:
            scope = re.search(r"\(([^)]*)\)", self.name)
            return f"semanal de {scope.group(1)}" if scope and scope.group(1) != "all models" else "semanal"
        return self.name

    @property
    def family_label(self) -> str:
        return self.family.removesuffix(" Models").removesuffix(" models")


_cache: tuple[float, list[Window]] | None = None
_claude_cache: tuple[float, list[Window]] | None = None


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


def parse_claude(text: str, now: datetime | None = None) -> list[Window]:
    """Las ventanas de `claude -p "/usage"`: la sesión (5 horas) y la semana."""
    now = now or datetime.now().astimezone()
    windows = []
    for kind, scope, used, month, day, hour, minute, ampm, tz in _CLAUDE_LINE.findall(text):
        name = "Five Hour Limit Remaining" if kind == "session" else f"Weekly Limit Remaining ({scope or 'all models'})"
        if month:
            zone = ZoneInfo(tz)
            hour24 = int(hour) % 12 + (12 if ampm == "pm" else 0)
            resets = datetime(now.year, _MONTHS[month], int(day), hour24, int(minute or 0), tzinfo=zone)
            if resets < now - timedelta(days=1):  # cruzó de año
                resets = resets.replace(year=now.year + 1)
        else:
            # Sin fecha de renovación: se vuelve a consultar en una hora.
            resets = now + timedelta(hours=1)
        windows.append(Window(CLAUDE_FAMILY, name, 100 - int(used), resets))
    return windows


async def read_claude(force: bool = False) -> list[Window]:
    """Cuota de la suscripción de Claude. Consultarla no gasta nada (0 turnos, 0 USD)."""
    global _claude_cache
    if not force and _claude_cache and time.monotonic() - _claude_cache[0] < CACHE_SECONDS:
        return _claude_cache[1]
    if _FAKE_CLAUDE_FILE:
        with open(_FAKE_CLAUDE_FILE, encoding="utf-8") as fh:
            text = fh.read()
    else:
        try:
            process = await asyncio.create_subprocess_exec(
                config.CLAUDE_BIN, "-p", "/usage", "--output-format", "json",
                "--safe-mode", "--setting-sources", "project", "--strict-mcp-config",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=60)
            text = json.loads(raw.decode("utf-8", "replace")).get("result", "")
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            log.warning("no pude consultar la cuota de Claude: %s", exc)
            return []
    windows = parse_claude(text)
    if not windows:
        log.warning("/usage de Claude no devolvió ventanas: %r", text[:200])
    _claude_cache = (time.monotonic(), windows)
    return windows


async def read_all(force: bool = False) -> list[Window]:
    """Todas las ventanas de los motores que se usan."""
    # Copia: `read` devuelve la lista del caché, y sumarle ventanas la ensuciaría.
    windows = list(await read(force))
    if "claude" in config.ENGINES.values():
        windows += await read_claude(force)
    return windows


async def exhausted(roles: list[str] | set[str], force: bool = False) -> list[Window]:
    """Ventanas agotadas de los motores que usan esos roles. Cada motor tiene su umbral:
    Claude se pausa antes porque comparte la suscripción con el uso propio del usuario."""
    low = []
    agy_roles = [r for r in roles if config.ENGINES[r] == "agy"]
    if agy_roles:
        families = {family_for(config.MODELS[r]) for r in agy_roles}
        low += [
            w for w in await read(force)
            if w.family in families and w.remaining <= config.QUOTA_MIN_REMAINING
        ]
    claude_models = [config.MODELS[r].lower() for r in roles if config.ENGINES[r] == "claude"]
    if claude_models:
        for w in await read_claude(force):
            if w.remaining > config.QUOTA_MIN_REMAINING_CLAUDE:
                continue
            # Una ventana propia de un modelo ("semanal de Opus") solo frena a ese modelo.
            scope = re.search(r"\(([^)]*)\)", w.name)
            model = scope.group(1).split()[0].lower() if scope and scope.group(1) != "all models" else ""
            if not model or any(model in m for m in claude_models):
                low.append(w)
    return low


_RESETS = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.I)


def from_limit_message(text: str, now: datetime | None = None) -> Window | None:
    """La ventana agotada que nombra un mensaje de Claude como "You've hit your session
    limit · resets 4:30am (America/Argentina/Buenos_Aires)": la próxima vez que sean esa hora."""
    match = _RESETS.search(text or "")
    if not match:
        return None
    hour, minute, ampm, tz = match.groups()
    zone = ZoneInfo(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    resets = now.replace(hour=int(hour) % 12 + (12 if ampm.lower() == "pm" else 0),
                         minute=int(minute or 0), second=0, microsecond=0)
    if resets <= now:
        resets += timedelta(days=1)
    name = "Weekly Limit Remaining (all models)" if "weekly" in text.lower() else "Five Hour Limit Remaining"
    return Window(CLAUDE_FAMILY, name, 0, resets)


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
