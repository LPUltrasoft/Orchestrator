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

from . import code, config, design, drive, jenkins, jobs, palette, process, progress, quota, runner, selftest, sessions, stitch, telegram, vault

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
                f"pausa: delegá en {paused['role']} sobre el proyecto «{paused['project']}» "
                f"con esta instrucción:\n\n{paused['instruction']}",
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

    poller = None
    if config.TELEGRAM_BOT_TOKEN:
        bot = telegram.TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_API_BASE)
        poller = asyncio.create_task(bot.poll_forever(_on_telegram_message, _on_telegram_callback))
    pusher = None
    if config.VAULT_AUTOCOMMIT and config.VAULT_AUTOPUSH:
        pusher = asyncio.create_task(_vault_pusher())
    unclean = _RUNNING_MARK.exists()
    _RUNNING_MARK.write_text(datetime.now(timezone.utc).isoformat())
    monitors = [asyncio.create_task(_startup_checks(unclean)), asyncio.create_task(_daily_selftest())]

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
            task=body.tarea,
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
    if result.get("images"):
        await _send_images(_chat_for(x_orc_job), result["images"])
    return result


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
    await asyncio.to_thread(process.save, body.project, state, "repos registrados")
    jobs = []
    if jenkins.configured():
        try:
            jobs = await asyncio.to_thread(jenkins.ensure_jobs, body.project, registered)
        except (jenkins.JenkinsError, httpx.HTTPError) as exc:
            log.warning("no pude crear los jobs de Jenkins de %s: %s", body.project, exc)
            jobs = [f"error: {exc}"]
    return {"status": "registrados", "repos": registered, "jenkins": jobs,
            "siguiente": "Los roles que programan ya pueden trabajar con orc-delegate --tarea."}


@app.post("/merge", dependencies=[Depends(auth)])
async def merge(body: MergeIn) -> dict:
    """Mergea una tarea en develop, solo si el Líder técnico y QA la aprobaron en
    Desarrollo/<tarea>.md (decisión del usuario: merges automáticos con esas dos)."""
    try:
        task = code.valid_task(body.tarea)
    except code.CodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    state = await asyncio.to_thread(process.load, body.project)
    if not state or not state.get("repos"):
        raise HTTPException(409, f"«{body.project}» no tiene repos registrados.")
    task_file = vault.project_path(body.project) / "Desarrollo" / f"{task}.md"
    verdicts = code.reviews(task_file)
    missing = [code.REVIEWS[k] for k, v in verdicts.items() if not code.approved(v)]
    if missing:
        detail = "; ".join(f"{code.REVIEWS[k]}: {v or 'todavía no revisó'}" for k, v in verdicts.items())
        raise HTTPException(409, f"No se mergea {task}: falta la aprobación de {', '.join(missing)} ({detail}).")
    if config.MERGE_REQUIRES_CI and jenkins.configured():
        red = []
        for repo in state["repos"]:
            try:
                status = await asyncio.to_thread(jenkins.branch_status, body.project, repo, task)
            except httpx.HTTPError as exc:
                raise HTTPException(503, f"No pude consultar Jenkins: {exc}") from exc
            build = status.get("build")
            if status["job"] and (not build or build.get("building") or build.get("result") != "SUCCESS"):
                state_text = "corriendo" if build and build.get("building") else (build or {}).get("result") or "sin build todavía"
                red.append(f"{repo}: {state_text}")
        if red:
            raise HTTPException(409, f"No se mergea {task}: Jenkins no está en verde ({'; '.join(red)}). Consultá el detalle con orc-ci.")
    merged = {}
    with runner.project_lock(body.project):
        for repo in state["repos"]:
            try:
                commit = await asyncio.to_thread(code.merge_task, body.project, repo, task)
            except (code.CodeError, subprocess.SubprocessError) as exc:
                raise HTTPException(409, str(exc)) from exc
            if commit:
                merged[repo] = commit
        if not merged:
            raise HTTPException(409, f"{task} no tiene cambios para mergear en ningún repo.")
        note = (f"\n\n## Merge\n\n{datetime.now().astimezone():%d/%m/%Y %H:%M} · en {config.DEV_BRANCH}: "
                + ", ".join(f"{r} {c}" for r, c in merged.items()) + "\n")
        await asyncio.to_thread(lambda: task_file.write_text(task_file.read_text(encoding="utf-8") + note, encoding="utf-8"))
        await asyncio.to_thread(vault.commit_paths, [vault.relative(task_file)], f"merge de {task} en {body.project}")
    for repo in merged:
        await asyncio.to_thread(runner._notify_jenkins, body.project, repo)
    return {"status": "mergeada", "tarea": task, "merges": merged}


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
