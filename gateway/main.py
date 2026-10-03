"""Gateway: recibe mensajes de Telegram y los pone a trabajar a los agentes `agy`.

Telegram entra por long polling (telegram.py). El endpoint POST /chat queda para
pruebas y para la alternativa con n8n.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config, jobs, runner, sessions, telegram, vault

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


async def _run_orchestrator(job_id: str, body: ChatIn) -> None:
    async with _chat_locks[body.chat_id]:
        typing = asyncio.create_task(_keep_typing(body.chat_id)) if bot else None
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
            )
            if result.get("conversation_id"):
                sessions.remember(body.chat_id, result["conversation_id"])

            reply = (result.get("response") or "").strip() or "(el orquestador no respondió nada)"
            if result.get("status") != "SUCCESS":
                reply = f"⚠️ El turno terminó con status={result.get('status')}, puede estar incompleto.\n\n{reply}"
            jobs.finish(job_id, result={"response": reply, "usage": result.get("usage", {})})
        except Exception as exc:  # noqa: BLE001 - el job nunca debe morir en silencio
            log.exception("job %s falló", job_id)
            jobs.finish(job_id, error=str(exc))
            reply = f"⚠️ El orquestador falló: {exc}"
        finally:
            if typing:
                typing.cancel()
        await _notify(body.chat_id, reply, job_id)


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
        if not running:
            await bot.send(chat_id, "No hay nada en curso.")
        else:
            lines = "\n".join(f"- desde {j['created_at'][11:16]} UTC: {j['payload']['text'][:80]}" for j in running)
            await bot.send(chat_id, f"En curso ({len(running)}):\n{lines}")
    else:
        _, queued = _start_job(ChatIn(chat_id=chat_id, text=text, from_name=from_name))
        await bot.send(
            chat_id,
            "📥 Lo anoto: termino lo anterior y sigo con esto." if queued
            else "👀 Tomado. El equipo está trabajando, te aviso cuando termine.",
        )


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

    poller = None
    if config.TELEGRAM_BOT_TOKEN:
        bot = telegram.TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_API_BASE)
        poller = asyncio.create_task(bot.poll_forever(_on_telegram_message))

    yield

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
async def delegate(role: str, body: DelegateIn) -> dict:
    """Invocación síncrona de un sub-agente. La usa `orc-delegate`."""
    if role not in config.ROLES:
        raise HTTPException(404, f"rol desconocido: {role}. Válidos: {', '.join(config.ROLES)}")
    try:
        return await runner.invoke_subagent(role, body.project, body.instruction)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


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
