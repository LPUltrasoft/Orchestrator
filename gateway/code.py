"""Repos de código de cada proyecto (etapa 3): clones de trabajo, espejos y publicación.

Por proyecto hay dos repos (front y back) que crea el usuario en GitHub. El gateway los
clona dos veces:

- **El espejo** (`MIRRORS_DIR/<proyecto>/<repo>.git`, bare): tiene el remoto de GitHub y
  es el único que usa las credenciales del usuario (push, PRs). Ningún agente lo toca.
- **El clon de trabajo** (`~/Proyectos/<proyecto>/<repo>`): donde programan los agentes,
  dentro del sandbox. No tiene remoto: un agente no puede subir nada.

Un agente puede plantar configuración en `.git/` de su clon (filtros, hooks) que
ejecutaría comandos cuando alguien corra git ahí. Por eso todo git que el gateway corre
sobre un clon de trabajo va dentro de bubblewrap: sin red, con el home oculto y solo ese
repo escribible. Lo que necesita credenciales pasa por el espejo.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import unicodedata
from pathlib import Path

from . import config

log = logging.getLogger("orchestrator.code")

REPOS = ("front", "back")
_GITHUB_URL = re.compile(r"^(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_TASK = re.compile(r"^(T-\d{3,}|esqueleto)$")
_SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false")


class CodeError(RuntimeError):
    pass


def slug(project: str) -> str:
    """'Turnos Médicos' -> 'turnos-medicos'."""
    plain = unicodedata.normalize("NFKD", project).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "proyecto"


def code_dir(project: str) -> Path:
    return config.PROJECTS_CODE_DIR / slug(project)


def worktree(project: str, repo: str) -> Path:
    return code_dir(project) / repo


def mirror(project: str, repo: str) -> Path:
    return config.MIRRORS_DIR / slug(project) / f"{repo}.git"


def cache_dir(project: str) -> Path:
    """Caché de npm, Maven, pip… del proyecto: escribible desde el sandbox."""
    return code_dir(project) / ".cache"


def valid_task(task: str) -> str:
    task = task.strip()
    if not _TASK.match(task):
        raise CodeError(f"tarea inválida: «{task}». Tiene que ser T-NNN (por ejemplo T-003) o «esqueleto».")
    return task


def parse_github(url: str) -> str:
    """'https://github.com/LPUltrasoft/turnos-back' -> 'LPUltrasoft/turnos-back'."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise CodeError(f"no es un link de un repo de GitHub: {url}")
    return f"{match.group(1)}/{match.group(2)}"


# ── git ────────────────────────────────────────────────────────────────────────


def _run(args: list[str], cwd: Path | None = None, timeout: int = 180) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                            timeout=timeout, env=env)
    if result.returncode != 0:
        detail = (result.stderr.strip() + " " + result.stdout.strip()).strip()
        raise CodeError(f"{' '.join(args[:4])}… falló: {detail[-400:]}")
    return result.stdout


def mirror_git(project: str, repo: str, *args: str, timeout: int = 180) -> str:
    """git en el espejo: código propio, con las credenciales del usuario (gh)."""
    return _run(["git", *_SAFE_GIT, *args], cwd=mirror(project, repo), timeout=timeout)


def _jail(writable: Path | None, readonly: list[Path] = ()) -> list[str]:
    """bubblewrap: raíz de solo lectura, home oculto, sin red, y como mucho un directorio
    escribible."""
    args = ["bwrap", "--ro-bind", "/", "/", "--tmpfs", str(Path.home()),
            "--dev", "/dev", "--proc", "/proc", "--unshare-net", "--die-with-parent"]
    for path in readonly:
        args += ["--ro-bind", str(path), str(path)]
    if writable:
        args += ["--bind", str(writable), str(writable)]
    return args


def work_git(project: str, repo: str, *args: str, timeout: int = 180) -> str:
    """git en un clon de trabajo, dentro de la jaula: lo que un agente haya plantado en
    `.git/` (filtros, hooks, fsmonitor) no puede salir de ahí."""
    tree = worktree(project, repo)
    identity = ("-c", f"user.name=Sistema Orchestrator", "-c", "user.email=sistema@orchestrator.local")
    return _run([*_jail(tree, [mirror(project, repo)]), "git", *_SAFE_GIT, *identity, "-C", str(tree), *args],
                timeout=timeout)


