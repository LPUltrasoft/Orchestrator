"""Verificación de efectos reales en el vault de Obsidian.

El agente puede decir que escribió un archivo sin haberlo hecho (pasa cuando se
lo invoca sin --mode accept-edits, pero también cuando alucina). Nunca le
creemos al texto: comparamos el estado de git antes y después.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from . import config

# Un solo git a la vez: el gateway commitea el estado de un proyecto mientras un agente
# trabaja en otro, y hasta `git status` escribe el índice (choca con index.lock).
_git_lock = threading.RLock()


def _git(*args: str, cwd: Path | None = None) -> str:
    result = _run_git(*args, cwd=cwd)
    if result.returncode != 0:
        # git avisa algunas cosas por stdout ("nothing to commit"): van las dos salidas.
        detail = (result.stderr.strip() + " " + result.stdout.strip()).strip()
        raise RuntimeError(f"git {' '.join(args)} falló: {detail}")
    return result.stdout


def _run_git(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        # quotePath=false: sin esto git escapa los acentos como \303\215.
        # hooksPath y fsmonitor anulados: un agente con permiso de `git config`
        # podría plantar un comando ahí y el gateway lo ejecutaría en su próximo
        # status o commit.
        ["git", "-c", "core.quotePath=false", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", *args],
        cwd=str(cwd or config.VAULT_PATH),
        capture_output=True,
        text=True,
        timeout=timeout,
        # Nunca esperar que alguien escriba una contraseña: si faltan credenciales, falla.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def unpushed() -> int | None:
    """Commits que todavía no están en GitHub, o None si el vault no tiene rama remota."""
    with _git_lock:
        result = _run_git("rev-list", "--count", "@{upstream}..HEAD")
    if result.returncode != 0:
        return None  # sin rama remota, por ejemplo en un clon de prueba
    return int(result.stdout.strip() or 0)


def push() -> None:
    """Sube los commits a la rama remota. Lanza RuntimeError si no puede."""
    # Sin el lock: la red puede tardar y no tiene que frenar los commits; push solo lee refs.
    result = _run_git("push", "--quiet", timeout=120)
    if result.returncode != 0:
        raise RuntimeError((result.stderr.strip() or result.stdout.strip() or "git push falló")[-300:])


def snapshot() -> dict[str, str]:
    """Mapa ruta -> estado de cada archivo sucio, más el HEAD actual."""
    with _git_lock:
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
    with _git_lock:
        return _changed_since(before)


def _changed_since(before: dict) -> list[str]:
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


def commit(role: str, project: str, files: list[str], complete: bool = True) -> str | None:
    """Commitea los cambios del turno. Devuelve el hash corto, o None."""
    prefix = "" if complete else "[INCOMPLETO] "
    message = f"{prefix}{role}: actualiza {project}\n\nArchivos:\n" + "\n".join(
        f"- {f}" for f in files[:20]
    )
    return commit_paths(files, message)


def commit_paths(paths: list[str], message: str) -> str | None:
    """Commitea solo esos archivos (rutas relativas al vault). Devuelve el hash corto.

    Nunca `git add -A`: sumaría cambios a medio hacer de otro proyecto.
    """
    if not config.VAULT_AUTOCOMMIT or not paths:
        return None
    with _git_lock:
        existing = [p for p in paths if (config.VAULT_PATH / p).exists()]
        gone = [p for p in paths if p not in existing]
        if existing:
            # -A dentro de las rutas: incluye cambios y archivos nuevos.
            _git("add", "-A", "--", *existing)
        if gone:
            # Borrados o movidos. --ignore-unmatch: puede que nunca hayan estado en git.
            _git("rm", "--cached", "--quiet", "--ignore-unmatch", "--", *gone)
        # --no-renames: un archivo movido aparecería solo con su nombre nuevo, y el
        # borrado del viejo quedaría sin commitear.
        staged = [
            p for p in _git("diff", "--cached", "--name-only", "--no-renames", "--", *paths).splitlines()
            if p
        ]
        if not staged:
            return None  # nada que commitear no es un error
        _git("commit", "-m", message, "--", *staged)
        return _git("rev-parse", "--short", "HEAD").strip()


def project_path(project: str) -> Path:
    """Carpeta del proyecto dentro del vault, sin crearla.

    El nombre se sanea para que no pueda escapar del vault.
    """
    safe = project.strip().replace("/", "-").replace("\\", "-").strip(". ")
    if not safe:
        raise ValueError("nombre de proyecto vacío")
    target = (config.projects_root() / safe).resolve()
    if not str(target).startswith(str(config.projects_root().resolve())):
        raise ValueError(f"ruta de proyecto inválida: {project}")
    return target


def project_dir(project: str) -> Path:
    """Carpeta del proyecto dentro del vault, creada si no existe."""
    target = project_path(project)
    target.mkdir(parents=True, exist_ok=True)
    return target


def relative(path: Path) -> str:
    """Ruta relativa al vault, como la usa git."""
    return str(path.resolve().relative_to(config.VAULT_PATH.resolve()))


def list_projects() -> list[str]:
    root = config.projects_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
