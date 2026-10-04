"""Invocación de `agy` en print mode, con lock y verificación."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from . import config, vault

log = logging.getLogger("orchestrator.runner")

@contextmanager
def project_lock(name: str):
    """Un sub-agente por proyecto: dos escribiendo el mismo .md se pisan."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower() or "default"
    lock_file = config.LOCK_DIR / f"{slug}.lock"
    handle = lock_file.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"ya hay un agente trabajando en '{name}'. Esperá a que termine."
            ) from exc
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


DENIED_RETRY_PROMPT = (
    "El comando shell que intentaste fue denegado: en este entorno corrés sin "
    "supervisión y casi ningún comando está permitido. No lo vuelvas a intentar ni "
    "pruebes variantes. Usá tus herramientas nativas (list_dir, view_file, "
    "grep_search, find_by_name) y seguí con la tarea original."
)

AUTH_MARKERS = (
    "Authentication required",
    "accounts.google.com/o/oauth2",
    "Waiting for authentication",
    "paste the authorization code",
)


def _check_auth(stdout: str, stderr: str) -> None:
    """agy pide re-login por stdout y se queda esperando: eso no es una respuesta."""
    blob = f"{stdout}\n{stderr}"
    if any(marker in blob for marker in AUTH_MARKERS):
        raise RuntimeError(
            "la sesión de agy expiró. Corré `agy` en una terminal, completá el login "
            "de Google y volvé a intentar."
        )


StepCallback = Callable[[dict], None]


def _result_from_stream(lines: list[str]) -> dict:
    """El evento `result` del stream trae lo mismo que --output-format json."""
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            return event.get("result") or {}
    tail = "\n".join(lines)[-2000:]
    raise RuntimeError(f"agy no devolvió un resultado. Salida cruda:\n{tail}")


def _notify_step(on_step: StepCallback | None, step: dict) -> None:
    if not on_step:
        return
    try:
        on_step(step)
    except Exception:  # noqa: BLE001 - el progreso es cosmético: nunca tumba un turno
        log.exception("falló el callback de progreso")


