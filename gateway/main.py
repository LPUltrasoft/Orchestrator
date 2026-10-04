"""Gateway: recibe mensajes de Telegram y los pone a trabajar a los agentes `agy`.

Telegram entra por long polling (telegram.py). El endpoint POST /chat queda para
pruebas y para la alternativa con n8n.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config, jobs, progress, quota, runner, sessions, telegram, vault

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("orchestrator")
# httpx loguea la URL de cada request en INFO, y la Bot API de Telegram lleva el
# token en la URL: sin esto el token termina en el journal cada 50 segundos.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Un poco menos que el TimeoutStopSec de la unit de systemd (300 s).
SHUTDOWN_GRACE = 280
# Telegram muestra "escribiendo…" durante 5 s por cada aviso.
TYPING_EVERY = 4.5

HELP_TEXT = (
    "Soy el Director de Proyecto. Contame qué querés construir y lo reparto entre el "
    "equipo: Producto, DBA y NestJS. Todo queda documentado en tu vault de Obsidian.\n\n"
    "Comandos:\n"
    "/reset: empezar una conversación nueva (olvido el contexto anterior)\n"
    "/estado: qué está haciendo el equipo ahora\n"
    "/ayuda: este mensaje"
)

bot: telegram.TelegramBot | None = None
# Trabajos en curso: el apagado los espera en lugar de cortarlos.
_inflight: set[asyncio.Task] = set()
# Un turno por chat a la vez: dos turnos en paralelo sobre el mismo
# conversation_id de agy se pisan el contexto.
_chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Turnos lanzados y no terminados por chat. Mirar el lock no alcanza: dos mensajes
# seguidos llegan antes de que la primera tarea alcance a tomarlo.
_pending: defaultdict[str, int] = defaultdict(int)
# Mensaje de progreso de cada trabajo en curso. Los sub-agentes llegan a /agents con
# el job_id (header X-Orc-Job), y así sus pasos se suman al mensaje correcto.
_progress: dict[str, progress.Progress] = {}
# Pedidos pausados por cuota, persistidos: sobreviven a un reinicio mientras esperan.
_PAUSED_FILE = config.STATE_DIR / "paused.json"
_resumers: set[asyncio.Task] = set()
# Delegaciones rechazadas por cuota a mitad de un turno, por job_id: al terminar ese
# turno se agenda la continuación.
_paused_delegations: dict[str, dict] = {}


class ChatIn(BaseModel):
    chat_id: str
    text: str
    from_name: str | None = None


class DelegateIn(BaseModel):
    project: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


def auth(x_orc_token: str = Header(default="")) -> None:
    if not config.TOKEN:
        raise HTTPException(503, "GATEWAY_TOKEN no configurado en el gateway")
    if x_orc_token != config.TOKEN:
        raise HTTPException(401, "token inválido")


# ── Orquestador ──────────────────────────────────────────────────────────────


def _orchestrator_prompt(body: ChatIn, is_new: bool) -> str:
    """En una conversación reanudada el rol ya está en contexto: no lo repetimos."""
    if not is_new:
        return body.text
    known = vault.list_projects()
    listing = "\n".join(f"- {p}" for p in known) if known else "- (todavía no hay ninguno)"
    return f"""{config.prompt_for("orchestrator")}

---

# Contexto del entorno

- **Vault de Obsidian:** `{config.VAULT_PATH}`
- **Carpeta de proyectos:** `{config.projects_root()}`
- **Proyectos existentes:**
{listing}

El usuario te escribe desde Telegram{f" y se llama {body.from_name}" if body.from_name else ""}.

# Mensaje del usuario

