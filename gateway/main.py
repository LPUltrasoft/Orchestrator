"""Gateway: recibe mensajes de Telegram y los pone a trabajar a los agentes `agy`.

Telegram entra por long polling (telegram.py). El endpoint POST /chat queda para
pruebas y para la alternativa con n8n.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import autenticacion, code, config, design, diagram, drive, jenkins, jobs, mapa, palette, process, progress, quota, runner, selftest, sessions, stitch, telegram, vault

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
    "Soy el Director de Proyecto. Contame qué querés construir y lo llevo por el proceso "
    "con el equipo: Producto, QA, Líder técnico, DBA, Legal, Team leader, UX, UI, "
    "Revisor UX/UI, Seguridad y los desarrolladores de back y front. Todo queda "
    "documentado en tu vault de Obsidian.\n\n"
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
# Builds de develop que se siguen después de un merge, persistidos: si Jenkins no puede
# leer el repo, el seguimiento espera a que se arregle aunque el gateway se reinicie.
_CI_FILE = config.STATE_DIR / "ci_develop.json"
CI_POLL = 20  # segundos entre consultas a Jenkins
CI_RESCAN = 300  # mientras el escaneo falla, se lo vuelve a pedir cada 5 minutos
CI_BUILD_WAIT = 1800  # con el escaneo andando, cuánto se espera el build de develop
# Existe mientras el gateway corre: si al arrancar ya estaba, el apagado anterior no fue
# limpio (corte de luz, cuelgue, SIGKILL).
_RUNNING_MARK = config.STATE_DIR / "en_marcha"
# Espacio del Drive de los backups (se ve en /health y en /estado).
_drive = {"uso": None, "revisado": None, "avisado": None, "error": None}
# Estado del push automático del vault a GitHub (se ve en /health y en /estado).
_push = {"pending": None, "last_push": None, "failing_since": None, "error": None, "alerted": False}
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
    # Etapa 3: la tarea del plan (T-NNN, o «esqueleto»). Con ella, el rol programa en
    # los repos del proyecto y el sistema publica la rama y abre el PR.
    tarea: str | None = None


class AuthIn(BaseModel):
    project: str = Field(min_length=1)


class ReposIn(BaseModel):
    project: str = Field(min_length=1)
    front: str = Field(min_length=1)  # nombre (pedir) o link (registrar)
    back: str = Field(min_length=1)
    chat_id: str | None = None


class MergeIn(BaseModel):
    project: str = Field(min_length=1)
    tarea: str = Field(min_length=1)


class ApprovalIn(BaseModel):
    project: str = Field(min_length=1)
    gate: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    chat_id: str | None = None  # sin X-Orc-Job: pruebas o uso manual


class DesignIn(BaseModel):
    project: str = Field(min_length=1)
    screens: list[str] | None = None  # solo esas pantallas; vacío = todas
    edit: str | None = None  # id de una pantalla ya generada a la que aplicar un cambio
    change: str | None = None
    chat_id: str | None = None


class MesaIn(BaseModel):
    project: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    rounds: int = Field(default=2, ge=1, le=3)
    chat_id: str | None = None


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
    listing = "\n".join(f"- {process.summary_line(p)}" for p in known) if known else "- (todavía no hay ninguno)"
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


async def _alert(text: str) -> None:
    """Aviso del sistema, no de un trabajo: va a todos los chats autorizados."""
    if not bot or not config.ALLOWED_CHAT_IDS:
        log.warning("aviso sin destinatario: %s", text)
        return
    for chat_id in config.ALLOWED_CHAT_IDS:
        try:
            await bot.send(chat_id, text)
        except (httpx.HTTPError, telegram.TelegramError) as exc:
            log.error("no pude mandar el aviso a %s: %s", chat_id, exc)


async def _push_vault() -> None:
    """Sube a GitHub los commits del vault. Si falla un buen rato, avisa una sola vez."""
    pending = await asyncio.to_thread(vault.unpushed)
    _push["pending"] = pending
    if not pending:
        return
    now = datetime.now(timezone.utc)
    try:
        await asyncio.to_thread(vault.push)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        _push["error"] = str(exc)
        # Una advertencia al empezar a fallar; los reintentos van a debug para no llenar el log.
        (log.debug if _push["failing_since"] else log.warning)(
            "no pude subir el vault (%d commits pendientes): %s", pending, exc)
        _push["failing_since"] = _push["failing_since"] or now
        failing = (now - _push["failing_since"]).total_seconds()
        if not _push["alerted"] and failing >= config.VAULT_PUSH_ALERT_AFTER:
            _push["alerted"] = True
            commits = "1 commit" if pending == 1 else f"{pending} commits"
            await _alert(
                f"⚠️ Hace {max(1, int(failing // 60))} minutos que no puedo subir el vault a "
                f"GitHub ({commits} sin subir). Si la PC se rompe, eso se pierde.\n\n"
                f"Error: {_push['error']}"
            )
        return
    log.info("vault subido a GitHub (%d commits)", pending)
    if _push["alerted"]:
        await _alert("✅ Ya pude subir el vault a GitHub: no queda nada pendiente.")
    _push.update(pending=0, last_push=now, failing_since=None, error=None, alerted=False)


def _selftest_notes(previous: dict | None, result: dict) -> list[str]:
    """Qué contarle al usuario de una autoprueba: fallas, recuperación y actualizaciones."""
    notes = []
    failures = [f"- {name}: {result[key]}" for key, name in (("agy", "agy"), ("claude", "Claude")) if result[key]]
    if failures:
        notes.append("🔧 Autoprueba de los motores: algo no anda.\n" + "\n".join(failures))
    elif previous and not previous.get("ok"):
        notes.append("✅ Autoprueba: agy y Claude volvieron a andar.")
    if previous:
        for key, name in (("agy", "agy"), ("claude", "Claude Code")):
            before, now = previous.get("versiones", {}).get(key), result["versiones"].get(key)
            if before and now and before != now:
                state = "falló (mirá arriba)" if result[key] else "pasó" + (": sigue aislado" if key == "claude" else "")
                notes.append(f"ℹ️ Se actualizó {name}: {before} → {now}. La autoprueba {state}.")
    return notes


async def _startup_checks(unclean: bool) -> None:
    """Al arrancar: avisa si el apagado anterior no fue limpio, si hay problemas de
    configuración, y corre la autoprueba si hace falta (para no gastar cuota en cada
    reinicio: solo si la última buena tiene más de una hora o cambió una versión)."""
    await asyncio.sleep(15)
    notes = []
    if unclean:
        notes.append("⚠️ El gateway no se había apagado bien (¿corte de luz o cuelgue?). Ya volvió a "
                     "andar; si estabas esperando una respuesta, volvé a pedírmela.")
    problems = config.validate()
    if problems:
        notes.append("⚠️ Problemas de configuración:\n" + "\n".join(f"- {p}" for p in problems))
    previous = selftest.last()
    current = await selftest.versions()
    fresh = previous and previous.get("ok") and previous.get("versiones") == current and (
        datetime.now(timezone.utc) - datetime.fromisoformat(previous["fecha"])).total_seconds() < 3600
    if not fresh:
        notes += _selftest_notes(previous, await selftest.run())
    if notes:
        await _alert("\n\n".join(notes))


def _seconds_until(hour: int) -> float:
    now = datetime.now().astimezone()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target.replace(day=now.day) + timedelta(days=1)
    return (target - now).total_seconds()


async def _daily_selftest() -> None:
    while True:
        await asyncio.sleep(_seconds_until(config.SELFTEST_HOUR))
        try:
            previous = selftest.last()
            notes = _selftest_notes(previous, await selftest.run())
            if notes:
                await _alert("\n\n".join(notes))
        except Exception:  # noqa: BLE001 — que un error raro no apague la autoprueba diaria
            log.exception("error inesperado en la autoprueba diaria")


async def _update_map() -> None:
    """Regenera el mapa de puertos y repos del vault (07). Un error no frena a quien lo llama."""
    try:
        if await asyncio.to_thread(mapa.update):
            log.info("mapa de puertos y repos actualizado")
    except Exception:  # noqa: BLE001
        log.exception("no pude actualizar el mapa de puertos y repos")


async def _map_refresher() -> None:
    """Al arrancar y cada media hora: toma cambios de fase y contenedores que se cayeron."""
    while True:
        await _update_map()
        await asyncio.sleep(1800)


async def _check_drive() -> None:
    """Revisa el Drive de los backups; la pide cada backup antes de subir. Avisa si pasa el
    umbral: al cruzarlo, y una vez por semana mientras siga arriba (los backups son
    diarios: no hace falta el mismo aviso todos los días). Cuando baja, avisa que se liberó."""
    try:
        u = await drive.usage()
    except (RuntimeError, OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
        _drive["error"] = str(exc)[:300]
        log.warning("no pude revisar el espacio de Drive: %s", exc)
        return
    now = datetime.now(timezone.utc)
    _drive.update(uso=u, revisado=now, error=None)
    limit = config.DRIVE_ALERT_GB * drive.GIB
    if u["usado"] >= limit:
        last = _drive["avisado"]
        if not last or (now - last).total_seconds() >= 7 * 86400:
            _drive["avisado"] = now
            await _alert(drive.alert_text(u))
    elif _drive["avisado"] and u["usado"] < limit - 0.5 * drive.GIB:
        _drive["avisado"] = None
        await _alert(f"✅ El Drive de los backups bajó a {drive.gb(u['usado'])}: hay espacio de nuevo.")


async def _vault_pusher() -> None:
    while True:
        await asyncio.sleep(config.VAULT_PUSH_INTERVAL)
        try:
            await _push_vault()
        except Exception:  # noqa: BLE001 — que un error raro no apague el push para siempre
            log.exception("error inesperado subiendo el vault")


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


# Sin await entre leer y guardar: dos seguimientos (front y back) no se pisan.
def _ci_pending() -> list[dict]:
    try:
        return json.loads(_CI_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _ci_get(project: str, repo: str) -> dict | None:
    return next((e for e in _ci_pending() if e["proyecto"] == project and e["repo"] == repo), None)


def _ci_drop(project: str, repo: str, add: dict | None = None) -> None:
    """Saca el seguimiento de ese repo; con `add`, lo reemplaza por ese."""
    entries = [e for e in _ci_pending() if (e["proyecto"], e["repo"]) != (project, repo)]
    tmp = _CI_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries + ([add] if add else []), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_CI_FILE)


def _ci_put(entry: dict) -> None:
    _ci_drop(entry["proyecto"], entry["repo"], add=entry)


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _pause(
    chat_id: str,
    text: str,
    from_name: str | None,
    low: list[quota.Window],
    label: str | None = None,
    extra: dict | None = None,
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
        **(extra or {}),
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
        if e.get("kind") == "mesa":
            _start_mesa(e["chat_id"], e["project"], e["topic"], e["folder"], e["rounds"], e["step"])
        else:
            _start_job(ChatIn(chat_id=e["chat_id"], text=e["text"], from_name=e.get("from_name")))


async def _run_orchestrator(job_id: str, body: ChatIn) -> None:
    async with _chat_locks[body.chat_id]:
        # Antes de gastar nada: si la cuota no alcanza, el pedido espera la renovación.
        low = await quota.exhausted(["orchestrator"], force=True)
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
                f"pausa: delegá en {paused['role']} sobre el proyecto «{paused['project']}»"
                + (f" con --tarea {paused['tarea']}" if paused.get("tarea") else "")
                + (" (ya había empezado: lo que alcanzó a escribir está en el vault o en su rama, que "
                   "lo continúe)" if paused.get("a_mitad") else "")
                + f" con esta instrucción:\n\n{paused['instruction']}",
                body.from_name,
                paused["windows"],
                label=f"continuar: {paused['role']} en «{paused['project']}»",
            )


def _chat_for(job_id: str) -> str | None:
    job = jobs.get(job_id) if job_id else None
    return job["payload"].get("chat_id") if job else None


def _track(chat_id: str, task: asyncio.Task) -> None:
    _inflight.add(task)

    def _done(finished: asyncio.Task) -> None:
        _inflight.discard(finished)
        _pending[chat_id] -= 1

    task.add_done_callback(_done)


def _start_mesa(chat_id: str, project: str, topic: str, folder: str, rounds: int, step: int = 0) -> str:
    _pending[chat_id] += 1
    job_id = jobs.create("mesa", {"chat_id": chat_id, "text": f"mesa técnica: {topic}", "project": project})
    _track(chat_id, asyncio.create_task(_run_mesa(job_id, chat_id, project, topic, folder, rounds, step)))
    return job_id


async def _run_mesa(
    job_id: str, chat_id: str, project: str, topic: str, folder: str, rounds: int, start: int
) -> None:
    """Las rondas de la mesa, una detrás de otra. Corre en el gateway y no en el turno
    del orquestador: un debate de diez minutos no entra en su timeout."""
    async with _chat_locks[chat_id]:
        plan = process.mesa_plan(rounds)
        live = progress.Progress(bot, chat_id, title=f"🏛️ Mesa técnica: {topic}") if bot else None
        typing = asyncio.create_task(_keep_typing(chat_id)) if bot else None
        if live:
            _progress[job_id] = live
            await live.start(with_director=False)
        ok = paused = False
        index = start
        text = None
        try:
            # La carpeta la crea el gateway: un agente que intenta `mkdir` choca con la
            # allowlist y gasta un reintento (pasó en la prueba de punta a punta).
            await asyncio.to_thread(
                (vault.project_path(project) / folder).mkdir, parents=True, exist_ok=True
            )
            before = {a["id"] for a in await asyncio.to_thread(process.list_adrs, project)}
            for index in range(start, len(plan)):
                step = plan[index]
                low = await quota.exhausted([step.role], force=True)
                if low:
                    paused = True
                    await _pause(
                        chat_id, "", None, low,
                        label=f"continuar: mesa técnica «{topic}» (paso {index + 1} de {len(plan)})",
                        extra={"kind": "mesa", "project": project, "topic": topic,
                               "folder": folder, "rounds": rounds, "step": index},
                    )
                    jobs.finish(job_id, result={"paused_at_step": index})
                    return
                stage = "cierre" if step.closing else f"ronda {step.round}"
                if live:
                    live.section(step.role, f"{project} · {stage}")
                before_step = await asyncio.to_thread(process.adr_names, project)
                try:
                    result = await runner.invoke_subagent(
                        step.role, project, process.mesa_instruction(step, topic, folder, rounds),
                        on_step=(lambda s, r=step.role: live.step(r, s)) if live else None,
                    )
                except (ValueError, RuntimeError) as exc:
                    if live:
                        live.agent_done(step.role, False, f"⚠️ {str(exc)[:120]}")
                    raise
                note = ""
                if not step.closing:
                    moved = await asyncio.to_thread(
                        process.quarantine_premature_adrs, project, folder, before_step
                    )
                    if moved:
                        log.warning("mesa %s: %s escritos antes del cierre, pasan a borrador", job_id, moved)
                        note = f" · {len(moved)} ADR antes de tiempo → borrador"
                if live:
                    files = result.get("files_changed") or []
                    live.agent_done(
                        step.role, True,
                        f"escribió {len(files)} archivo{'s' if len(files) != 1 else ''}{note}",
                    )
            adrs = [a for a in await asyncio.to_thread(process.list_adrs, project) if a["id"] not in before]
            ok = True
            jobs.finish(job_id, result={"adrs": [a["id"] for a in adrs]})
            text = process.after_mesa_text(project, topic, folder, adrs)
        except Exception as exc:  # noqa: BLE001 - la mesa nunca debe morir en silencio
            log.exception("la mesa %s falló", job_id)
            jobs.finish(job_id, error=str(exc))
            text = (
                f"(Mensaje del sistema) La mesa técnica «{topic}» de «{project}» falló en el "
                f"paso {index + 1} de {len(plan)}: {exc}. Avisale al usuario y proponé cómo seguir."
            )
        finally:
            if typing:
                typing.cancel()
            if live:
                await live.finish(ok, paused=paused)
                _progress.pop(job_id, None)
        # Despierta al orquestador (espera el lock del chat: corre después de esto).
        if text:
            _start_job(ChatIn(chat_id=chat_id, text=text))


def _start_design(chat_id: str, project: str, only: list[str] | None, edit: str | None, change: str | None) -> str:
    _pending[chat_id] += 1
    label = f"cambio en {edit}" if edit else "pantallas en Stitch"
    job_id = jobs.create("diseno", {"chat_id": chat_id, "text": f"diseño: {label}", "project": project})
    _track(chat_id, asyncio.create_task(_run_design(job_id, chat_id, project, only, edit, change)))
    return job_id


async def _run_design(
    job_id: str, chat_id: str, project: str,
    only: list[str] | None, edit: str | None, change: str | None,
) -> None:
    """Genera (o corrige) pantallas en Stitch, te las manda como álbum y despierta al
    orquestador. Corre en el gateway: generar diez pantallas lleva varios minutos."""
    async with _chat_locks[chat_id]:
        title = f"✏️ Cambio de diseño: {project}" if edit else f"🖌️ Diseño en Stitch: {project}"
        live = progress.Progress(bot, chat_id, title=title) if bot else None
        typing = asyncio.create_task(_keep_typing(chat_id)) if bot else None
        if live:
            _progress[job_id] = live
            await live.start(with_director=False)

        def on_screen(label: str, status: str, detail: str) -> None:
            if not live:
                return
            if status == "start":
                live.section("stitch", label)
            elif status == "done":
                live.agent_done("stitch", True, "lista")
            else:
                live.agent_done("stitch", False, f"⚠️ {detail[:100]}")

        ok = False
        rendered: list[design.Rendered] = []
        errors: list[str] = []
        try:
            if edit:
                rendered = await design.edit(project, edit, change or "", on_screen)
            else:
                rendered, errors = await design.generate(project, only, on_screen)
            ok = bool(rendered) and not errors
            jobs.finish(job_id, result={"pantallas": [r.id for r in rendered], "errores": errors})
        except Exception as exc:  # noqa: BLE001 - el diseño nunca debe morir en silencio
            log.exception("el diseño %s falló", job_id)
            jobs.finish(job_id, error=str(exc))
            errors.append(str(exc))
        finally:
            if typing:
                typing.cancel()
            if live:
                await live.finish(ok)
                _progress.pop(job_id, None)

        if bot and rendered:
            photos = [
                (await asyncio.to_thread(r.png.read_bytes),
                 f"✏️ {r.label}: {change}" if edit else f"{i}/{len(rendered)} · {r.label}")
                for i, r in enumerate(rendered, 1)
            ]
            try:
                await bot.send_album(chat_id, photos)
            except (httpx.HTTPError, telegram.TelegramError) as exc:
                log.error("no pude mandar las capturas: %s", exc)
                errors.append(f"no pude mandarte las capturas por Telegram: {exc}")

        screens = {r.id: r.title for r in rendered}
        names = ", ".join(f"{title} ({screen_id})" for screen_id, title in screens.items()) or "ninguna"
        problems = ("\nProblemas: " + "; ".join(errors)) if errors else ""
        state = await asyncio.to_thread(process.load, project)
        if edit:
            what = f"Stitch aplicó el cambio «{change}» a la pantalla {names}"
        else:
            what = (
                f"Stitch generó {len(screens)} pantalla(s) de «{project}» en sus "
                f"dispositivos ({len(rendered)} capturas): {names}"
            )
        next_step = (
            "Si UX, UI, el 03 y el 04 ya están, pedí la aprobación «diseño» con un resumen; "
            "si falta algo de la fase 5, seguí con eso."
            if state is not None else
            "Preguntale al usuario qué le parecen."
        )
        _start_job(ChatIn(chat_id=chat_id, text=(
            f"(Mensaje del sistema) {what}. El usuario ya vio las capturas en Telegram.{problems}\n\n"
            f"Qué hacer ahora: {next_step} Si pide cambios en una pantalla, usá "
            f"orc-diseno \"{project}\" editar <id> \"<cambio>\"."
        )))


def _start_job(body: ChatIn) -> tuple[str, bool]:
    """Lanza un turno del orquestador. Devuelve (job_id, quedó_en_cola)."""
    chat_id = body.chat_id
    queued = _pending[chat_id] > 0
    _pending[chat_id] += 1
    job_id = jobs.create("orchestrator", {"chat_id": chat_id, "text": body.text[:500]})
    _track(chat_id, asyncio.create_task(_run_orchestrator(job_id, body)))
    return job_id, queued


# ── Telegram ─────────────────────────────────────────────────────────────────


def _running_for(chat_id: str) -> list[dict]:
    return [
        j for j in jobs.recent(50)
        if j["status"] == "running" and j["payload"].get("chat_id") == chat_id
    ]


def _approval_text(project: str, gate: str, summary: str) -> str:
    head = f"⛩ Aprobación pendiente · {project}\n{process.gate_label(gate, project)}"
    if gate in process.GATES:
        number = process.GATES[gate]
        head += f" (cierra la fase {process.phase(number).label})"
    return f"{head}\n\n{summary}"


async def _on_telegram_callback(
    chat_id: str, from_name: str | None, data: str, message_id: int, callback_id: str
) -> None:
    if not config.chat_allowed(chat_id):
        await bot.answer_callback(callback_id)
        return
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "apr" or parts[2] not in ("ok", "chg"):
        await bot.answer_callback(callback_id, "Botón desconocido")
        return
    _, approval_id, action = parts
    found = await asyncio.to_thread(process.find_pending, approval_id)
    if not found:
        await bot.answer_callback(callback_id, "Este pedido ya no está vigente")
        return
    project, state, entry = found
    by = from_name or "el usuario"
    original = _approval_text(project, entry["puerta"], entry["resumen"])
    stamp = datetime.now().astimezone().strftime("%d/%m %H:%M")

    if action == "ok":
        try:
            gate, advanced = await asyncio.to_thread(process.approve, project, state, approval_id, by)
            event = f"{gate} aprobado por {by}" + (f"; pasa a la fase {advanced}" if advanced else "")
            await asyncio.to_thread(process.save, project, state, event)
        except Exception as exc:  # noqa: BLE001 - el usuario tiene que saber que no se registró
            log.exception("no pude registrar la aprobación %s", approval_id)
            await bot.answer_callback(callback_id, "⚠️ No se pudo registrar")
            await bot.send(chat_id, f"⚠️ No pude registrar la aprobación de «{entry['puerta']}»: {exc}")
            return
        await bot.answer_callback(callback_id, "✅ Aprobado")
        await bot.edit(chat_id, message_id, f"{original}\n\n✅ Aprobado por {by} · {stamp}")
        if gate.startswith("merge:"):
            await _merge_after_approval(chat_id, from_name, project, gate[6:], by)
            return
        _start_job(ChatIn(
            chat_id=chat_id, from_name=from_name,
            text=process.after_approval_text(project, state, gate, by, advanced),
        ))
    else:
        entry["esperando_cambios"] = True
        await asyncio.to_thread(process.save, project, state, f"{entry['puerta']}: pidió cambios")
        await bot.answer_callback(callback_id, "Escribime qué querés cambiar")
        await bot.edit(
            chat_id, message_id,
            f"{original}\n\n✏️ Pediste cambios · {stamp}\nEscribime cuáles en tu próximo mensaje.",
        )


async def _merge_after_approval(chat_id: str, from_name: str | None, project: str, task: str, by: str) -> None:
    """El usuario aprobó el merge: lo hace el sistema y le cuenta al Director."""
    try:
        result = await _merge_task(project, task, chat_id)
    except MergeError as exc:
        await bot.send(chat_id, f"⚠️ Aprobaste el merge de {task}, pero no se pudo hacer: {exc.detail}")
        _start_job(ChatIn(chat_id=chat_id, from_name=from_name, text=(
            f"(Mensaje del sistema) {by} aprobó el merge de {task} en «{project}», pero falló: {exc.detail}\n\n"
            "Qué hacer: resolvelo (por ejemplo, que el desarrollador traiga develop a su rama y resuelva "
            "los conflictos) y volvé a pedir la aprobación del merge."
        )))
        return
    merges = ", ".join(f"{r} {c}" for r, c in result["merges"].items())
    await bot.send(chat_id, f"🔀 Mergeado en {config.DEV_BRANCH}: {merges}. Le avisé a Jenkins; "
                            "te aviso cuando termine el build.")
    _start_job(ChatIn(chat_id=chat_id, from_name=from_name, text=(
        f"(Mensaje del sistema) {by} aprobó el merge de {task} en «{project}» y el sistema lo mergeó en "
        f"{config.DEV_BRANCH} ({merges}). El sistema sigue el build de develop y le avisa el resultado al "
        "usuario; si falla, a vos también, con el log. Contale al usuario en una línea que quedó mergeado "
        "(sin decir que Jenkins ya está compilando: eso lo confirma el aviso) y terminá el turno."
    )))


async def _on_telegram_message(chat_id: str, text: str | None, from_name: str | None) -> None:
    if not config.chat_allowed(chat_id):
        # A un extraño no se le contesta nada: ni siquiera que el bot existe.
        log.warning("mensaje ignorado de un chat no autorizado: %s", chat_id)
        return
    if not text:
        await bot.send(chat_id, "Por ahora solo entiendo mensajes de texto.")
        return

    command = text.strip().split()[0].lower().split("@")[0]
    if not command.startswith("/"):
        waiting = await asyncio.to_thread(process.waiting_changes, chat_id)
        if waiting:
            project, state, approval_id, entry = waiting
            state["pendientes"].pop(approval_id, None)
            await asyncio.to_thread(process.save, project, state, f"{entry['puerta']}: cambios pedidos")
            _, queued = _start_job(ChatIn(
                chat_id=chat_id, from_name=from_name,
                text=process.changes_text(project, entry["puerta"], text),
            ))
            if queued:
                await bot.send(chat_id, "📥 Lo anoto: termino lo anterior y sigo con tus cambios.")
            return
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
        if _drive["uso"] and _drive["uso"]["usado"] >= config.DRIVE_ALERT_GB * drive.GIB:
            await bot.send(chat_id, f"💾 El Drive de los backups está en {drive.gb(_drive['uso']['usado'])} de {drive.gb(_drive['uso']['total'])}.")
        if _push["failing_since"]:
            await bot.send(chat_id, (
                f"⚠️ El vault no se sube a GitHub desde las "
                f"{_push['failing_since'].astimezone():%H:%M} ({_push['pending']} sin subir)."
            ))
        staged = await asyncio.to_thread(process.projects_with_state)
        if staged:
            await bot.send(chat_id, "📍 Proyectos:\n" + "\n".join(
                f"- {process.summary_line(name)}" for name, _ in staged
            ))
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
    if jenkins.configured():
        for entry in _ci_pending():
            log.info("retomo el seguimiento de %s de %s/%s (%s)", config.DEV_BRANCH, entry["proyecto"], entry["repo"], entry["tarea"])
            _watch(entry["proyecto"], entry["repo"])

    poller = None
    if config.TELEGRAM_BOT_TOKEN:
        bot = telegram.TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_API_BASE)
        poller = asyncio.create_task(bot.poll_forever(_on_telegram_message, _on_telegram_callback))
    pusher = None
    if config.VAULT_AUTOCOMMIT and config.VAULT_AUTOPUSH:
        pusher = asyncio.create_task(_vault_pusher())
    unclean = _RUNNING_MARK.exists()
    _RUNNING_MARK.write_text(datetime.now(timezone.utc).isoformat())
    monitors = [asyncio.create_task(_startup_checks(unclean)), asyncio.create_task(_daily_selftest()),
                asyncio.create_task(_map_refresher())]

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
    for task in monitors:
        task.cancel()
    if pusher:
        pusher.cancel()
        # Lo último que commitearon los trabajos que acaban de terminar. Poco margen:
        # systemd corta a los 300 s y la espera de arriba puede haber usado 280.
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_push_vault(), timeout=15)
    if bot:
        await bot.close()
    _RUNNING_MARK.unlink(missing_ok=True)  # apagado limpio


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
        "stitch": stitch.configured(),
        "paused": len(_load_paused()),
        "quota": [
            {"family": w.family, "window": w.label, "remaining": w.remaining,
             "resets_at": w.resets_at.isoformat()}
            for w in await quota.read_all()
        ],
        "vault": str(config.VAULT_PATH),
        "vault_push": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in _push.items()},
        "drive": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in _drive.items()},
        "autoprueba": selftest.last(),
        "jenkins": (await asyncio.to_thread(jenkins.whoami)) if jenkins.configured() else None,
        "ci_develop": [{k: e.get(k) for k in ("proyecto", "repo", "tarea", "aviso")} for e in _ci_pending()],
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


def _pause_delegation(job_id: str, live, role: str, body: DelegateIn, low: list, midway: bool = False) -> None:
    """Pausa una delegación por cuota: queda agendada para cuando se renueve y el Director
    cierra el turno. Lanza siempre la HTTPException que le explica qué pasó."""
    until = quota.resume_at(low)
    if job_id:
        _paused_delegations[job_id] = {
            "role": role, "project": body.project, "instruction": body.instruction,
            "tarea": body.tarea, "windows": low, "a_mitad": midway,
        }
    if live:
        live.agent_done(role, False, f"⏸️ en pausa por cuota hasta {quota.when(until)}")
    raise HTTPException(
        409,
        ("Se agotó la cuota a mitad del trabajo (lo hecho quedó guardado como INCOMPLETO): " if midway
         else "Cuota agotada: ")
        + f"{quota.describe(low)}. El sistema pausó esta tarea y la retoma solo {quota.when(until)}. "
        "No reintentes ni delegues otra cosa: decile al usuario que quedó en pausa.",
    )


@app.post("/agents/{role}", dependencies=[Depends(auth)])
async def delegate(role: str, body: DelegateIn, x_orc_job: str = Header(default="")) -> dict:
    """Invocación síncrona de un sub-agente. La usa `orc-delegate`."""
    if role not in config.ROLES:
        raise HTTPException(404, f"rol desconocido: {role}. Válidos: {', '.join(config.ROLES)}")
    live = _progress.get(x_orc_job)
    if live:
        live.section(role, body.project)
    try:
        state = await asyncio.to_thread(process.ensure, body.project)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    blocked = process.check_role(state, role)
    if blocked:
        if live:
            live.agent_done(role, False, "⛔ todavía no es su fase")
        raise HTTPException(409, blocked)
    low = await quota.exhausted([role], force=True)
    if low:
        _pause_delegation(x_orc_job, live, role, body, low)
    try:
        result = await runner.invoke_subagent(
            role, body.project, body.instruction,
            on_step=(lambda step: live.step(role, step)) if live else None,
            task=body.tarea,
        )
    except runner.QuotaExhausted as exc:
        # Se agotó a mitad de la corrida: la misma pausa que si se hubiera agotado antes.
        from_message = quota.from_limit_message(str(exc))
        low = await quota.exhausted([role], force=True) or ([from_message] if from_message else [quota.Window(
            quota.CLAUDE_FAMILY, "Five Hour Limit Remaining", 0,
            datetime.now(timezone.utc) + timedelta(hours=1))])
        log.warning("cuota agotada durante %s en %s: %s", role, body.project, exc)
        _pause_delegation(x_orc_job, live, role, body, low, midway=True)
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
    if result.get("images"):
        await _send_images(_chat_for(x_orc_job), result["images"])
    if role == "dba" and any(Path(f).name.startswith(diagram.DATA_MODEL_DOC) for f in result.get("files_changed") or []):
        await _send_er_diagram(_chat_for(x_orc_job), body.project)
    return result


async def _send_er_diagram(chat_id: str | None, project: str) -> None:
    """El DER del 03 como imagen, cada vez que el DBA lo toca (pedido del usuario)."""
    if not bot or not chat_id:
        return
    docs = sorted(vault.project_path(project).glob(f"{diagram.DATA_MODEL_DOC}*.md"))
    blocks = diagram.er_blocks(docs[0].read_text(encoding="utf-8")) if docs else []
    if not blocks:
        log.info("el DBA tocó el 03 de %s pero no tiene un erDiagram", project)
        return
    for i, block in enumerate(blocks, 1):
        caption = f"🗄️ DER de «{project}»" + (f" ({i}/{len(blocks)})" if len(blocks) > 1 else "")
        try:
            png = await asyncio.to_thread(diagram.render, block)
            width, height = diagram.size(png)
            # Grande como foto, Telegram la comprime y no se lee: va como archivo.
            if max(width, height) > 2560:
                await bot.send_document(chat_id, png, f"DER {code.slug(project)}{f'-{i}' if len(blocks) > 1 else ''}.png", caption)
            else:
                await bot.send_album(chat_id, [(png, caption)])
        except (diagram.DiagramError, subprocess.SubprocessError, httpx.HTTPError, telegram.TelegramError) as exc:
            log.error("no pude mandar el DER de %s: %s", project, exc)
            await bot.send(chat_id, f"⚠️ No pude dibujar el DER de «{project}»: {str(exc)[:300]}")


async def _send_images(chat_id: str | None, images: list[str]) -> None:
    """Las imágenes que generó el rol Imágenes, en un álbum, con el nombre de cada una."""
    if not bot or not chat_id:
        return
    try:
        photos = [
            ((config.VAULT_PATH / path).read_bytes(), PurePath(path).stem)
            for path in images
        ]
        await bot.send_album(chat_id, photos)
    except (OSError, httpx.HTTPError, telegram.TelegramError) as exc:
        log.error("no pude mandar las imágenes generadas: %s", exc)


def _merge_evidence(project: str, state: dict, task: str) -> tuple[str, bool]:
    """Lo que el usuario necesita para aprobar un merge sin hacerlo a ciegas: el PR de cada
    repo, cuánto cambia y los veredictos (pedido del usuario del 5/10/2026). Devuelve el
    texto y si hay algún PR para mirar: sin PR, el pedido no se manda."""
    lines = ["🔍 Para revisar antes de aprobar:"]
    with_pr = False
    for repo in state.get("repos", {}):
        try:
            stat = code.diff_stat(project, repo, task)
            if not stat:
                continue
            pr = code.find_pr(project, repo, task)
            with_pr = with_pr or bool(pr)
            lines.append(f"- {repo}: {pr or 'sin PR abierto'} ({stat})")
        except (code.CodeError, subprocess.SubprocessError) as exc:
            lines.append(f"- {repo}: no pude leer el PR ({str(exc)[:80]})")
    verdicts = code.reviews(vault.project_path(project) / "Desarrollo" / f"{task}.md")
    lines.append(f"Líder técnico: {verdicts['tecnica']} · QA: {verdicts['qa']}")
    lines.append("Si aprobás, el sistema mergea en develop y Jenkins lo construye y lo despliega en desarrollo.")
    return "\n".join(lines), with_pr


@app.post("/aprobaciones", dependencies=[Depends(auth)])
async def request_approval(body: ApprovalIn, x_orc_job: str = Header(default="")) -> dict:
    """Le manda al usuario un pedido de aprobación con botones. Lo usa `orc-aprobacion`."""
    if not bot:
        raise HTTPException(503, "las aprobaciones necesitan el bot de Telegram")
    chat_id = _chat_for(x_orc_job) or body.chat_id
    if not chat_id:
        raise HTTPException(400, "no sé a qué chat mandar la aprobación")
    gate = process.normalize_gate(body.gate)
    state = await asyncio.to_thread(process.load, body.project)
    error = process.validate_gate(body.project, state, gate)
    if error:
        raise HTTPException(409, error)
    summary = body.summary
    swatch = None
    if gate == "paleta":
        # La paleta se aprueba viéndola: va una imagen con los colores y sus contrastes.
        try:
            colors = await asyncio.to_thread(palette.load, body.project)
        except palette.PaletteError as exc:
            raise HTTPException(409, f"La paleta de Diseño/DESIGN.md tiene un problema: {exc}. Pedile a ui que la corrija.") from exc
        if not colors:
            raise HTTPException(409, "Diseño/DESIGN.md todavía no tiene la sección «Paleta» con su bloque JSON: delegá en ui.")
        swatch = await asyncio.to_thread(palette.swatch, colors, f"Paleta · {body.project}")
        summary = f"{summary}\n\n{palette.summary(colors)}"
    if gate.startswith("merge:"):
        evidence, with_pr = await asyncio.to_thread(_merge_evidence, body.project, state, gate[6:])
        if not with_pr and config.GITHUB_CHECKS:
            # Pedir un merge sin nada que mirar es aprobar a ciegas: no se manda.
            raise HTTPException(409, (
                f"{gate[6:]} no tiene ningún PR abierto en GitHub (puede que la rama no se haya "
                "podido subir): no le pidas al usuario aprobar sin PRs. Revisá qué pasó con la "
                "publicación de la rama."))
        summary = f"{summary}\n\n{evidence}"
    approval_id, replaced = process.request(state, gate, summary, chat_id)
    for old in replaced:
        if old.get("message_id"):
            with suppress(httpx.HTTPError, telegram.TelegramError):
                await bot.edit(
                    old["chat_id"], old["message_id"],
                    f"🔁 Este pedido de «{process.gate_label(gate, body.project)}» fue reemplazado por uno nuevo.",
                )
    if swatch:
        try:
            await bot.send_album(chat_id, [(swatch, f"Paleta propuesta para «{body.project}»")])
        except (httpx.HTTPError, telegram.TelegramError) as exc:
            log.error("no pude mandar la imagen de la paleta: %s", exc)
    message_id = await bot.send_buttons(
        chat_id, _approval_text(body.project, gate, summary),
        [[("✅ Aprobar", f"apr:{approval_id}:ok"), ("✏️ Pedir cambios", f"apr:{approval_id}:chg")]],
    )
    state["pendientes"][approval_id]["message_id"] = message_id
    await asyncio.to_thread(process.save, body.project, state, f"pedido de aprobación: {gate}")
    return {
        "id": approval_id,
        "status": "enviado",
        "siguiente": "Terminá tu turno: el usuario responde con los botones y el sistema te avisa.",
    }


@app.post("/mesa", dependencies=[Depends(auth)], status_code=202)
async def start_mesa(body: MesaIn, x_orc_job: str = Header(default="")) -> dict:
    """Convoca la mesa técnica. Corre en background; al terminar despierta al orquestador."""
    chat_id = _chat_for(x_orc_job) or body.chat_id
    if not chat_id:
        raise HTTPException(400, "no sé en qué chat mostrar la mesa")
    state = await asyncio.to_thread(process.load, body.project)
    if state is None:
        raise HTTPException(409, f"«{body.project}» es anterior al proceso: no tiene mesa técnica.")
    if state["fase"] < 2:
        raise HTTPException(409, process.check_role(state, "lider_tecnico"))
    folder = process.mesa_folder(body.project, body.topic)
    steps = len(process.mesa_plan(body.rounds))
    _start_mesa(chat_id, body.project, body.topic, folder, body.rounds)
    return {
        "status": "iniciada",
        "carpeta": folder,
        "pasos": steps,
        "siguiente": "Terminá tu turno: el usuario ve el progreso y el sistema te despierta al terminar.",
    }


@app.post("/diseno", dependencies=[Depends(auth)], status_code=202)
async def start_design(body: DesignIn, x_orc_job: str = Header(default="")) -> dict:
    """Genera pantallas en Stitch o aplica un cambio. Corre en segundo plano."""
    if not stitch.configured():
        raise HTTPException(409, "Falta STITCH_API_KEY en el .env del gateway (se crea en Stitch → Settings → API Keys).")
    chat_id = _chat_for(x_orc_job) or body.chat_id
    if not chat_id:
        raise HTTPException(400, "no sé a qué chat mandar las capturas")
    state = await asyncio.to_thread(process.load, body.project)
    blocked = process.check_role(state, "ui")
    if blocked:
        raise HTTPException(409, blocked)
    problem = await asyncio.to_thread(process.palette_problem, body.project, state)
    if problem:
        raise HTTPException(409, f"Antes de generar pantallas, {problem}: así todas usan los mismos colores.")
    try:
        if body.edit:
            if not body.change:
                raise ValueError("Para editar una pantalla hace falta el cambio a aplicar.")
            if body.edit not in (await asyncio.to_thread(design.load_state, body.project))["screens"]:
                raise ValueError(f"La pantalla «{body.edit}» todavía no está generada.")
        else:
            await asyncio.to_thread(design.specs, body.project)  # falla rápido si UI no la escribió
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _start_design(chat_id, body.project, body.screens, body.edit, body.change)
    return {
        "status": "iniciado",
        "siguiente": "Terminá tu turno: el usuario ve el progreso y las capturas, y el sistema te despierta al terminar.",
    }


@app.post("/repos/pedir", dependencies=[Depends(auth)])
async def ask_repos(body: ReposIn, x_orc_job: str = Header(default="")) -> dict:
    """Le pide al usuario los dos repos del proyecto (los crea él, decisión del 4/10/2026)."""
    if not bot:
        raise HTTPException(503, "pedir los repos necesita el bot de Telegram")
    chat_id = _chat_for(x_orc_job) or body.chat_id
    if not chat_id:
        raise HTTPException(400, "no sé a qué chat mandar el pedido")
    state = await asyncio.to_thread(process.load, body.project)
    if state is None:
        raise HTTPException(409, f"«{body.project}» es anterior al proceso: no tiene repos.")
    state["repos_pedidos"] = {"front": body.front, "back": body.back}
    await asyncio.to_thread(process.save, body.project, state, "repos pedidos al usuario")
    await bot.send(chat_id, (
        f"📦 Para «{body.project}» necesito dos repos en GitHub, privados y con las ramas "
        f"{config.PROD_BRANCH} y {config.DEV_BRANCH}:\n\n"
        f"- Front: {body.front}\n- Back: {body.back}\n\n"
        "Cuando estén, mandame los dos links."
    ))
    return {"status": "enviado", "siguiente": "Terminá el turno: cuando el usuario mande los links, registralos con orc-repos registrar."}


@app.post("/repos/registrar", dependencies=[Depends(auth)])
async def register_repos(body: ReposIn) -> dict:
    """Verifica y clona los repos que creó el usuario: espejo (con GitHub) y clon de trabajo."""
    state = await asyncio.to_thread(process.load, body.project)
    if state is None:
        raise HTTPException(409, f"«{body.project}» es anterior al proceso: no tiene repos.")
    registered = dict(state.get("repos") or {})
    for repo, url in (("front", body.front), ("back", body.back)):
        if repo in registered:
            continue
        try:
            registered[repo] = await asyncio.to_thread(code.register, body.project, repo, url)
        except (code.CodeError, subprocess.TimeoutExpired) as exc:
            if registered:
                state["repos"] = registered
                await asyncio.to_thread(process.save, body.project, state, "repos registrados (en parte)")
            raise HTTPException(409, f"No pude registrar el repo {repo}: {exc}") from exc
    state["repos"] = registered
    ports = await asyncio.to_thread(process.allocate_ports, state)
    await asyncio.to_thread(process.save, body.project, state, "repos registrados")
    jobs, unreadable = [], {}
    if jenkins.configured():
        try:
            jobs = await asyncio.to_thread(jenkins.ensure_jobs, body.project, registered)
        except (jenkins.JenkinsError, httpx.HTTPError) as exc:
            log.warning("no pude crear los jobs de Jenkins de %s: %s", body.project, exc)
            jobs = [f"error: {exc}"]
        else:
            # Que Jenkins pueda leer los repos se sabe recién al escanearlos: mejor ahora
            # que después del primer merge.
            for repo in registered:
                try:
                    cause = await asyncio.to_thread(jenkins.scan_and_wait, body.project, repo)
                except (jenkins.JenkinsError, httpx.HTTPError) as exc:
                    log.warning("no pude escanear %s/%s en Jenkins: %s", body.project, repo, exc)
                    continue
                if cause:
                    unreadable[repo] = cause
    await _update_map()
    result = {"status": "registrados", "repos": registered, "jenkins": jobs, "puertos": ports,
              "siguiente": "Los roles que programan ya pueden trabajar con orc-delegate --tarea."}
    if unreadable:
        await _alert(_scan_alert(body.project, registered, unreadable,
                                 "Hasta que lo arregles, Jenkins no va a construir develop ni master."))
        result["jenkins_no_puede_leer"] = unreadable
        result["siguiente"] += (" Jenkins no puede leer " + ", ".join(unreadable) + ": el usuario ya recibió "
                                "el aviso con cómo arreglarlo. No frena el trabajo en las ramas de tarea.")
    return result


def _scan_alert(project: str, repos: dict[str, dict], problems: dict[str, str], after: str) -> str:
    """Aviso de que Jenkins no puede leer repos de un proyecto, con la causa y cómo arreglarlo."""
    names = " y ".join(f"{r} ({repos[r]['github']})" for r in problems)
    text = f"⚠️ Jenkins no puede leer {'el repo' if len(problems) == 1 else 'los repos'} {names} de «{project}»:\n"
    text += "".join(f"\n{r}: {cause}" for r, cause in problems.items())
    if any(jenkins.credential_problem(c) for c in problems.values()):
        owners = sorted({repos[r]["github"].split("/")[0] for r in problems})
        text += (
            f"\n\nLa credencial «{config.JENKINS_CREDENTIALS_ID}» de Jenkins no tiene acceso (o venció). Un token "
            "fine-grained de GitHub cubre los repos de un solo dueño: tiene que tener como Resource owner a "
            f"{' y '.join(owners)}, con Contents de solo lectura. Cargalo en Jenkins → Manage Jenkins → "
            f"Credentials → «{config.JENKINS_CREDENTIALS_ID}» → Update."
        )
    return f"{text}\n\n{after}"


@app.post("/autenticacion/alta", dependencies=[Depends(auth)])
async def auth_signup(body: AuthIn, x_orc_job: str = Header(default="")) -> dict:
    """Da de alta (o actualiza) el proyecto en la autenticación compartida de desarrollo según
    «Roles y permisos» del 01: su sociedad, roles y permisos, y la primera vez el admin, cuya
    contraseña temporal le llega solo al usuario."""
    state = await asyncio.to_thread(process.load, body.project)
    if state is None:
        raise HTTPException(409, f"«{body.project}» es anterior al proceso: no tiene autenticación.")
    try:
        data = await asyncio.to_thread(autenticacion.load, body.project)
    except autenticacion.AuthError as exc:
        raise HTTPException(409, f"{exc}. Delegá en producto que lo complete: sección «Roles y permisos» del 01, "
                                 "con el bloque ```json que pide su guía.") from exc
    current = (state.get("autenticacion") or {}).get("desarrollo") or {}
    try:
        result = await asyncio.to_thread(autenticacion.apply, body.project, data, current.get("idsociedad"))
        admin_role = next(r["nombre"] for r in data["roles"] if r.get("admin"))
        password = None if current.get("admin") else await asyncio.to_thread(
            autenticacion.create_admin, result["idsociedad"], result["roles"][admin_role])
    except (autenticacion.AuthError, subprocess.SubprocessError) as exc:
        raise HTTPException(502, f"No pude dar de alta «{body.project}» en la autenticación: {exc}") from exc
    entry = {**result, "admin": "admin"}
    state.setdefault("autenticacion", {})["desarrollo"] = entry
    await asyncio.to_thread(process.save, body.project, state, "alta en la autenticación de desarrollo")
    await _update_map()
    if password:
        # Directo al usuario: la contraseña no pasa por el Director ni queda en el vault.
        text = (f"🔐 «{body.project}» ya tiene login en desarrollo (sociedad {result['idsociedad']}).\n\n"
                f"Usuario: admin\nContraseña temporal: {password}\n\n"
                "Cambiala al entrar y borrá este mensaje. Es solo para desarrollo.")
        chat = _chat_for(x_orc_job)
        if chat and bot:
            await bot.send(chat, text)
        else:
            await _alert(text)
    return {"status": "ok", "desarrollo": entry, "admin_creado": bool(password),
            "siguiente": ("Listo. " + ("El usuario ya recibió la contraseña del admin. " if password else "")
                          + "Los roles que programan ven el idsociedad en su nota de código. Si cambian los "
                            "roles o permisos del 01, volvé a correr orc-autenticacion.")}


class MergeError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


async def _merge_task(project: str, task: str, chat_id: str | None) -> dict:
    """Mergea una tarea en develop (con QA y el Líder técnico aprobados), avisa a Jenkins y
    sigue el build de develop. Lo usan /merge y el botón de aprobación del merge."""
    state = await asyncio.to_thread(process.load, project)
    if not state or not state.get("repos"):
        raise MergeError(409, f"«{project}» no tiene repos registrados.")
    task_file = vault.project_path(project) / "Desarrollo" / f"{task}.md"
    verdicts = code.reviews(task_file)
    missing = [code.REVIEWS[k] for k, v in verdicts.items() if not code.approved(v)]
    if missing:
        detail = "; ".join(f"{code.REVIEWS[k]}: {v or 'todavía no revisó'}" for k, v in verdicts.items())
        raise MergeError(409, f"No se mergea {task}: falta la aprobación de {', '.join(missing)} ({detail}).")
    # El build de develop que había antes del merge: después se espera uno nuevo.
    previous = {}
    if jenkins.configured():
        for repo in state["repos"]:
            try:
                previous[repo] = await asyncio.to_thread(jenkins.last_number, project, repo, config.DEV_BRANCH)
            except httpx.HTTPError:
                previous[repo] = None
    merged = {}
    with runner.project_lock(project):
        for repo in state["repos"]:
            try:
                commit = await asyncio.to_thread(code.merge_task, project, repo, task)
            except (code.CodeError, subprocess.SubprocessError) as exc:
                raise MergeError(409, str(exc)) from exc
            if commit:
                merged[repo] = commit
        if not merged:
            raise MergeError(409, f"{task} no tiene cambios para mergear en ningún repo.")
        note = (f"\n\n## Merge\n\n{datetime.now().astimezone():%d/%m/%Y %H:%M} · en {config.DEV_BRANCH}: "
                + ", ".join(f"{r} {c}" for r, c in merged.items()) + "\n")
        await asyncio.to_thread(lambda: task_file.write_text(task_file.read_text(encoding="utf-8") + note, encoding="utf-8"))
        await asyncio.to_thread(vault.commit_paths, [vault.relative(task_file)], f"merge de {task} en {project}")
    for repo in merged:
        await asyncio.to_thread(runner._notify_jenkins, project, repo)
        if previous.get(repo) is not None:
            _ci_put({"proyecto": project, "repo": repo, "tarea": task, "previo": previous[repo], "chat_id": chat_id})
            _watch(project, repo)
    return {"status": "mergeada", "tarea": task, "merges": merged,
            "siguiente": "El sistema sigue el build de develop: si falla, les avisa a vos y al usuario, con el log."}


@app.post("/merge", dependencies=[Depends(auth)])
async def merge(body: MergeIn, x_orc_job: str = Header(default="")) -> dict:
    """Mergea una tarea en develop, solo si el Líder técnico y QA la aprobaron en
    Desarrollo/<tarea>.md. Las de MERGE_NEEDS_APPROVAL (el esqueleto) necesitan además
    la aprobación del usuario: esas las mergea el sistema cuando toca «Aprobar»."""
    try:
        task = code.valid_task(body.tarea)
    except code.CodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if task in config.MERGE_NEEDS_APPROVAL:
        state = await asyncio.to_thread(process.load, body.project)
        if not (state or {}).get("aprobaciones", {}).get(f"merge:{task}"):
            raise HTTPException(409, (
                f"El merge de {task} necesita la aprobación del usuario. Pedila con: orc-aprobacion "
                f"\"{body.project}\" merge:{task} \"<resumen>\". El sistema le agrega los links de los PRs y, "
                "si aprueba, mergea solo: no uses orc-merge."))
    try:
        return await _merge_task(body.project, task, _chat_for(x_orc_job))
    except MergeError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


_background: set[asyncio.Task] = set()


def _track_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


_ci_watching: set[tuple[str, str]] = set()


def _watch(project: str, repo: str) -> None:
    """Arranca el seguimiento de develop de ese repo, si no hay uno andando: el que anda
    relee ci_develop.json en cada vuelta, así que toma un merge nuevo solo."""
    if (project, repo) not in _ci_watching:
        _ci_watching.add((project, repo))
        _track_background(_watch_develop(project, repo))


async def _watch_develop(project: str, repo: str) -> None:
    """Sigue el build de develop que dispara un merge (anotado en ci_develop.json) hasta que
    termina. Si Jenkins no puede ni leer el repo (credencial sin acceso o vencida), avisa
    con la causa y sigue esperando: cuando se arregla, el build sale solo. Si el escaneo
    anda pero el build no llega, avisa y deja de esperar."""
    waited = since_scan = 0
    try:
        while True:
            await asyncio.sleep(CI_POLL)
            entry = _ci_get(project, repo)
            if not entry:
                return
            try:
                status = await asyncio.to_thread(jenkins.branch_status, project, repo, config.DEV_BRANCH)
                build = status.get("build")
                if build and build.get("number", 0) > entry["previo"]:
                    if build.get("building"):
                        continue
                    _ci_drop(project, repo)
                    await _develop_built(entry, build)
                    return
                cause = jenkins.scan_failure(await asyncio.to_thread(jenkins.scan_log, project, repo, 400))
            except (httpx.HTTPError, jenkins.JenkinsError) as exc:  # Jenkins reiniciándose: se reintenta
                log.warning("no pude consultar Jenkins por %s/%s: %s", project, repo, exc)
                continue
            if cause:
                waited = 0
                if entry.get("aviso") != cause:
                    entry["aviso"] = cause
                    _ci_put(entry)
                    state = await asyncio.to_thread(process.load, project) or {}
                    await _alert(_scan_alert(project, state.get("repos", {}), {repo: cause}, (
                        f"El merge de {entry['tarea']} ya está en {config.DEV_BRANCH}: cuando Jenkins pueda "
                        "leer el repo, el build sale solo y te aviso.")))
                since_scan += CI_POLL
                if since_scan >= CI_RESCAN:
                    since_scan = 0
                    with suppress(httpx.HTTPError, jenkins.JenkinsError):
                        await asyncio.to_thread(jenkins.scan, project, repo)
                continue
            if entry.pop("aviso", None):
                _ci_put(entry)
                await _alert(f"✅ Jenkins ya puede leer {repo} de «{project}»: espero el build de {config.DEV_BRANCH}.")
            waited += CI_POLL
            if waited >= CI_BUILD_WAIT:
                _ci_drop(project, repo)
                await _develop_missing(entry)
                return
    except Exception:  # noqa: BLE001 — que un error raro no se lleve el aviso en silencio
        log.exception("error inesperado siguiendo develop de %s/%s", project, repo)
        await _alert(f"⚠️ Se cortó el seguimiento del build de {config.DEV_BRANCH} de «{project}» ({repo}) "
                     "por un error del gateway: mirá Jenkins a mano.")
    finally:
        _ci_watching.discard((project, repo))


async def _develop_built(entry: dict, build: dict) -> None:
    """Terminó el build de develop después de un merge: se anota en la tarea; si falló, se
    avisa al usuario y se despierta al Director para que pida el arreglo."""
    project, repo, task, chat_id = entry["proyecto"], entry["repo"], entry["tarea"], entry.get("chat_id")
    result = build.get("result")
    await _update_map()
    task_file = vault.project_path(project) / "Desarrollo" / f"{task}.md"
    if task_file.exists():
        line = (f"\n- Jenkins, {config.DEV_BRANCH} de {repo} después del merge: "
                f"{'✅' if result == 'SUCCESS' else '❌'} {result} ({build.get('url')})\n")
        try:  # la nota es lo de menos: que no se lleve el aviso ni el avance de fase
            await asyncio.to_thread(lambda: task_file.write_text(task_file.read_text(encoding="utf-8") + line, encoding="utf-8"))
            await asyncio.to_thread(vault.commit_paths, [vault.relative(task_file)], f"Jenkins: {task} en {config.DEV_BRANCH} de {repo}")
        except (OSError, RuntimeError) as exc:
            log.warning("no pude anotar el build de %s en %s: %s", task, task_file, exc)
    if result == "SUCCESS":
        if task in config.MERGE_NEEDS_APPROVAL:  # el usuario aprobó ese merge: espera el resultado
            await _alert(f"✅ Jenkins en verde en {config.DEV_BRANCH} de «{project}» ({repo}) con {task}.")
        if task == "esqueleto":
            await _maybe_finish_skeleton(chat_id, project)
        return
    tail = await asyncio.to_thread(jenkins.console_tail, project, repo, config.DEV_BRANCH, 25)
    await _alert(f"❌ Jenkins falló en {config.DEV_BRANCH} de «{project}» ({repo}) después de mergear {task}.\n\n{tail[-1500:]}")
    if chat_id:
        _start_job(ChatIn(chat_id=chat_id, text=(
            f"(Mensaje del sistema) Jenkins falló en {config.DEV_BRANCH} del repo {repo} de «{project}» "
            f"después de mergear {task} (el usuario ya vio el log). Final del log:\n{tail[-2500:]}\n\n"
            "Qué hacer: delegá el arreglo con orc-delegate --tarea (una tarea nueva de arreglo, o la "
            "misma si corresponde), con el log en la instrucción, y después revisión, validación y merge."
        )))


async def _develop_missing(entry: dict) -> None:
    """Jenkins lee el repo pero develop no se construyó: casi seguro, no tiene Jenkinsfile."""
    project, repo, task, chat_id = entry["proyecto"], entry["repo"], entry["tarea"], entry.get("chat_id")
    minutes = CI_BUILD_WAIT // 60
    await _alert(f"⚠️ Jenkins lee bien {repo} de «{project}», pero {config.DEV_BRANCH} no se construyó en "
                 f"{minutes} minutos después de mergear {task}. Lo más probable: no tiene Jenkinsfile en la raíz.")
    if chat_id:
        _start_job(ChatIn(chat_id=chat_id, text=(
            f"(Mensaje del sistema) {config.DEV_BRANCH} del repo {repo} de «{project}» no se construyó en "
            f"{minutes} minutos después de mergear {task}, aunque Jenkins lee el repo (el usuario ya lo sabe). "
            f"Qué hacer: mirá con orc-ci \"{project}\" {config.DEV_BRANCH} y, si falta el Jenkinsfile, delegá "
            "el arreglo al devops con orc-delegate --tarea."
        )))


async def _maybe_finish_skeleton(chat_id: str | None, project: str) -> None:
    """Fase 4 sin puerta: con el esqueleto en develop y Jenkins en verde en todos los
    repos, el proyecto pasa a la fase 5 y se despierta al Director."""
    state = await asyncio.to_thread(process.load, project)
    if not state or state.get("fase") != 4:
        return
    for repo in state.get("repos", {}):
        status = await asyncio.to_thread(jenkins.branch_status, project, repo, config.DEV_BRANCH)
        build = status.get("build")
        if not build or build.get("building") or build.get("result") != "SUCCESS":
            return  # otro repo todavía no terminó: su propio seguimiento vuelve a llamar acá
    new_phase = process.advance_after_skeleton(state)
    await asyncio.to_thread(process.save, project, state, f"esqueleto en develop con Jenkins en verde: fase {new_phase}")
    if chat_id:
        current = process.phase(new_phase)
        _start_job(ChatIn(chat_id=chat_id, text=(
            f"(Mensaje del sistema) El esqueleto de «{project}» quedó en develop y Jenkins está en verde: "
            f"el proyecto pasó a la fase {current.label}. Contale al usuario en pocas líneas, con el puerto "
            f"de desarrollo ({state.get('puertos', {}).get('desarrollo')}). Qué hacer ahora: {current.guide}"
        )))


@app.get("/ci/{project}/{task}", dependencies=[Depends(auth)])
async def ci_status(project: str, task: str) -> dict:
    """Estado de Jenkins para la rama de una tarea (o develop/master), por repo. Lo usa orc-ci."""
    if not jenkins.configured():
        raise HTTPException(409, "Jenkins no está configurado (JENKINS_URL, JENKINS_USER, JENKINS_TOKEN en el .env).")
    state = await asyncio.to_thread(process.load, project)
    if not state or not state.get("repos"):
        raise HTTPException(409, f"«{project}» no tiene repos registrados.")
    result = {}
    for repo in state["repos"]:
        try:
            status = await asyncio.to_thread(jenkins.branch_status, project, repo, task)
            build = status.get("build")
            entry = {"job": status["job"], "resultado": None, "corriendo": False, "url": None}
            if build:
                entry.update(resultado=build.get("result"), corriendo=build.get("building"), url=build.get("url"))
                if build.get("result") not in (None, "SUCCESS"):
                    entry["log"] = await asyncio.to_thread(jenkins.console_tail, project, repo, task)
            elif not status["job"]:
                entry["nota"] = "la rama no tiene job: no tiene Jenkinsfile o Jenkins todavía no la escaneó"
                entry["escaneo"] = await asyncio.to_thread(jenkins.scan_log, project, repo, 15)
        except httpx.HTTPError as exc:
            entry = {"error": str(exc)}
        result[repo] = entry
    return {"proyecto": project, "rama": task, "repos": result}


@app.post("/autoprueba", dependencies=[Depends(auth)])
async def run_selftest() -> dict:
    """Corre la autoprueba de los motores ya, y avisa por Telegram si algo falla."""
    previous = selftest.last()
    result = await selftest.run()
    notes = _selftest_notes(previous, result)
    if notes:
        await _alert("\n\n".join(notes))
    return result


@app.post("/drive/revision", dependencies=[Depends(auth)])
async def drive_check() -> dict:
    """Revisión del espacio del Drive de los backups. La llama cada backup antes de subir
    (pedido del usuario: revisar solo en ese momento, no periódicamente)."""
    if not Path(config.RCLONE_BIN).exists():
        raise HTTPException(503, f"rclone no está instalado en {config.RCLONE_BIN}")
    await _check_drive()
    if _drive["error"]:
        raise HTTPException(502, f"no pude revisar el Drive: {_drive['error']}")
    u = _drive["uso"]
    limit = config.DRIVE_ALERT_GB * drive.GIB
    return {
        "usado": u["usado"],
        "total": u["total"],
        "libre": u["total"] - u["usado"],
        "umbral": int(limit),
        "sobre_el_umbral": u["usado"] >= limit,
        "client_id_compartido": u["client_id_compartido"],
    }


@app.get("/proyectos/{project}/estado", dependencies=[Depends(auth)])
async def project_status(project: str) -> dict:
    try:
        return {"proyecto": project, "estado": await asyncio.to_thread(process.status_text, project)}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


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
