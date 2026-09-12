"""Verificación de efectos reales en el vault de Obsidian.

El agente puede decir que escribió un archivo sin haberlo hecho (pasa cuando se
lo invoca sin --mode accept-edits, pero también cuando alucina). Nunca le
creemos al texto: comparamos el estado de git antes y después.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        # quotePath=false: sin esto git escapa los acentos como \303\215.
        ["git", "-c", "core.quotePath=false", *args],
        cwd=str(cwd or config.VAULT_PATH),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falló: {result.stderr.strip()}")
    return result.stdout


def snapshot() -> dict[str, str]:
    """Mapa ruta -> estado de cada archivo sucio, más el HEAD actual."""
    entries = {}
    for line in _git("status", "--porcelain", "-uall").splitlines():
        if len(line) > 3:
            entries[line[3:].strip().strip('"')] = line[:2]
    head = _git("rev-parse", "HEAD").strip() if has_commits() else ""
    return {"dirty": entries, "head": head}


def has_commits() -> bool:
    try:
        _git("rev-parse", "HEAD")
        return True
    except RuntimeError:
        return False


def changed_since(before: dict) -> list[str]:
    """Archivos que realmente cambiaron entre dos snapshots."""
    after = snapshot()
    changed = set()
    for path, state in after["dirty"].items():
        if before["dirty"].get(path) != state:
            changed.add(path)
    # Si hubo commits nuevos (autocommit de otra corrida), contamos su diff.
    if before["head"] and after["head"] and before["head"] != after["head"]:
        diff = _git("diff", "--name-only", f"{before['head']}..{after['head']}")
        changed.update(p.strip() for p in diff.splitlines() if p.strip())
    return sorted(changed)


def commit(role: str, project: str, files: list[str]) -> str | None:
    """Commitea los cambios del turno. Devuelve el hash corto, o None."""
    if not config.VAULT_AUTOCOMMIT or not files:
        return None
    _git("add", "-A")
    message = f"{role}: actualiza {project}\n\nArchivos:\n" + "\n".join(
        f"- {f}" for f in files[:20]
    )
    try:
        _git("commit", "-m", message)
    except RuntimeError as exc:
        if "nothing to commit" in str(exc) or "nada para hacer commit" in str(exc):
            return None
        raise
    return _git("rev-parse", "--short", "HEAD").strip()


def project_dir(project: str) -> Path:
    """Carpeta del proyecto dentro del vault, creada si no existe.

    El nombre se sanea para que no pueda escapar del vault.
    """
    safe = project.strip().replace("/", "-").replace("\\", "-").strip(". ")
    if not safe:
        raise ValueError("nombre de proyecto vacío")
    target = (config.projects_root() / safe).resolve()
    if not str(target).startswith(str(config.projects_root().resolve())):
        raise ValueError(f"ruta de proyecto inválida: {project}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def list_projects() -> list[str]:
    root = config.projects_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
