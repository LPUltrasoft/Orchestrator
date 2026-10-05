"""Invocación de `agy` en print mode, con lock y verificación."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from . import code, config, jenkins, vault

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


# Corte de red a mitad de una corrida de Claude (verificado el 4/10/2026: "API Error:
# Can't reach the API server — check your internet or DNS (EAI_AGAIN)" después de
# varios minutos de reintentos propios). Se retoma la misma sesión cuando vuelve la red.
_NETWORK_ERROR = re.compile(
    r"API Error: (Can't reach the API server|Connection error|Request timed out)"
    r"|EAI_AGAIN|ECONNRESET|ETIMEDOUT|ENOTFOUND",
    re.I,
)
NETWORK_RETRY_PROMPT = "Se cortó la conexión a mitad de tu trabajo. Seguí exactamente donde quedaste."
NETWORK_RETRY_WAIT = int(os.environ.get("NETWORK_RETRY_WAIT", "60"))

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


def _denied_detail(denied: list[dict], last_by_tool: dict[str, dict]) -> str:
    """Qué se denegó y sobre qué. 'RunCommand' no es la única: también 'ViewFile' de
    un archivo fuera del vault. Mostrar siempre el último comando confundía."""
    parts = []
    for item in denied:
        name = item.get("display_name") or item.get("action", "?")
        tool = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()  # ViewFile -> view_file
        params = last_by_tool.get(tool) or {}
        target = next(
            (params[k] for k in ("CommandLine", "AbsolutePath", "TargetFile", "DirectoryPath")
             if params.get(k)),
            "",
        )
        parts.append(f"{name} ({target[:150]})" if target else name)
    return ", ".join(parts)


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
    last_by_tool: dict[str, dict] = {}

    async def pump() -> None:
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
            if step.get("step_type") == "tool" and step.get("tool_name"):
                params = (step.get("tool_info") or {}).get("parameters") or {}
                last_by_tool[step["tool_name"]] = params
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
        detail = _denied_detail(denied, last_by_tool)
        # Cada pregunta nueva lleva al modelo a otro comando shell (find, wc, stat...):
        # agrandar la allowlist cada vez no termina nunca, y algunos (find -delete,
        # find -exec) no son seguros. Mejor reanudar la conversación y reencauzarlo.
        if denied_retries > 0 and data.get("conversation_id"):
            log.warning("agy denegó %s; reanudo la conversación con una indicación", detail)
            _notify_step(on_step, {"synthetic": "retry", "detail": detail})
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


# ── Motor Claude Code ───────────────────────────────────────────────────────────

# Exactamente estas herramientas: sin shell, sin publicar páginas, sin programar tareas
# ni lanzar otros agentes (con la configuración por defecto, el CLI trae todo eso).
CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep"
# Herramienta de Claude -> (nombre en agy, parámetro en agy, parámetro en Claude): así el
# progreso en vivo y el diagnóstico de denegaciones son los mismos para los dos motores.
_CLAUDE_AS_AGY = {
    "Bash": ("run_command", "CommandLine", "command"),
    "Read": ("view_file", "AbsolutePath", "file_path"),
    "Write": ("write_to_file", "TargetFile", "file_path"),
    "Edit": ("replace_file_content", "TargetFile", "file_path"),
    "MultiEdit": ("multi_replace_file_content", "TargetFile", "file_path"),
    "Glob": ("find_by_name", "Pattern", "pattern"),
    "Grep": ("grep_search", "Query", "pattern"),
}


def _claude_step(tool: str, arguments: dict) -> dict:
    name, param, source = _CLAUDE_AS_AGY.get(tool, (tool, "", ""))
    # Las herramientas sin equivalente en agy (las de los MCP) pasan sus argumentos tal cual.
    params = {param: arguments.get(source, "")} if param else dict(arguments)
    return {"step_type": "tool", "state": "ACTIVE", "tool_name": name,
            "tool_info": {"name": name, "parameters": params}}


def _claude_result(event: dict) -> dict:
    """El evento `result` de Claude Code, en el mismo formato que el de agy."""
    usage = event.get("usage") or {}
    prompt_tokens = sum(usage.get(k, 0) or 0 for k in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    output = usage.get("output_tokens", 0) or 0
    return {
        "conversation_id": event.get("session_id"),
        "status": "SUCCESS" if event.get("subtype") == "success" and not event.get("is_error") else "ERROR",
        "response": event.get("result") or "",
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": output,
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
            "total_tokens": prompt_tokens + output,
        },
        "denied_actions": [
            {"action": d.get("tool_name", "?"), "display_name": d.get("tool_name", "?")}
            for d in event.get("permission_denials") or []
        ],
        "cost_usd": event.get("total_cost_usd"),
    }


async def run_claude(
    *,
    prompt: str,
    model: str,
    effort: str | None,
    cwd: Path,
    session_id: str | None = None,
    timeout: int | None = None,
    denied_retries: int = 2,
    on_step: StepCallback | None = None,
    mcps: tuple[str, ...] = (),
    network_retries: int = 2,
    code: dict | None = None,
    extra_read: list[Path] | None = None,
) -> dict:
    """Corre un agente con el CLI de Claude Code, aislado de la configuración personal
    del usuario, y devuelve su resultado con el mismo formato que run_agy.

    `code` (los roles que programan, etapa 3): {"edit": [...], "read": [...],
    "writable": [...], "env": {...}}. Le da Bash, pero dentro del sandbox nativo de
    Claude Code: escribe solo en `writable`, el home está oculto (salvo los toolchains) y
    la red solo llega a los registros de paquetes."""
    servers = {name: config.MCP_SERVERS[name] for name in mcps}
    edit_dirs = [Path(p) for p in (code or {}).get("edit", [cwd])]
    read_dirs = [config.VAULT_PATH, *[Path(p) for p in (code or {}).get("read", [])], *(extra_read or [])]
    argv = [
        config.CLAUDE_BIN, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        # Sin aislar, el agente hereda los permisos, hooks, plugins y MCP del usuario: en
        # la primera prueba, un `ls` corrió aunque Bash no estaba permitido. --restricted
        # ignora todos los settings (también los que un agente plantara en la carpeta del
        # proyecto) y no deja escribir archivos de configuración.
        "--restricted", "--strict-mcp-config", "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--tools", CLAUDE_TOOLS + (",Bash" if code else ""),
        *(arg for d in {*read_dirs, *edit_dirs} - {cwd} for arg in ("--add-dir", str(d))),
        # --tools dice qué herramientas existen, no les da permiso: en dontAsk todo lo
        # no permitido se deniega, incluso escribir en la propia carpeta. Los permisos van
        # acotados: leer el vault (y los repos), escribir solo en la carpeta del proyecto
        # (o sus repos), y las herramientas de sus MCP de a una. ("//" = ruta absoluta.)
        "--allowedTools",
        *(f"Read(/{d.resolve()}/**)" for d in read_dirs),
        *(f"Edit(/{d.resolve()}/**)" for d in edit_dirs),
        *(f"mcp__{name}__{tool}" for name, server in servers.items() for tool in server["tools"]),
        # Bash completo, porque el límite es el sandbox (abajo), no una lista de comandos:
        # sin esta regla, en dontAsk se deniega todo lo que no es trivial (un heredoc, por
        # ejemplo), y con allowUnsandboxedCommands=false ningún comando sale de la jaula.
        *(["Bash"] if code else []),
    ]
    if code:
        # El sandbox nativo (bubblewrap): sin él, Bash correría con todo el usuario. Si no
        # puede arrancar, no corre nada (failIfUnavailable), y el agente no puede pedir
        # correr algo afuera (allowUnsandboxedCommands). Un TMPDIR propio lo rompe: usa
        # el de Claude (/tmp/claude-<uid>).
        argv += ["--settings", json.dumps({"sandbox": {
            "enabled": True, "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(p) for p in [*code["writable"], *code.get("read", []), *config.SANDBOX_TOOLCHAINS]],
                "allowWrite": [str(p) for p in code["writable"]],
            },
            "network": {"allowedDomains": list(config.SANDBOX_DOMAINS)},
        }})]
    if servers:
        argv += ["--mcp-config", json.dumps({"mcpServers": {n: s["config"] for n, s in servers.items()}})]
    else:
        # --safe-mode apaga plugins, skills, hooks y CLAUDE.md, pero también los MCP
        # elegidos con --mcp-config: solo se puede usar en los roles sin MCP.
        argv.append("--safe-mode")
    if effort:
        argv += ["--effort", effort]
    if session_id:
        argv += ["--resume", session_id]

    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={**os.environ, **(code or {}).get("env", {})},
        # El prompt va por stdin: los de los agentes son largos.
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    result_event: dict | None = None
    init: dict = {}
    lines: list[str] = []
    last_by_tool: dict[str, dict] = {}

    async def pump() -> None:
        nonlocal result_event
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result_event = event
            elif event.get("type") == "system" and event.get("subtype") == "init":
                # Lo que Claude cargó de verdad: la autoprueba verifica que siga aislado.
                init.update(tools=event.get("tools") or [], mcp_servers=event.get("mcp_servers") or [],
                            plugins=event.get("plugins") or [])
            elif event.get("type") == "assistant":
                for item in (event.get("message") or {}).get("content") or []:
                    if item.get("type") == "tool_use":
                        step = _claude_step(item.get("name", ""), item.get("input") or {})
                        last_by_tool[step["tool_name"]] = step["tool_info"]["parameters"]
                        _notify_step(on_step, step)

    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        await asyncio.wait_for(pump(), timeout=timeout or config.JOB_TIMEOUT)
        await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        stderr_task.cancel()
        raise RuntimeError(f"claude superó el timeout de {timeout or config.JOB_TIMEOUT}s y fue cancelado")
    stderr = (await stderr_task).decode("utf-8", "replace")
    if result_event is None:
        tail = "\n".join(lines)[-1500:] or stderr[-1500:]
        raise RuntimeError(f"claude no devolvió un resultado (código {process.returncode}): {tail}")

    data = _claude_result(result_event)
    data["init"] = init
    denied = data["denied_actions"]
    if denied and not data["response"].strip():
        detail = _denied_detail(denied, last_by_tool)
        if denied_retries > 0 and data["conversation_id"]:
            log.warning("claude denegó %s; reanudo la sesión con una indicación", detail)
            _notify_step(on_step, {"synthetic": "retry", "detail": detail})
            return await run_claude(
                prompt=DENIED_RETRY_PROMPT, model=model, effort=effort, cwd=cwd,
                session_id=data["conversation_id"], timeout=timeout,
                denied_retries=denied_retries - 1, on_step=on_step, mcps=mcps,
                network_retries=network_retries, code=code, extra_read=extra_read,
            )
        raise RuntimeError(f"claude denegó herramientas: {detail}")
    if (data["status"] != "SUCCESS" and _NETWORK_ERROR.search(data["response"])
            and network_retries > 0 and data["conversation_id"]):
        log.warning("claude perdió la conexión (%s); retomo la sesión en %d s",
                    data["response"][:120], NETWORK_RETRY_WAIT)
        _notify_step(on_step, {"synthetic": "network"})
        await asyncio.sleep(NETWORK_RETRY_WAIT)
        return await run_claude(
            prompt=NETWORK_RETRY_PROMPT, model=model, effort=effort, cwd=cwd,
            session_id=data["conversation_id"], timeout=timeout,
            denied_retries=denied_retries, on_step=on_step, mcps=mcps,
            network_retries=network_retries - 1, code=code, extra_read=extra_read,
        )
    if data["status"] != "SUCCESS" and not data["response"].strip():
        raise RuntimeError(f"claude terminó con error: {stderr[-500:] or result_event}")
    return data


def _tools_note(role: str) -> str:
    if config.ENGINES[role] == "claude":
        note = (
            "Tenés `Read`, `Write`, `Edit`, `Glob` y `Grep`: no hay shell. Con "
            "`Read` también podés mirar imágenes (por ejemplo, capturas de pantallas)."
        )
        if "context7" in config.MCPS[role]:
            note += (
                "\n\nTambién tenés **Context7** (`resolve-library-id` y después `query-docs`): "
                "la documentación actual de librerías y frameworks. Consultalo antes de afirmar "
                "versiones, APIs o configuraciones de una librería (por ejemplo, en un ADR), y "
                "citá la versión que consultaste. Lo que mandás ahí sale de la PC: nunca "
                "incluyas datos del proyecto, solo la pregunta técnica."
            )
        return note
    return (
        "Trabajá solo con tus herramientas nativas de archivos: `list_dir`, `view_file`,\n"
        "`grep_search`, `find_by_name`, `write_to_file` y `replace_file_content`. **No uses\n"
        "`run_command`**: corrés sin supervisión y cualquier comando shell fuera de una lista\n"
        "corta se deniega automáticamente, lo que corta tu trabajo a la mitad.\n\n"
        "Para la documentación actual de una librería o framework tenés el MCP **context7**\n"
        "(`resolve-library-id` y después `query-docs`). Lo que le mandás sale de la PC: solo\n"
        "la pregunta técnica, nunca datos del proyecto. Ningún otro MCP está permitido."
    )


def _code_note(project: str, task: str, repos: list[str], project_path: Path) -> str:
    trees = "\n".join(f"- Repo **{r}**: `{code.worktree(project, r)}`" for r in repos)
    return f"""# Tu código y tu terminal

