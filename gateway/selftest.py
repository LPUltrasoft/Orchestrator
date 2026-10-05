"""Autoprueba de los dos motores: que agy y Claude respondan, y que Claude siga aislado.

agy y Claude Code se actualizan solos, y las sesiones vencen: antes, un problema así se
descubría recién al escribirle al bot. La autoprueba corre al arrancar el gateway y una
vez por día, con el modelo más barato de cada motor, y el gateway avisa por Telegram si
algo falla, con cómo arreglarlo.

En Claude, además de que responda, se verifica lo que cargó (el evento `init`): que no
tenga Bash, ni MCP, ni plugins del usuario. Si una versión nueva cambia cómo funcionan
`--restricted` o `--safe-mode`, se ve acá y no en un agente con más permisos de la cuenta.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, runner

log = logging.getLogger("orchestrator.selftest")

STATE_FILE = config.STATE_DIR / "autoprueba.json"
WORKDIR = config.STATE_DIR / "autoprueba"
PROMPT = "Respondé solo con la palabra OK."


async def _version(binary: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        return out.decode("utf-8", "replace").strip().splitlines()[0] if out else "?"
    except (OSError, asyncio.TimeoutError, IndexError):
        return "?"


async def versions() -> dict[str, str]:
    agy, claude = await asyncio.gather(_version(config.AGY_BIN), _version(config.CLAUDE_BIN))
    return {"agy": agy, "claude": claude}


def _fresh_workdir() -> Path:
    shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True)
    return WORKDIR


async def check_agy() -> str | None:
    """None si anda; si no, qué pasa y cómo se arregla."""
    try:
        result = await runner.run_agy(
            prompt=PROMPT, model=config.SELFTEST_AGY_MODEL, cwd=_fresh_workdir(),
            caller="autoprueba", timeout=180, denied_retries=0,
        )
    except RuntimeError as exc:
        text = str(exc)
        if "expiró" in text:
            return "la sesión de agy venció. Abrí una terminal, corré `agy` y completá el login de Google."
        if _quota_limited(text):
            log.info("autoprueba de agy sin cuota: no es una falla (%s)", text[:120])
            return None
        return f"agy falló: {text[:300]}"
    if result.get("status") != "SUCCESS" or "OK" not in (result.get("response") or "").upper():
        return f"agy respondió algo inesperado ({result.get('status')}): {(result.get('response') or '')[:200]}"
    return None


async def check_claude() -> str | None:
    """None si anda y sigue aislado; si no, qué pasa y cómo se arregla."""
    try:
        result = await runner.run_claude(
            prompt=PROMPT, model=config.SELFTEST_CLAUDE_MODEL, effort=None, cwd=_fresh_workdir(),
            timeout=180, denied_retries=0, network_retries=0,
        )
    except RuntimeError as exc:
        return _claude_failure(str(exc))
    if result.get("status") != "SUCCESS" or "OK" not in (result.get("response") or "").upper():
        return _claude_failure(f"respondió algo inesperado ({result.get('status')}): {result.get('response') or ''}")

    init = result.get("init") or {}
    tools = set(init.get("tools") or [])
    allowed = set(runner.CLAUDE_TOOLS.split(","))
    problems = []
    if not tools:
        problems.append("no se pudo ver qué herramientas cargó")
    if tools - allowed:
        problems.append(f"tiene herramientas de más: {', '.join(sorted(tools - allowed))}")
    if init.get("mcp_servers"):
        names = ", ".join(str(m.get("name")) for m in init["mcp_servers"])
        problems.append(f"cargó MCP que no debería: {names}")
    own_plugins = [p.get("name") for p in init.get("plugins") or [] if p.get("path") != "builtin"]
    if own_plugins:
        problems.append(f"cargó plugins del usuario: {', '.join(map(str, own_plugins))}")
    if problems:
        return ("Claude YA NO ESTÁ AISLADO (probablemente por una actualización): "
                + "; ".join(problems) + ". Revisá los flags de `runner.run_claude` antes de usar los agentes.")
    return None


def _quota_limited(text: str) -> bool:
    """Sin cuota no es una caída: el gateway ya pausa y retoma solo."""
    low = text.lower()
    return "limit" in low and any(w in low for w in ("usage", "quota", "rate", "reached"))


def _claude_failure(text: str) -> str | None:
    if _quota_limited(text):
        log.info("autoprueba de Claude sin cuota: no es una falla (%s)", text[:120])
        return None
    # Sin sesión, Claude Code termina con "Not logged in · Please run /login" (o
    # "Invalid API key"): eso no es una falla del sistema, es un login pendiente.
    if any(m in text for m in ("/login", "Not logged in", "Invalid API key")):
        return "la sesión de Claude Code venció. Abrí una terminal, corré `claude` y hacé `/login`."
    return f"Claude falló: {text[:300]}"


async def run() -> dict:
    """Corre las dos pruebas y guarda el resultado."""
    agy, claude = await asyncio.gather(check_agy(), check_claude())
    result = {
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agy": agy,
        "claude": claude,
        "ok": agy is None and claude is None,
        "versiones": await versions(),
    }
    STATE_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    shutil.rmtree(WORKDIR, ignore_errors=True)
    log.info("autoprueba: %s", "todo bien" if result["ok"] else f"agy={agy} claude={claude}")
    return result


def last() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