{body.text}
"""


async def _notify(chat_id: str, text: str, job_id: str) -> None:
    """Entrega la respuesta: directo por Telegram, o vía n8n en la alternativa."""
    if bot:
        try:
            await bot.send(chat_id, text)
        except (httpx.HTTPError, telegram.TelegramError) as exc:
            log.error("no pude mandar la respuesta del job %s: %s", job_id, exc)
        return
    if not config.N8N_CALLBACK_URL:
        log.warning("sin canal de Telegram: la respuesta del job %s no se entrega", job_id)
        return
    payload = {"chat_id": chat_id, "text": text, "job_id": job_id}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # n8n rechaza el callback sin este header.
            response = await client.post(
                config.N8N_CALLBACK_URL, json=payload, headers={"X-Orc-Token": config.TOKEN}
            )
            if response.status_code >= 400:
                log.error("callback a n8n devolvió %s: %s", response.status_code, response.text[:300])
    except httpx.HTTPError as exc:
        log.error("callback a n8n falló: %s", exc)


async def _keep_typing(chat_id: str) -> None:
    while True:
        await bot.typing(chat_id)
        await asyncio.sleep(TYPING_EVERY)


def _load_paused() -> list[dict]:
    try:
        return json.loads(_PAUSED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_paused(entries: list[dict]) -> None:
    tmp = _PAUSED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_PAUSED_FILE)


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _pause(
    chat_id: str,
    text: str,
    from_name: str | None,
    low: list[quota.Window],
    label: str | None = None,
) -> None:
    """Guarda el pedido para cuando se renueve la cuota y avisa."""
    until = quota.resume_at(low)
    entries = _load_paused()
    already = any(e["chat_id"] == chat_id for e in entries)
    entry = {
        "id": uuid.uuid4().hex[:12],
        "chat_id": chat_id,
        "text": text,
        "from_name": from_name,
        "resume_at": until.isoformat(),
        # Lo que ve el usuario en los avisos; el texto puede ser una instrucción interna.
        "label": label,
    }
    _save_paused([*entries, entry])
    log.warning("pausa por cuota para el chat %s hasta %s", chat_id, until.isoformat())
    if already:
        notice = f"📥 Lo anoto: estamos en pausa por cuota hasta {quota.when(until)}."
    else:
        used = 100 - min(w.remaining for w in low)
        notice = (
            f"⏸️ Pausé el trabajo: se usó el {used}% de {quota.describe(low)}. "
            f"Se renueva {quota.when(until)}; ahí retomo solo y te aviso."
        )
    await _notify(chat_id, notice, "cuota")
    _schedule_resume(entry)


def _schedule_resume(entry: dict) -> None:
    task = asyncio.create_task(_resume_later(entry))
    _resumers.add(task)
    task.add_done_callback(_resumers.discard)


async def _resume_later(entry: dict) -> None:
    until = datetime.fromisoformat(entry["resume_at"])
    delay = (until - datetime.now(timezone.utc)).total_seconds() + config.QUOTA_RESUME_BUFFER
    if delay > 0:
        await asyncio.sleep(delay)

    # El primero que despierta retoma todos los pedidos vencidos de ese chat, en el
    # orden en que llegaron. Si cada uno se retomara solo, competirían y el segundo
    # mensaje podría correr antes que el primero (pasó en las pruebas).
    now = datetime.now(timezone.utc)
    entries = _load_paused()
    due = [
        e for e in entries
        if e["chat_id"] == entry["chat_id"] and datetime.fromisoformat(e["resume_at"]) <= now
    ]
    if not any(e["id"] == entry["id"] for e in due):
        return  # ya lo retomó otro temporizador
    due_ids = {e["id"] for e in due}
    _save_paused([e for e in entries if e["id"] not in due_ids])
    log.info("se renovó la cuota: retomo %d pedido(s) del chat %s", len(due), entry["chat_id"])

    listing = "\n".join(f"- {e.get('label') or _short(e['text'])}" for e in due)
    await _notify(
        entry["chat_id"],
        f"▶️ Se renovó la cuota. Retomo {'este pedido' if len(due) == 1 else f'{len(due)} pedidos, en orden'}:\n{listing}",
        "cuota",
    )
    # _start_job es síncrono y el lock por chat es FIFO: se ejecutan en este orden.
    for e in due:
        _start_job(ChatIn(chat_id=e["chat_id"], text=e["text"], from_name=e.get("from_name")))


async def _run_orchestrator(job_id: str, body: ChatIn) -> None:
    async with _chat_locks[body.chat_id]:
        # Antes de gastar nada: si la cuota no alcanza, el pedido espera la renovación.
        low = await quota.exhausted(list(config.MODELS.values()), force=True)
        if low:
            await _pause(body.chat_id, body.text, body.from_name, low)
            jobs.finish(job_id, result={"paused_until": quota.resume_at(low).isoformat()})
            return
        typing = asyncio.create_task(_keep_typing(body.chat_id)) if bot else None
        live = progress.Progress(bot, body.chat_id) if bot else None
        ok = False
        if live:
            _progress[job_id] = live
            await live.start()
        try:
            if sessions.should_recycle(body.chat_id):
                log.info("chat %s alcanzó el máximo de turnos: reciclo conversación", body.chat_id)
                sessions.reset(body.chat_id)
            conversation_id = sessions.get(body.chat_id).get("conversation_id")

            result = await runner.run_agy(
                prompt=_orchestrator_prompt(body, is_new=not conversation_id),
                model=config.MODELS["orchestrator"],
                cwd=config.VAULT_PATH,
                conversation_id=conversation_id,
                on_step=(lambda step: live.step("orchestrator", step)) if live else None,
                extra_env={"ORC_JOB_ID": job_id},
            )
            if result.get("conversation_id"):
                sessions.remember(body.chat_id, result["conversation_id"])

            reply = (result.get("response") or "").strip() or "(el orquestador no respondió nada)"
            if result.get("status") != "SUCCESS":
                reply = f"⚠️ El turno terminó con status={result.get('status')}, puede estar incompleto.\n\n{reply}"
            jobs.finish(job_id, result={"response": reply, "usage": result.get("usage", {})})
            ok = result.get("status") == "SUCCESS"
        except Exception as exc:  # noqa: BLE001 - el job nunca debe morir en silencio
            log.exception("job %s falló", job_id)
            jobs.finish(job_id, error=str(exc))
            reply = f"⚠️ El orquestador falló: {exc}"
        finally:
            if typing:
                typing.cancel()
            if live:
                await live.finish(ok)
                _progress.pop(job_id, None)
        await _notify(body.chat_id, reply, job_id)

        # Si a mitad del turno se rechazó una delegación por cuota, se agenda la
        # continuación: el orquestador retoma con su contexto (misma conversación).
        paused = _paused_delegations.pop(job_id, None)
        if paused:
            await _pause(
                body.chat_id,
                "(Mensaje del sistema) Se renovó la cuota. Retomá la tarea que quedó en "
                f"pausa: delegá en {paused['role']} sobre el proyecto «{paused['project']}» "
                f"con esta instrucción:\n\n{paused['instruction']}",
                body.from_name,
                paused["windows"],
                label=f"continuar: {paused['role']} en «{paused['project']}»",
            )


def _start_job(body: ChatIn) -> tuple[str, bool]:
    """Lanza un turno del orquestador. Devuelve (job_id, quedó_en_cola)."""
    chat_id = body.chat_id
    queued = _pending[chat_id] > 0
    _pending[chat_id] += 1
    job_id = jobs.create("orchestrator", {"chat_id": chat_id, "text": body.text[:500]})
    task = asyncio.create_task(_run_orchestrator(job_id, body))
    _inflight.add(task)

    def _done(finished: asyncio.Task) -> None:
        _inflight.discard(finished)
        _pending[chat_id] -= 1

    task.add_done_callback(_done)
    return job_id, queued


# ── Telegram ─────────────────────────────────────────────────────────────────


def _running_for(chat_id: str) -> list[dict]:
    return [
        j for j in jobs.recent(50)
        if j["status"] == "running" and j["payload"].get("chat_id") == chat_id
    ]


async def _on_telegram_message(chat_id: str, text: str | None, from_name: str | None) -> None:
    if not config.chat_allowed(chat_id):
        # A un extraño no se le contesta nada: ni siquiera que el bot existe.
        log.warning("mensaje ignorado de un chat no autorizado: %s", chat_id)
        return
    if not text:
        await bot.send(chat_id, "Por ahora solo entiendo mensajes de texto.")
        return

    command = text.strip().split()[0].lower().split("@")[0]
    if command in ("/start", "/ayuda", "/help"):
        await bot.send(chat_id, HELP_TEXT)
    elif command == "/reset":
        sessions.reset(chat_id)
        await bot.send(chat_id, "Listo, arrancamos una conversación nueva.")
    elif command == "/estado":
        running = _running_for(chat_id)
        paused = [e for e in _load_paused() if e["chat_id"] == chat_id]
        if paused:
            lines = "\n".join(
                f"- {e.get('label') or _short(e['text'])} "
                f"(retomo {quota.when(datetime.fromisoformat(e['resume_at']))})"
                for e in paused
            )
            await bot.send(chat_id, f"⏸️ En pausa por cuota ({len(paused)}):\n{lines}")
        if running:
            lines = "\n".join(
                f"- desde las {datetime.fromisoformat(j['created_at']).astimezone():%H:%M}: "
                f"{_short(j['payload']['text'])}"
                for j in running
            )
            await bot.send(chat_id, f"En curso ({len(running)}):\n{lines}")
        if not running and not paused:
            await bot.send(chat_id, "No hay nada en curso.")
    else:
        _, queued = _start_job(ChatIn(chat_id=chat_id, text=text, from_name=from_name))
        if queued:
            # Si no está en cola, el mensaje de progreso que aparece al arrancar ya
            # hace de acuse.
            await bot.send(chat_id, "📥 Lo anoto: termino lo anterior y sigo con esto.")


# ── Ciclo de vida ────────────────────────────────────────────────────────────


async def _load_known_models() -> None:
    """Lista los modelos reales de agy. Si falla, no bloquea el arranque."""
    try:
        process = await asyncio.create_subprocess_exec(
            config.AGY_BIN, "models",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("no pude listar los modelos de agy: %s", exc)
        return
    ids = {
        line.split("\t", 1)[0].strip()
        for line in raw.decode("utf-8", "replace").splitlines()
        if "\t" in line
    }
    if ids:
        config.KNOWN_MODELS = ids
        log.info("agy ofrece %d modelos", len(ids))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global bot
    await _load_known_models()
    for problem in config.validate():
        log.error("CONFIG: %s", problem)
    log.info("gateway listo · vault=%s · proyectos=%s", config.VAULT_PATH, vault.list_projects())

    for entry in _load_paused():
        log.info("pausa guardada: retomo %s %s", entry["id"], quota.when(datetime.fromisoformat(entry["resume_at"])))
        _schedule_resume(entry)

    poller = None
    if config.TELEGRAM_BOT_TOKEN:
        bot = telegram.TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_API_BASE)
        poller = asyncio.create_task(bot.poll_forever(_on_telegram_message))

    yield

    for task in list(_resumers):
        task.cancel()  # siguen guardadas en paused.json: se reagendan al arrancar
    if poller:
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller
    if _inflight:
        log.info("esperando %d trabajo(s) en curso antes de apagar", len(_inflight))
        await asyncio.wait(_inflight, timeout=SHUTDOWN_GRACE)
    if bot:
        await bot.close()


app = FastAPI(title="Orchestrator Gateway", version="2.0.0", lifespan=lifespan)


# ── HTTP ─────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    problems = config.validate()
    if bot and bot.last_error:
        problems.append(f"Telegram: {bot.last_error}")
    return {
        "status": "ok" if not problems else "misconfigured",
        "problems": problems,
        "telegram": {
            "mode": "polling" if bot else ("n8n" if config.N8N_CALLBACK_URL else "off"),
            "bot": bot.username if bot else None,
        },
        "inflight": len(_inflight),
        "paused": len(_load_paused()),
        "quota": [
            {"family": w.family, "window": w.label, "remaining": w.remaining,
             "resets_at": w.resets_at.isoformat()}
            for w in await quota.read()
        ],
        "vault": str(config.VAULT_PATH),
        "projects": vault.list_projects(),
        "models": config.MODELS,
    }


@app.get("/projects", dependencies=[Depends(auth)])
async def projects() -> dict:
    return {"projects": vault.list_projects()}


@app.post("/chat", dependencies=[Depends(auth)], status_code=202)
async def chat(body: ChatIn) -> dict:
    """Entrada por HTTP. Responde al instante; el trabajo sigue en background."""
    if not config.chat_allowed(body.chat_id):
        log.warning("chat no autorizado intentó usar el bot: %s", body.chat_id)
        raise HTTPException(403, "este chat no está autorizado")
    job_id, queued = _start_job(body)
    return {"job_id": job_id, "status": "queued" if queued else "running"}


@app.post("/agents/{role}", dependencies=[Depends(auth)])
async def delegate(role: str, body: DelegateIn, x_orc_job: str = Header(default="")) -> dict:
    """Invocación síncrona de un sub-agente. La usa `orc-delegate`."""
    if role not in config.ROLES:
        raise HTTPException(404, f"rol desconocido: {role}. Válidos: {', '.join(config.ROLES)}")
    live = _progress.get(x_orc_job)
    if live:
        live.section(role, body.project)
    low = await quota.exhausted([config.MODELS[role]], force=True)
    if low:
        until = quota.resume_at(low)
        if x_orc_job:
            _paused_delegations[x_orc_job] = {
                "role": role, "project": body.project,
                "instruction": body.instruction, "windows": low,
            }
        if live:
            live.agent_done(role, False, f"⏸️ en pausa por cuota hasta {quota.when(until)}")
        raise HTTPException(
            409,
            f"Cuota agotada: {quota.describe(low)}. El sistema pausó esta tarea y la "
            f"retoma solo {quota.when(until)}. No reintentes ni delegues otra cosa: "
            "decile al usuario que quedó en pausa.",
        )
    try:
        result = await runner.invoke_subagent(
            role, body.project, body.instruction,
            on_step=(lambda step: live.step(role, step)) if live else None,
        )
    except (ValueError, RuntimeError) as exc:
        if live:
            live.agent_done(role, False, f"⚠️ {str(exc)[:120]}")
        code = 400 if isinstance(exc, ValueError) else 409
        raise HTTPException(code, str(exc)) from exc
    if live:
        files = result.get("files_changed") or []
        live.agent_done(
            role, True,
            f"escribió {len(files)} archivo{'s' if len(files) != 1 else ''}"
            + (f" · commit {result['commit']}" if result.get("commit") else "")
            if files else "no escribió archivos",
        )
    return result


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
async def job_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job desconocido")
    return job


@app.get("/jobs", dependencies=[Depends(auth)])
async def job_list() -> dict:
    return {"jobs": jobs.recent()}


@app.post("/sessions/{chat_id}/reset", dependencies=[Depends(auth)])
async def reset_session(chat_id: str) -> dict:
    sessions.reset(chat_id)
    return {"chat_id": chat_id, "status": "reiniciada"}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