Trabajás la tarea **{task}**. Tus repos ya están en la rama `{task}`, creada desde
`develop`:

{trees}

Tenés **Bash**, pero dentro de un sandbox:
- Escribís solo en tus repos y en la caché del proyecto. El resto de la PC es de solo
  lectura y tu home está oculto.
- La red solo llega a los registros de paquetes (npm, Maven, PyPI): `npm install`
  funciona; bajar cosas de otros sitios, no.
- **No hay Docker.** Los tests de integración con base de datos real los corre el
  sistema (y Jenkins); vos corré los unitarios y todo lo que no necesite contenedores.
- Nada global (`npm -g`, `sudo`, `pip install --user`): todo como dependencia del proyecto.

Git:
- Commiteá en la rama `{task}`, con mensajes que empiecen con `{task}:` y digan qué
  hiciste. No cambies de rama ni toques `.git/config`.
- El repo no tiene remoto: **el sistema sube la rama y abre el pull request** cuando
  terminás. Antes de terminar, corré los tests y dejá todo commiteado.

Los documentos del proyecto están en el vault, en `{project_path}`: leelos. Escribí ahí
solo lo que tu rol pida (por ejemplo, la validación de QA en `Desarrollo/{task}.md`).
"""


def build_subagent_prompt(
    role: str, project: str, instruction: str, project_path: Path,
    task: str | None = None, repos: list[str] | None = None,
) -> str:
    """Prompt autocontenido: el sub-agente no ve la charla de Telegram."""
    where = (
        f"- **Código:** en tus repos (ver «Tu código y tu terminal»); tu directorio de trabajo ya es {'tu repo' if len(repos or []) == 1 else 'la carpeta con los repos'}.\n"
        f"- **Carpeta del proyecto en el vault:** `{project_path}`"
        if task else
        f"- **Carpeta del proyecto (escribí acá):** `{project_path}`\n"
        "- Tu directorio de trabajo actual ya es la carpeta del proyecto."
    )
    return f"""{config.prompt_for(role)}