def _upload_pack(tree: Path) -> str:
    """Para leer un clon de trabajo desde el espejo: el upload-pack del lado del clon
    también corre en la jaula, solo lectura."""
    return " ".join(shlex.quote(a) for a in _jail(None, [tree])) + " git-upload-pack"


# ── alta de los repos ───────────────────────────────────────────────────────────


def check_github(url: str) -> dict:
    """Que exista, que el usuario tenga acceso y que sea privado."""
    name = parse_github(url)
    if config.GITHUB_CHECKS:
        import json
        data = json.loads(_run(["gh", "repo", "view", name, "--json", "visibility,isEmpty,nameWithOwner,url"]))
        if data.get("visibility") != "PRIVATE":
            raise CodeError(f"{data['nameWithOwner']} no es privado: tiene que serlo.")
        return data
    return {"nameWithOwner": name, "url": url, "isEmpty": None, "visibility": "?"}


def register(project: str, repo: str, url: str, remote: str | None = None) -> dict:
    """Clona un repo creado por el usuario (con `master` y `develop`): espejo + clon de
    trabajo. `remote` reemplaza la URL de GitHub en las pruebas (un repo local)."""
    if repo not in REPOS:
        raise CodeError(f"repo desconocido: {repo} (front o back)")
    info = check_github(url)
    source = remote or f"https://github.com/{info['nameWithOwner']}.git"
    bare, tree = mirror(project, repo), worktree(project, repo)
    if bare.exists() or tree.exists():
        raise CodeError(f"el repo {repo} de «{project}» ya está registrado ({tree}).")
    # Si algo falla a mitad de camino (por ejemplo, GitHub niega el push), se borra lo que
    # creó este intento: si no, el reintento choca con "ya está registrado" (pasó en la
    # prueba de punta a punta del 5/10/2026 y el usuario tuvo que borrar a mano).
    try:
        bare.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", *_SAFE_GIT, "clone", "--quiet", "--bare", source, str(bare)], timeout=300)
        mirror_git(project, repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")

        # El usuario crea los repos con las dos ramas (master y develop): si están, no se toca
        # nada. Si falta una, se crea sin pisar contenido; si el repo vino vacío, se siembra.
        prod, dev = config.PROD_BRANCH, config.DEV_BRANCH
        branches = mirror_git(project, repo, "branch", "--format=%(refname:short)").split()
        created = []
        if not branches:
            seed = config.STATE_DIR / f"semilla-{slug(project)}-{repo}"
            shutil.rmtree(seed, ignore_errors=True)
            _run(["git", *_SAFE_GIT, "clone", "--quiet", str(bare), str(seed)])
            (seed / "README.md").write_text(f"# {project} · {repo}\n\nRepo creado por el usuario; lo arma el equipo de agentes.\n")
            (seed / ".gitignore").write_text("node_modules/\ndist/\n.env\n.cache/\ncoverage/\n")
            _run(["git", *_SAFE_GIT, "-C", str(seed), "checkout", "--quiet", "-b", prod])
            _run(["git", *_SAFE_GIT, "-C", str(seed), "add", "-A"])
            _run(["git", *_SAFE_GIT, "-c", "user.name=Sistema Orchestrator", "-c", "user.email=sistema@orchestrator.local",
                  "-C", str(seed), "commit", "--quiet", "-m", "Inicio del repo"])
            _run(["git", *_SAFE_GIT, "-C", str(seed), "push", "--quiet", "origin", prod])
            shutil.rmtree(seed, ignore_errors=True)
            created.append(prod)
        elif prod not in branches:
            head = mirror_git(project, repo, "symbolic-ref", "--short", "HEAD").strip()
            mirror_git(project, repo, "branch", prod, head if head in branches else branches[0])
            created.append(prod)
        if dev not in mirror_git(project, repo, "branch", "--format=%(refname:short)").split():
            mirror_git(project, repo, "branch", dev, prod)
            created.append(dev)
        if created:
            mirror_git(project, repo, "push", "--quiet", "origin", *created, timeout=300)

        tree.parent.mkdir(parents=True, exist_ok=True)
        cache_dir(project).mkdir(parents=True, exist_ok=True)
        _run(["git", *_SAFE_GIT, "clone", "--quiet", "--branch", config.DEV_BRANCH, str(bare), str(tree)])
        # Sin remoto: un agente no tiene adónde subir nada.
        _run(["git", *_SAFE_GIT, "-C", str(tree), "remote", "remove", "origin"])
    except BaseException:
        shutil.rmtree(bare, ignore_errors=True)
        shutil.rmtree(tree, ignore_errors=True)
        raise
    return {"repo": repo, "github": info["nameWithOwner"], "local": str(tree)}


# ── tareas ─────────────────────────────────────────────────────────────────────


def prepare_task(project: str, repo: str, task: str) -> None:
    """Deja el clon de trabajo en la rama de la tarea, creada desde `develop` actualizado."""
    mirror_git(project, repo, "fetch", "--quiet", "--prune", "origin", timeout=300)
    branches = mirror_git(project, repo, "branch", "--format=%(refname:short)").split()
    if task not in branches:
        mirror_git(project, repo, "branch", task, config.DEV_BRANCH)
    tree = worktree(project, repo)
    # Lo que quedó a medias de una tarea anterior se commitea en su rama, no se pierde.
    if work_git(project, repo, "status", "--porcelain", "--", ".", ":!.claude").strip():
        current = work_git(project, repo, "branch", "--show-current").strip() or "sin-rama"
        work_git(project, repo, "add", "-A", "--", ".", ":!.claude")
        work_git(project, repo, "commit", "--quiet", "-m", f"{current}: cambios que quedaron sin commitear")
    work_git(project, repo, "fetch", "--quiet", str(mirror(project, repo)),
             f"+refs/heads/{task}:refs/remotes/espejo/{task}",
             f"+refs/heads/{config.DEV_BRANCH}:refs/remotes/espejo/{config.DEV_BRANCH}")
    local = work_git(project, repo, "branch", "--format=%(refname:short)").split()
    if task in local:
        work_git(project, repo, "switch", "--quiet", task)
        # Si la rama ya avanzó en GitHub (por ejemplo, otra corrida), se trae.
        work_git(project, repo, "merge", "--quiet", "--ff-only", f"espejo/{task}")
    else:
        work_git(project, repo, "switch", "--quiet", "-c", task, f"espejo/{task}")
    log.info("clon de %s/%s en la rama %s (%s)", slug(project), repo, task, tree)


def finish_task(project: str, repo: str, task: str) -> dict:
    """Después del agente: commitea lo que haya quedado, lo lleva al espejo y lo sube a
    GitHub. Devuelve los commits nuevos de la rama respecto de `develop`."""
    if work_git(project, repo, "status", "--porcelain", "--", ".", ":!.claude").strip():
        # .claude: el sandbox de Claude deja ahí una carpeta (punto de montaje); nunca va al repo.
        work_git(project, repo, "add", "-A", "--", ".", ":!.claude")
        work_git(project, repo, "commit", "--quiet", "-m", f"{task}: cambios sin commitear (los commiteó el sistema)")
    current = work_git(project, repo, "branch", "--show-current").strip()
    if current != task:
        raise CodeError(f"el agente dejó el clon en la rama «{current}» en vez de «{task}»: no se publica.")
    tree = worktree(project, repo)
    mirror_git(project, repo, "fetch", "--quiet", "--upload-pack", _upload_pack(tree),
               f"file://{tree}", f"+refs/heads/{task}:refs/heads/{task}")
    commits = mirror_git(project, repo, "log", "--format=%h %s", f"{config.DEV_BRANCH}..{task}").strip().splitlines()
    if commits:
        mirror_git(project, repo, "push", "--quiet", "origin", task, timeout=300)
    return {"repo": repo, "rama": task, "commits": commits}


def open_pr(project: str, repo: str, task: str, title: str, body: str) -> str | None:
    """Abre (o encuentra) el pull request de la tarea hacia `develop`. Devuelve su URL."""
    if not config.GITHUB_CHECKS:
        return None
    name = parse_github(_origin_url(project, repo))
    existing = _run(["gh", "pr", "list", "--repo", name, "--head", task, "--base", config.DEV_BRANCH,
                     "--state", "open", "--json", "url", "--jq", ".[0].url"]).strip()
    if existing:
        return existing
    return _run(["gh", "pr", "create", "--repo", name, "--head", task, "--base", config.DEV_BRANCH,
                 "--title", title, "--body", body]).strip().splitlines()[-1]


def _origin_url(project: str, repo: str) -> str:
    return mirror_git(project, repo, "remote", "get-url", "origin").strip()


def diff(project: str, repo: str, task: str, limit: int = 400_000) -> str:
    """El diff de la tarea contra `develop`, para que lo revise el Líder técnico."""
    text = mirror_git(project, repo, "diff", "--stat", "--patch", f"{config.DEV_BRANCH}...{task}")
    return text if len(text) <= limit else text[:limit] + "\n\n[… diff recortado …]\n"


def merge_task(project: str, repo: str, task: str) -> str | None:
    """Mergea la rama de la tarea en `develop`, en el espejo y sin checkout (merge-tree:
    no ejecuta nada del repo), y lo sube. Devuelve el commit del merge, o None si esa
    rama no tiene nada nuevo en este repo."""
    mirror_git(project, repo, "fetch", "--quiet", "--prune", "origin", timeout=300)
    dev = config.DEV_BRANCH
    if task not in mirror_git(project, repo, "branch", "--format=%(refname:short)").split():
        return None
    if not mirror_git(project, repo, "log", "--format=%h", f"{dev}..{task}").strip():
        return None
    result = subprocess.run(["git", *_SAFE_GIT, "merge-tree", "--write-tree", "--name-only", dev, task],
                            cwd=mirror(project, repo), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        conflicts = [line for line in result.stdout.splitlines()[1:] if line and not line.startswith(" ")]
        raise CodeError(f"{task} tiene conflictos con {dev} en {repo}: {', '.join(conflicts[:10]) or result.stderr.strip()}. "
                        "El desarrollador tiene que traer develop a su rama y resolverlos.")
    tree = result.stdout.splitlines()[0].strip()
    old = mirror_git(project, repo, "rev-parse", dev).strip()
    env = {**os.environ, "GIT_AUTHOR_NAME": "Sistema Orchestrator", "GIT_AUTHOR_EMAIL": "sistema@orchestrator.local",
           "GIT_COMMITTER_NAME": "Sistema Orchestrator", "GIT_COMMITTER_EMAIL": "sistema@orchestrator.local"}
    commit = subprocess.run(["git", *_SAFE_GIT, "commit-tree", tree, "-p", old, "-p", task,
                             "-m", f"Merge {task} en {dev} (aprobada por QA y el Líder técnico)"],
                            cwd=mirror(project, repo), capture_output=True, text=True, env=env, check=True).stdout.strip()
    mirror_git(project, repo, "update-ref", f"refs/heads/{dev}", commit, old)
    mirror_git(project, repo, "push", "--quiet", "origin", dev, timeout=300)
    return commit[:7]


_VERDICT = re.compile(r"\*\*Veredicto:\*\*\s*([^\n]+)", re.I)
REVIEWS = {"tecnica": "Revisión técnica", "qa": "Validación de QA"}


def reviews(task_file: Path) -> dict[str, str | None]:
    """Los veredictos de `Desarrollo/<tarea>.md`: la sección de cada revisor y su línea
    «**Veredicto:** …». None si todavía no revisó."""
    text = task_file.read_text(encoding="utf-8") if task_file.exists() else ""
    found: dict[str, str | None] = {}
    for key, title in REVIEWS.items():
        section = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
        verdict = _VERDICT.search(section.group(1)) if section else None
        found[key] = verdict.group(1).strip() if verdict else None
    return found


def approved(verdict: str | None) -> bool:
    return bool(verdict) and verdict.lower().startswith("aprobad")
