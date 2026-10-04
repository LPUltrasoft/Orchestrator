"""Progreso en vivo en Telegram: qué agente está trabajando y qué está haciendo.

Es un solo mensaje que se va editando (editMessageText), para no llenar el chat ni
hacer sonar el celular con cada paso. La respuesta final llega aparte, como mensaje
nuevo, justamente para que sí notifique.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import PurePath

import httpx

from . import telegram

log = logging.getLogger("orchestrator.progress")

AGENTS = {
    "orchestrator": "🧭 Director",
    "producto": "📋 Producto",
    "dba": "🗄️ DBA",
    "nestjs": "🧱 NestJS",
}
MAX_ACTIONS = 4  # acciones visibles por agente: el mensaje tiene que entrar en pantalla
EDIT_EVERY = 3.0  # Telegram limita cuántas veces por segundo se edita un mensaje
TICK_EVERY = 10.0  # refresco del reloj aunque el agente esté pensando sin eventos


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _doc(params: dict, *keys: str) -> str:
    """Nombre legible del archivo o carpeta que toca una herramienta."""
    value = next((params[k] for k in keys if params.get(k)), None)
    if value is None:
        value = next((v for v in params.values() if isinstance(v, str) and v), "")
    name = PurePath(value).name or value
    name = name.removesuffix(".md")
    return name if len(name) <= 45 else name[:44] + "…"


def describe(step: dict) -> str | None:
    """Traduce un paso de agy a una línea legible, o None si no aporta nada."""
    if step.get("synthetic") == "retry":
        return f"↻ acción no permitida ({step.get('detail', '')[:40]}), reintenta"
    if step.get("step_type") != "tool" or step.get("state") != "ACTIVE":
        return None
    tool = step.get("tool_name", "")
    params = (step.get("tool_info") or {}).get("parameters") or {}
    if tool == "view_file":
        return f"📖 lee {_doc(params, 'AbsolutePath')}"
    if tool == "list_dir":
        return f"📂 mira {_doc(params, 'DirectoryPath', 'AbsolutePath')}"
    if tool in ("grep_search", "find_by_name"):
        query = params.get("Query") or params.get("Pattern") or ""
        return f"🔎 busca «{query[:30]}»"
    if tool == "write_to_file":
        return f"✍️ escribe {_doc(params, 'TargetFile')}"
    if tool in ("replace_file_content", "multi_replace_file_content", "sed_file"):
        return f"✏️ edita {_doc(params, 'TargetFile', 'AbsolutePath')}"
    if tool == "run_command":
        return _describe_command((params.get("CommandLine") or "").strip())
    if tool in ("search_web", "read_url_content"):
        return "🌐 consulta la web"
    return f"🔧 {tool}"


def _describe_command(command: str) -> str | None:
    """Los comandos permitidos, en castellano. `which` y la ayuda son ruido."""
    try:
        parts = shlex.split(command)  # respeta rutas con espacios entre comillas
    except ValueError:
        parts = command.split()  # comillas desbalanceadas: mejor algo que nada
    if not parts:
        return None
    name = parts[0]
    args = [a for a in parts[1:] if not a.startswith("-")]
    target = PurePath(args[-1]).name.removesuffix(".md") if args else ""
    if name == "orc-delegate":
        role = args[0] if args else ""
        return f"↪ delega en {AGENTS[role]}" if role in AGENTS else None
    if name == "which":
        return None
    if name == "ls":
        return f"📂 mira {target or 'la carpeta'}"
    if name in ("cat", "head"):
        return f"📖 lee {target}"
    if name == "grep":
        return f"🔎 busca «{args[0][:30]}»" if args else "🔎 busca"
    if name == "git":
        return "🕘 revisa el historial del vault"
    return f"⚙️ {command[:40]}"


@dataclass
class Section:
    agent: str
    project: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    ok: bool = True
    outcome: str | None = None
    actions: list[str] = field(default_factory=list)


class Progress:
    def __init__(self, bot: telegram.TelegramBot, chat_id: str) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.started = time.monotonic()
        self.finished: float | None = None
        self.ok = True
        self.sections: list[Section] = []
        self._message_id: int | None = None
        self._last_text = ""
        self._dirty = True
        self._loop_task: asyncio.Task | None = None

    # ── eventos ──────────────────────────────────────────────────────────────

    def section(self, agent: str, project: str | None = None) -> Section:
        """La sección abierta de ese agente, o una nueva."""
        for existing in reversed(self.sections):
            if existing.agent == agent and existing.finished is None:
                if project:
                    existing.project = project
                return existing
        created = Section(agent, project)
        self.sections.append(created)
        self._dirty = True
        return created

    def step(self, agent: str, step: dict) -> None:
        line = describe(step)
        if not line:
            return
        section = self.section(agent)
        if section.actions and section.actions[-1] == line:
            return
        section.actions.append(line)
        del section.actions[:-MAX_ACTIONS]
        self._dirty = True

    def agent_done(self, agent: str, ok: bool, outcome: str | None = None) -> None:
        section = self.section(agent)
        section.finished = time.monotonic()
        section.ok = ok
        section.outcome = outcome
        self._dirty = True

    # ── ciclo de vida ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.section("orchestrator")
        await self._render()
        self._loop_task = asyncio.create_task(self._loop())

    async def finish(self, ok: bool) -> None:
        self.ok = ok
        self.finished = time.monotonic()
        for section in self.sections:
            if section.finished is None:
                section.finished = self.finished
                section.ok = section.ok and ok
        if self._loop_task:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
        await self._render()

    # ── render ───────────────────────────────────────────────────────────────

    def text(self) -> str:
        now = time.monotonic()
        total = _clock((self.finished or now) - self.started)
        if self.finished is None:
            lines = [f"⏳ Trabajando… {total}"]
        elif self.ok:
            lines = [f"✅ Listo en {total}"]
        else:
            lines = [f"⚠️ Terminó con error a los {total}"]

        for section in self.sections:
            icon = "⏳" if section.finished is None else ("✅" if section.ok else "⚠️")
            title = f"{icon} {AGENTS.get(section.agent, section.agent)}"
            if section.project:
                title += f" · {section.project}"
            title += f" · {_clock((section.finished or now) - section.started)}"
            lines += ["", title]
            if section.outcome:
                lines.append(f"   {section.outcome}")
            else:
                lines += [f"   {action}" for action in section.actions]
        return "\n".join(lines)[:4000]

    async def _render(self) -> None:
        text = self.text()
        if text == self._last_text:
            return
        try:
            if self._message_id is None:
                sent = await self.bot.call("sendMessage", chat_id=self.chat_id, text=text)
                self._message_id = sent["message_id"]
            else:
                await self.bot.call(
                    "editMessageText",
                    chat_id=self.chat_id,
                    message_id=self._message_id,
                    text=text,
                )
            self._last_text = text
        except (httpx.HTTPError, telegram.TelegramError, ValueError, KeyError) as exc:
            if "not modified" not in str(exc):
                log.warning("no pude actualizar el progreso en Telegram: %s", exc)

    async def _loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(EDIT_EVERY)
            now = time.monotonic()
            if self._dirty or now - last >= TICK_EVERY:
                self._dirty = False
                last = now
                await self._render()