---

# Contexto de esta invocación

- **Vault de Obsidian:** `{config.VAULT_PATH}`
- **Proyecto:** {project}
{where}

Antes de escribir, listá la carpeta y leé los documentos que ya existan.

# Documentos del proyecto

| Documento | Lo escribe |
|-----------|------------|
| 00 - Índice & Visión General del Proyecto | Producto |
| 01 - Requerimientos Funcionales y Reglas de Negocio | Producto |
| 02 - Arquitectura del Sistema & Stack Tecnológico | Líder técnico |
| 03 - Modelo de Datos & Liquibase Changelogs | DBA |
| 04 - Especificación API REST & Contratos de Integración | Líder técnico |
| 05 - Frontend & Experiencia de Usuario (UI-UX) | UX y UI |
| Diseño/DESIGN.md y Diseño/Pantallas.md | UI |
| Diseño/Pantallas/ y Diseño/stitch.json | El sistema (Stitch) |
| Diseño/Imágenes.md | Imágenes |
| Diseño/Imágenes/ | El sistema (guarda lo que genera Imágenes) |
| 06 - DevOps, Docker & CI-CD | DevOps |
| 08 - Estimaciones | Team leader |
| 09 - Propuesta y Contrato | Legal |
| 10 - Plan de Trabajo | Team leader |
| 11 - Plan de Pruebas | QA |
| 12 - Revisión de Seguridad | Seguridad |
| 13 - Revisión UX-UI | Revisor de UX y UI |
| ADRs/ADR-NNN - Título.md | Líder técnico |
| Mesa Técnica/ | Los participantes de la mesa |

