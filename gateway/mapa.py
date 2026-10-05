"""Mapa de puertos y repos de todos los proyectos, en el vault (pedido del usuario del 5/10/2026).

Lo arma el sistema solo con lo que sabe: los repos y los puertos asignados de cada proyecto
(su estado), lo que de verdad está publicado en cada puerto (el Docker de orc-ci), la
plataforma compartida y los servicios del sistema. Se regenera al registrar repos, al dar de
alta un proyecto en la autenticación, después de cada build de develop, al arrancar y cada
media hora; solo se commitea si cambió algo más que la hora.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from . import code, config, process, vault

log = logging.getLogger("orchestrator.mapa")

DOC = "07 - Mapa de Puertos y Repos"
HOME_PROJECT = "Orchestrator - Sistema Multi-Agente"
_STAMP = re.compile(r"^\*Actualizado: .*\*$", re.M)
_PORT = re.compile(r"^(?P<ip>[\d.]+|\[::\]):(?P<host>\d+)->(?P<container>\d+)/tcp$")

# El último lugar de cada rango es de la plataforma compartida (config.PORTS_MAX_PROJECTS).
PLATFORM = [
    {"servicio": "Autenticación compartida", "repo": "LucianoPal/Autenticacion",
     "ramas": "develop (Jenkins `plataforma/autenticacion` despliega desarrollo) y master; main es la de Synergia y Grandes Pasos",
     "desarrollo": "18490 API · 18491 base", "produccion": "19490–19499 (etapa 3b)"},
]
SYSTEM = [
    ("Gateway del Orchestrator", "127.0.0.1:8787", "Solo la PC"),
    ("Jenkins", "127.0.0.1:8090 (HTTP) · 8443 (HTTPS)", "8443, desde la red local según scripts/jenkins-red-local.sh"),
]


def path() -> Path:
    return vault.project_path(HOME_PROJECT) / f"{DOC}.md"


def containers() -> list[dict] | None:
    """Contenedores del Docker de orc-ci con sus puertos publicados. None si no responde."""
    try:
        result = subprocess.run(["sudo", "-n", "-u", "orc-ci", "orc-ci-docker", "ps", "--format", "{{json .}}"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("no pude listar los contenedores de orc-ci: %s", exc)
        return None
    if result.returncode != 0:
        log.warning("no pude listar los contenedores de orc-ci: %s", result.stderr.strip()[:200])
        return None
    found = []
    for line in result.stdout.splitlines():
        data = json.loads(line)
        ports = [m.groupdict() for p in data.get("Ports", "").split(", ") if (m := _PORT.match(p.strip()))]
        # El estado sin el tiempo («Up 2 minutes»): si no, el mapa cambiaría en cada vuelta.
        health = re.search(r"\((healthy|unhealthy|health: starting)\)", data.get("Status", ""))
        state = data.get("State", "")
        if state == "running":
            state = {"healthy": "✅ sano", "unhealthy": "⚠️ con problemas"}.get(health.group(1) if health else "", "corriendo")
        found.append({"nombre": data["Names"], "estado": state, "puertos": ports})
    return found


def _owner(port: int, projects: list[tuple[str, dict]]) -> tuple[str, str] | None:
    """De qué proyecto y ambiente es un puerto, por los rangos asignados."""
    for name, state in projects:
        ports = state.get("puertos") or {}
        for env, key in (("desarrollo", "desarrollo"), ("producción", "produccion")):
            low, high = ports.get(key) or (0, -1)
            if low <= port <= high:
                return name, env
    return None


def _platform_env(port: int) -> str | None:
    """«desarrollo» o «producción» si el puerto es del lugar de la plataforma."""
    for env, base in (("desarrollo", config.PORTS_DEV_BASE), ("producción", config.PORTS_PROD_BASE)):
        first = base + config.PORTS_PER_ENV * config.PORTS_MAX_PROJECTS
        if first <= port < first + config.PORTS_PER_ENV:
            return env
    return None


def _published(project: str | None, projects: list[tuple[str, dict]], running: list[dict]) -> list[str]:
    """Filas de los puertos publicados de un proyecto, o de la plataforma si `project` es None."""
    rows = {}
    for c in running:
        for p in c["puertos"]:
            port = int(p["host"])
            if project is None:
                env = _platform_env(port)
            else:
                owner = _owner(port, projects)
                env = owner[1] if owner and owner[0] == project else None
            if env:
                where = "toda la red (el firewall filtra)" if p["ip"] in ("0.0.0.0", "[::]") else f"solo {p['ip']}"
                rows[port] = f"| {port} | {env} | `{c['nombre']}` (puerto {p['container']}) | {where} | {c['estado']} |"
    return [rows[k] for k in sorted(rows)]


def render(projects: list[tuple[str, dict]], running: list[dict] | None, now: datetime) -> str:
    lines = [
        f"# 🗺️ {DOC}",
        "",
        "> En qué puertos y desde qué repos se levanta cada proyecto. **Lo genera el sistema solo:**",
        "> no lo edites a mano (se pisa). Detalle de cada puerto en el `06` de cada proyecto.",
        "",
        f"*Actualizado: {now:%d/%m/%Y %H:%M}*",
        "",
        "---",
        "",
        "## 📦 Proyectos",
        "",
        "| Proyecto | Fase | Repos | Desarrollo | Producción |",
        "|----------|------|-------|------------|------------|",
    ]
    with_code = [(n, s) for n, s in projects if s.get("repos") or s.get("puertos")]
    for name, state in with_code:
        repos = " · ".join(f"{r}: [{i['github']}](https://github.com/{i['github']})"
                           for r, i in (state.get("repos") or {}).items()) or "—"
        ports = state.get("puertos") or {}
        dev = "–".join(map(str, ports["desarrollo"])) if ports.get("desarrollo") else "—"
        prod = "–".join(map(str, ports["produccion"])) if ports.get("produccion") else "—"
        try:
            phase = process.phase(state["fase"]).label
        except (KeyError, ValueError, StopIteration, TypeError):
            phase = str(state.get("fase", "—"))
        lines.append(f"| [[{name}]] | {phase} | {repos} | {dev} | {prod} |")
    if not with_code:
        lines.append("| — | — | Ningún proyecto tiene repos todavía | — | — |")
    without = [n for n, s in projects if not (s.get("repos") or s.get("puertos"))]
    if without:
        lines += ["", f"Sin repos ni puertos todavía (fases 1 a 3): {', '.join(f'[[{n}]]' for n in without)}."]

    for name, state in with_code:
        lines += ["", f"### {name}", ""]
        auth = (state.get("autenticacion") or {}).get("desarrollo")
        if auth:
            lines.append(f"- Autenticación: sociedad **{auth['idsociedad']}** en desarrollo; roles "
                         + ", ".join(f"{r} ({i})" for r, i in auth.get("roles", {}).items()) + ".")
        for repo, info in (state.get("repos") or {}).items():
            lines.append(f"- Repo **{repo}**: `{info['github']}`, clon de trabajo en `{info.get('local', '—')}`; "
                         f"Jenkins `{code.slug(name)}/{repo}` (develop y master).")
        if running is None:
            lines.append("- Puertos en uso: no pude consultar Docker en esta actualización.")
            continue
        rows = _published(name, projects, running)
        if rows:
            lines += ["", "| Puerto | Ambiente | Contenedor | Escucha en | Estado |",
                      "|--------|----------|------------|------------|--------|", *rows]
        else:
            lines.append("- Puertos en uso: ninguno publicado todavía.")

    lines += ["", "---", "", "## 🔐 Plataforma compartida", "",
              "| Servicio | Repo | Ramas | Desarrollo | Producción |",
              "|----------|------|-------|------------|------------|"]
    lines += [f"| {p['servicio']} | [{p['repo']}](https://github.com/{p['repo']}) | {p['ramas']} | "
              f"{p['desarrollo']} | {p['produccion']} |" for p in PLATFORM]
    if running is not None and (rows := _published(None, projects, running)):
        lines += ["", "| Puerto | Ambiente | Contenedor | Escucha en | Estado |",
                  "|--------|----------|------------|------------|--------|", *rows]
    lines += ["", "## ⚙️ Servicios del sistema", "", "| Servicio | Puertos | Acceso |", "|---|---|---|"]
    lines += [f"| {s} | {p} | {a} |" for s, p, a in SYSTEM]
    max_dev = config.PORTS_DEV_BASE + config.PORTS_PER_ENV * config.PORTS_MAX_PROJECTS - 1
    max_prod = config.PORTS_PROD_BASE + config.PORTS_PER_ENV * config.PORTS_MAX_PROJECTS - 1
    lines += [
        "", "## 📐 Rangos", "",
        f"- **Desarrollo**: {config.PORTS_DEV_BASE}–{max_dev}, {config.PORTS_PER_ENV} por proyecto; "
        f"{max_dev + 1}–{max_dev + config.PORTS_PER_ENV}, la plataforma. Se abre a la red local con "
        "`scripts/desarrollo-red-local.sh` (solo las IPs que se le pasan).",
        f"- **Producción**: {config.PORTS_PROD_BASE}–{max_prod}, en el mismo lugar que su desarrollo; "
        f"{max_prod + 1}–{max_prod + config.PORTS_PER_ENV}, la plataforma. Solo para el nginx del usuario (etapa 3b).",
        "",
    ]
    return "\n".join(lines)


def update() -> bool:
    """Regenera el mapa y lo commitea si cambió algo más que la hora. Devuelve si cambió."""
    text = render(process.projects_with_state(), containers(), datetime.now().astimezone())
    target = path()
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    if _STAMP.sub("", old) == _STAMP.sub("", text):
        return False
    target.write_text(text, encoding="utf-8")
    vault.commit_paths([vault.relative(target)], "mapa de puertos y repos")
    return True
