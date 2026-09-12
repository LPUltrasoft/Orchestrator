"""Invocación de `agy` en print mode, con lock y verificación."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from . import config, vault

# Claves que devuelve agy --output-format json
JSON_KEYS = ("conversation_id", "status", "response")


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


def _parse_output(stdout: str) -> dict:
    """agy imprime una línea JSON; tomamos la última que parsee."""
    for line in reversed([l for l in stdout.splitlines() if l.strip()]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and any(k in data for k in JSON_KEYS):
            return data
    raise RuntimeError(f"agy no devolvió JSON parseable. Salida cruda:\n{stdout[-2000:]}")


async def run_agy(
    *,
    prompt: str,
    model: str,
    cwd: Path,
    conversation_id: str | None = None,
    extra_dirs: list[Path] | None = None,
    timeout: int | None = None,
) -> dict:
    """Corre agy y devuelve su JSON. No verifica efectos: eso lo hace el caller."""
    argv = [
        config.AGY_BIN,
        "--print",
        prompt,
        "--output-format", "json",
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

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(
            process.communicate(), timeout=timeout or config.JOB_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(
            f"agy superó el timeout de {timeout or config.JOB_TIMEOUT}s y fue cancelado"
        )

    stdout = raw_out.decode("utf-8", "replace")
    stderr = raw_err.decode("utf-8", "replace")
    _check_auth(stdout, stderr)
    if process.returncode != 0 and not stdout.strip():
        raise RuntimeError(f"agy salió con código {process.returncode}: {stderr[-2000:]}")

    data = _parse_output(stdout)

    # En headless, un tool sin allow-rule se auto-deniega y la respuesta vuelve vacía.
    denied = data.get("denied_actions") or []
    if denied and not (data.get("response") or "").strip():
        names = ", ".join(d.get("display_name") or d.get("action", "?") for d in denied)
        raise RuntimeError(
            f"agy denegó herramientas por falta de permisos en headless: {names}. "
            f"Agregá la allow-rule correspondiente en "
            f"~/.gemini/antigravity-cli/settings.json (permissions.allow), "
            f"por ejemplo command(orc-delegate)."
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

# Tarea

{instruction}

# Formato de tu respuesta

Terminá con un resumen de máximo 8 líneas: qué archivos escribiste o modificaste, las
decisiones importantes que tomaste, y el siguiente paso recomendado (qué sub-agente y
para qué). Es lo único que va a leer el Director de Proyecto.
"""


async def invoke_subagent(role: str, project: str, instruction: str) -> dict:
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
        )
        # git bloquea: fuera del event loop para no frenar las demás requests.
        files_changed = await asyncio.to_thread(vault.changed_since, before)
        commit_hash = await asyncio.to_thread(vault.commit, role, project, files_changed)

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