Escribí solo los documentos de tu rol y leé los demás como contexto. Si tu documento ya
existe, actualizalo en vez de duplicarlo. **Nunca edites «Estado del Proyecto.md»** (lo
mantiene el sistema) **ni un ADR aprobado** (si una decisión cambia, va un ADR nuevo).

# Herramientas

{_tools_note(role)}

{_code_note(project, task, repos or [], project_path) if task else ""}
# Tarea

{instruction}

# Formato de tu respuesta

Terminá con un resumen de máximo 8 líneas: qué archivos escribiste o modificaste, las
decisiones importantes que tomaste, y el siguiente paso recomendado (qué sub-agente y
para qué). Es lo único que va a leer el Director de Proyecto.
"""


# Roles que, sin programar, leen el código del proyecto (revisión, seguridad, datos).
REVIEW_ROLES = ("lider_tecnico", "seguridad", "dba", "qa", "devops")
IMAGES_DIR = "Diseño/Imágenes"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _collect_images(conversation_id: str | None, since: float, project_path: Path) -> list[Path]:
    """Copia al proyecto las imágenes que generó agy en esta corrida.

    agy las deja en la carpeta de la conversación (brain/<id>/nombre_<marca>.jpg) y el
    agente no puede copiarlas (no tiene `cp`): las copia el gateway."""
    source = config.AGY_BRAIN_DIR / conversation_id if conversation_id else None
    if not source or not source.is_dir():
        return []
    dest = project_path / IMAGES_DIR
    copied: list[Path] = []
    candidates = [
        p for p in source.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES and p.stat().st_mtime >= since
    ]
    for image in sorted(candidates, key=lambda p: p.stat().st_mtime):
        # "logo_concepto_1_1791152163067.jpg" -> "logo-concepto-1.jpg"
        stem = re.sub(r"_\d{10,}$", "", image.stem).replace("_", "-") or "imagen"
        target = dest / f"{stem}{image.suffix.lower()}"
        n = 2
        while target in copied:  # el mismo nombre dos veces en una corrida: no pisar
            target = dest / f"{stem}-{n}{image.suffix.lower()}"
            n += 1
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image, target)
        copied.append(target)
    return copied


def _code_run(role: str, project: str, repos: list[str], project_path: Path) -> tuple[dict, Path]:
    """Permisos y entorno de un rol que programa: sus repos, la caché del proyecto (npm,
    Maven, pip… sin tocar las del usuario) y el vault para leer y anotar."""
    trees = [code.worktree(project, r) for r in repos]
    cache = code.cache_dir(project)
    # Los commits dicen qué agente los hizo (si no, git toma la identidad del usuario).
    name = f"{role.capitalize()} (agente)"
    identity = {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name,
                "GIT_AUTHOR_EMAIL": f"{role}@agentes.orchestrator", "GIT_COMMITTER_EMAIL": f"{role}@agentes.orchestrator"}
    run = {
        "edit": [*trees, project_path],
        "read": [code.code_dir(project)],
        "writable": [*trees, cache],
        "env": {
            "npm_config_cache": str(cache / "npm"), "npm_config_update_notifier": "false",
            "XDG_CACHE_HOME": str(cache / "xdg"), "PIP_CACHE_DIR": str(cache / "pip"),
            "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}", "GRADLE_USER_HOME": str(cache / "gradle"),
            "PLAYWRIGHT_BROWSERS_PATH": str(cache / "ms-playwright"),
            **identity,
        },
    }
    return run, trees[0] if len(trees) == 1 else code.code_dir(project)


def _publish(project: str, role: str, task: str, repos: list[str], instruction: str,
             response: str, project_path: Path) -> list[dict]:
    """Sube la rama de cada repo con cambios, abre su PR y deja el diff en el vault para
    el Líder técnico (Desarrollo/<tarea> - <repo>.diff)."""
    published = []
    for repo in repos:
        result = code.finish_task(project, repo, task)
        if result["commits"]:
            body = (f"Tarea {task}, hecha por el rol {role}.\n\n**Instrucción:**\n{instruction[:3000]}\n\n"
                    f"**Resumen del agente:**\n{response[:3000]}\n\n🤖 Generado por el equipo de agentes del Orchestrator")
            result["pr"] = code.open_pr(project, repo, task, f"{task}: {instruction.splitlines()[0][:80]}", body)
            folder = project_path / "Desarrollo"
            folder.mkdir(exist_ok=True)
            (folder / f"{task} - {repo}.diff").write_text(code.diff(project, repo, task), encoding="utf-8")
            _notify_jenkins(project, repo)
        published.append(result)
    return published


def _notify_jenkins(project: str, repo: str) -> None:
    """Después de un push: Jenkins no puede enterarse solo (no hay webhook), le avisa el gateway."""
    if not jenkins.configured():
        return
    try:
        jenkins.scan(project, repo)
    except Exception as exc:  # noqa: BLE001 — el push ya está hecho; Jenkins igual revisa cada 15 min
        log.warning("no pude avisarle a Jenkins del push de %s/%s: %s", project, repo, exc)


async def invoke_subagent(
    role: str, project: str, instruction: str, on_step: StepCallback | None = None,
    task: str | None = None,
) -> dict:
    """Corre un sub-agente y verifica con git lo que realmente cambió. Con `task`, el rol
    programa en los repos del proyecto (etapa 3) y el sistema publica lo que hizo."""
    if role not in config.ROLES:
        raise ValueError(f"rol desconocido: {role}. Válidos: {', '.join(config.ROLES)}")
    repos: list[str] = []
    if task:
        if role not in config.CODE_ROLES or config.ENGINES[role] != "claude":
            raise ValueError(f"«{role}» no programa en los repos: --tarea es para {', '.join(config.CODE_ROLES)}.")
        task = code.valid_task(task)
        repos = [r for r in config.CODE_REPOS[role] if code.worktree(project, r).exists()]
        if not repos:
            raise ValueError(f"«{project}» todavía no tiene repos: primero hay que pedírselos al usuario y registrarlos con orc-repos.")

    project_path = await asyncio.to_thread(vault.project_dir, project)
    published: list[dict] = []
    with project_lock(project):
        before = await asyncio.to_thread(vault.snapshot)
        started = time.time()
        code_run, cwd = None, project_path
        if task:
            for repo in repos:
                await asyncio.to_thread(code.prepare_task, project, repo, task)
            code_run, cwd = _code_run(role, project, repos, project_path)
        prompt = build_subagent_prompt(role, project, instruction, project_path, task, repos)
        # Los que revisan (Líder técnico, Seguridad…) leen el código aunque no lo toquen.
        extra_read = ([code.code_dir(project)] if not task and role in REVIEW_ROLES
                      and code.code_dir(project).exists() else None)
        if config.ENGINES[role] == "claude":
            result = await run_claude(
                prompt=prompt, model=config.MODELS[role], effort=config.EFFORTS[role],
                cwd=cwd, on_step=on_step, mcps=config.MCPS[role], code=code_run, extra_read=extra_read,
            )
        else:
            result = await run_agy(
                prompt=prompt, model=config.MODELS[role], cwd=project_path,
                caller=role, on_step=on_step,
            )
        if task:
            published = await asyncio.to_thread(
                _publish, project, role, task, repos, instruction, result.get("response", ""), project_path
            )
        images: list[Path] = []
        if role == "imagenes":
            images = await asyncio.to_thread(
                _collect_images, result.get("conversation_id"), started - 1, project_path
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
        "images": [vault.relative(p) for p in images],
        "codigo": published,
        # Señal de alerta: dijo que escribió pero git no vio nada.
        "suspect_no_writes": claimed and not files_changed,
    }