async def run_agy(
    *,
    prompt: str,
    model: str,
    cwd: Path,
    conversation_id: str | None = None,
    extra_dirs: list[Path] | None = None,
    timeout: int | None = None,
    denied_retries: int = 2,
    caller: str = "orchestrator",
    on_step: StepCallback | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Corre agy y devuelve su resultado. No verifica efectos: eso lo hace el caller.

    Con --output-format stream-json cada paso del agente (qué archivo lee, cuál
    escribe) llega a `on_step` en el momento: así se muestra el progreso en vivo.
    """
    argv = [
        config.AGY_BIN,
        "--print",
        prompt,
        "--output-format", "stream-json",
        # Sin esto, agy responde como si hubiera usado herramientas pero no las usa.
        "--mode", "accept-edits",
        "--model", model,
        "--add-dir", str(config.VAULT_PATH),
    ]
    for extra in extra_dirs or []:
        argv += ["--add-dir", str(extra)]
    if conversation_id:
        argv += ["--conversation", conversation_id]

    env = os.environ.copy()
    # El orquestador llama a `orc-delegate` con run_command.
    env["PATH"] = f"{config.BIN_DIR}:{env.get('PATH', '')}"
    env["ORC_VAULT_PATH"] = str(config.VAULT_PATH)
    env["ORC_GATEWAY_URL"] = f"http://127.0.0.1:{config.PORT}"
    env["ORC_TOKEN"] = config.TOKEN
    # orc-delegate se niega si no lo llama el orquestador: un sub-agente que
    # delega dispara otro sub-agente, y así sin fin.
    env["ORC_CALLER"] = caller
    env.update(extra_env or {})

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        # Sin stdin: si agy pide re-login, que no se quede esperando una respuesta.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # El evento final trae la respuesta entera en una sola línea.
        limit=16 * 1024 * 1024,
    )
    lines: list[str] = []
    last_command = ""

    async def pump() -> None:
        nonlocal last_command
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # texto suelto, como el pedido de login
            step = event.get("step_update") if isinstance(event, dict) else None
            if not step:
                continue
            if step.get("tool_name") == "run_command":
                params = (step.get("tool_info") or {}).get("parameters") or {}
                last_command = params.get("CommandLine", "") or last_command
            _notify_step(on_step, step)

    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        await asyncio.wait_for(pump(), timeout=timeout or config.JOB_TIMEOUT)
        await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        stderr_task.cancel()
        raise RuntimeError(
            f"agy superó el timeout de {timeout or config.JOB_TIMEOUT}s y fue cancelado"
        )

    stderr = (await stderr_task).decode("utf-8", "replace")
    stdout = "\n".join(lines)
    _check_auth(stdout, stderr)
    if process.returncode != 0 and not lines:
        raise RuntimeError(f"agy salió con código {process.returncode}: {stderr[-2000:]}")

    data = _result_from_stream(lines)

    # En headless, un tool sin allow-rule se auto-deniega y la respuesta vuelve vacía.
    denied = data.get("denied_actions") or []
    if denied and not (data.get("response") or "").strip():
        names = ", ".join(d.get("display_name") or d.get("action", "?") for d in denied)
        detail = f"{names} (`{last_command}`)" if last_command else names
        # Cada pregunta nueva lleva al modelo a otro comando shell (find, wc, stat...):
        # agrandar la allowlist cada vez no termina nunca, y algunos (find -delete,
        # find -exec) no son seguros. Mejor reanudar la conversación y reencauzarlo.
        if denied_retries > 0 and data.get("conversation_id"):
            log.warning("agy denegó %s; reanudo la conversación con una indicación", detail)
            _notify_step(on_step, {"synthetic": "retry", "detail": last_command or names})
            return await run_agy(
                prompt=DENIED_RETRY_PROMPT,
                model=model,
                cwd=cwd,
                conversation_id=data["conversation_id"],
                extra_dirs=extra_dirs,
                timeout=timeout,
                denied_retries=denied_retries - 1,
                caller=caller,
                on_step=on_step,
                extra_env=extra_env,
            )
        raise RuntimeError(
            f"agy denegó herramientas por falta de permisos en headless: {detail}. "
            f"Agregá la allow-rule correspondiente en "
            f"~/.gemini/antigravity-cli/settings.json (permissions.allow)."
        )

    # Respuesta vacía con usage en cero = el turno nunca corrió de verdad.
    if not (data.get("response") or "").strip():
        usage = data.get("usage") or {}
        if not usage.get("total_tokens"):
            raise RuntimeError(
                f"agy no ejecutó el turno (respuesta vacía, sin consumo de tokens). "
                f"status={data.get('status')}. stderr: {stderr[-500:]}"
            )
    return data


def build_subagent_prompt(role: str, project: str, instruction: str, project_path: Path) -> str:
    """Prompt autocontenido: el sub-agente no ve la charla de Telegram."""
    return f"""{config.prompt_for(role)}

---

# Contexto de esta invocación

- **Vault de Obsidian:** `{config.VAULT_PATH}`
- **Proyecto:** {project}
- **Carpeta del proyecto (escribí acá):** `{project_path}`
- Tu directorio de trabajo actual ya es la carpeta del proyecto.

Antes de escribir, listá la carpeta y leé los documentos que ya existan.

# Herramientas

Trabajá solo con tus herramientas nativas de archivos: `list_dir`, `view_file`,
`grep_search`, `find_by_name`, `write_to_file` y `replace_file_content`. **No uses
`run_command`**: corrés sin supervisión y cualquier comando shell fuera de una lista
corta se deniega automáticamente, lo que corta tu trabajo a la mitad.

# Tarea

{instruction}

# Formato de tu respuesta

Terminá con un resumen de máximo 8 líneas: qué archivos escribiste o modificaste, las
decisiones importantes que tomaste, y el siguiente paso recomendado (qué sub-agente y
para qué). Es lo único que va a leer el Director de Proyecto.
"""


async def invoke_subagent(
    role: str, project: str, instruction: str, on_step: StepCallback | None = None
) -> dict:
    """Corre un sub-agente y verifica con git lo que realmente cambió."""
    if role not in config.ROLES:
        raise ValueError(f"rol desconocido: {role}. Válidos: {', '.join(config.ROLES)}")

    project_path = await asyncio.to_thread(vault.project_dir, project)
    with project_lock(project):
        before = await asyncio.to_thread(vault.snapshot)
        result = await run_agy(
            prompt=build_subagent_prompt(role, project, instruction, project_path),
            model=config.MODELS[role],
            cwd=project_path,
            caller=role,
            on_step=on_step,
        )
        # git bloquea: fuera del event loop para no frenar las demás requests.
        files_changed = await asyncio.to_thread(vault.changed_since, before)
        complete = result.get("status") == "SUCCESS"
        commit_hash = await asyncio.to_thread(
            vault.commit, role, project, files_changed, complete
        )

    if not complete:
        # agy puede cortar a mitad de camino (señal, timeout interno) y aun así
        # devolver JSON prolijo: un documento a medias no es un éxito.
        written = ", ".join(files_changed) or "ninguno"
        raise RuntimeError(
            f"el sub-agente {role} terminó con status={result.get('status')}. "
            f"Archivos que llegó a escribir: {written}"
            + (f" (commit {commit_hash}, marcado como INCOMPLETO)." if commit_hash else ".")
        )

    claimed = bool(re.search(r"escrib|cre[éeo]|actualic|modific", result.get("response", ""), re.I))
    return {
        "role": role,
        "project": project,
        "status": result.get("status"),
        "response": result.get("response", "").strip(),
        "files_changed": files_changed,
        "commit": commit_hash,
        "usage": result.get("usage", {}),
        # Señal de alerta: dijo que escribió pero git no vio nada.
        "suspect_no_writes": claimed and not files_changed,
    }
