"""Gateway HTTP: puente entre n8n (Telegram) y los agentes `agy` del host."""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config, jobs, runner, sessions, vault

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("orchestrator")

app = FastAPI(title="Orchestrator Gateway", version="1.0.0")

# Telegram corta los mensajes en 4096 caracteres.
TELEGRAM_LIMIT = 3900


def auth(x_orc_token: str = Header(default="")) -> None:
    if not config.TOKEN:
        raise HTTPException(503, "GATEWAY_TOKEN no configurado en el gateway")
    if x_orc_token != config.TOKEN:
        raise HTTPException(401, "token inválido")


class ChatIn(BaseModel):
    chat_id: str
    text: str
    from_name: str | None = None


class DelegateIn(BaseModel):
    project: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok" if not config.validate() else "misconfigured",
        "problems": config.validate(),
        "vault": str(config.VAULT_PATH),
        "projects": vault.list_projects(),
        "models": config.MODELS,
    }


@app.get("/projects", dependencies=[Depends(auth)])
async def projects() -> dict:
    return {"projects": vault.list_projects()}


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
    """Devuelve el resultado a n8n, que lo manda a Telegram."""
    if not config.N8N_CALLBACK_URL:
        log.warning("N8N_CALLBACK_URL vacío: la respuesta del job %s no se entrega", job_id)
        return
    payload = {"chat_id": chat_id, "text": text[:TELEGRAM_LIMIT], "job_id": job_id}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(config.N8N_CALLBACK_URL, json=payload)
            if response.status_code >= 400:
                log.error("callback a n8n devolvió %s: %s", response.status_code, response.text[:300])
    except httpx.HTTPError as exc:
        log.error("callback a n8n falló: %s", exc)


async def _run_orchestrator(job_id: str, body: ChatIn) -> None:
    try:
        if sessions.should_recycle(body.chat_id):
            log.info("chat %s alcanzó el máximo de turnos: reciclo conversación", body.chat_id)
            sessions.reset(body.chat_id)
        session = sessions.get(body.chat_id)
        conversation_id = session.get("conversation_id")

        result = await runner.run_agy(
            prompt=_orchestrator_prompt(body, is_new=not conversation_id),
            model=config.MODELS["orchestrator"],
            cwd=config.VAULT_PATH,
            conversation_id=conversation_id,
        )
        if result.get("conversation_id"):
            sessions.remember(body.chat_id, result["conversation_id"])

        reply = (result.get("response") or "").strip() or "(el orquestador no respondió nada)"
        jobs.finish(job_id, result={"response": reply, "usage": result.get("usage", {})})
        await _notify(body.chat_id, reply, job_id)
    except Exception as exc:  # noqa: BLE001 - el job nunca debe morir en silencio
        log.exception("job %s falló", job_id)
        jobs.finish(job_id, error=str(exc))
        await _notify(body.chat_id, f"⚠️ El orquestador falló: {exc}", job_id)


@app.post("/chat", dependencies=[Depends(auth)], status_code=202)
async def chat(body: ChatIn, background: BackgroundTasks) -> dict:
    """Entrada desde Telegram. Responde al instante; el trabajo sigue en background."""
    if not config.chat_allowed(body.chat_id):
        log.warning("chat no autorizado intentó usar el bot: %s", body.chat_id)
        raise HTTPException(403, "este chat no está autorizado")
    job_id = jobs.create("orchestrator", {"chat_id": body.chat_id, "text": body.text[:500]})
    background.add_task(_run_orchestrator, job_id, body)
    return {"job_id": job_id, "status": "running"}


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


@app.on_event("startup")
async def on_startup() -> None:
    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("CONFIG: %s", problem)
    else:
        log.info("gateway listo · vault=%s · proyectos=%s", config.VAULT_PATH, vault.list_projects())


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
