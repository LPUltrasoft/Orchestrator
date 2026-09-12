"""Configuración del gateway, leída del entorno (.env)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8787"))
TOKEN = os.environ.get("GATEWAY_TOKEN", "")

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "")).expanduser()
PROJECTS_SUBDIR = os.environ.get("PROJECTS_SUBDIR", "Proyectos")
VAULT_AUTOCOMMIT = os.environ.get("VAULT_AUTOCOMMIT", "true").lower() == "true"

AGY_BIN = os.environ.get("AGY_BIN", "agy")
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "900"))
ORCHESTRATOR_MAX_TURNS = int(os.environ.get("ORCHESTRATOR_MAX_TURNS", "25"))

N8N_CALLBACK_URL = os.environ.get("N8N_CALLBACK_URL", "")

MODELS = {
    "orchestrator": os.environ.get("MODEL_ORCHESTRATOR", "gemini-3.8-flash-high"),
    "producto": os.environ.get("MODEL_PRODUCTO", "gemini-3.8-flash-high"),
    "dba": os.environ.get("MODEL_DBA", "claude-sonnet-4-6"),
    "nestjs": os.environ.get("MODEL_NESTJS", "claude-sonnet-4-6"),
}

ROLES = ("producto", "dba", "nestjs")

STATE_DIR = ROOT / "state"
LOCK_DIR = STATE_DIR / "locks"
PROMPTS_DIR = ROOT / "prompts"
BIN_DIR = ROOT / "bin"

for _d in (STATE_DIR, LOCK_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def projects_root() -> Path:
    return VAULT_PATH / PROJECTS_SUBDIR


def prompt_for(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def validate() -> list[str]:
    """Errores de configuración que hacen que nada vaya a funcionar."""
    problems = []
    if not TOKEN or TOKEN.startswith("cambiame"):
        problems.append("GATEWAY_TOKEN sin definir (o con el valor de ejemplo)")
    if not VAULT_PATH or not VAULT_PATH.is_dir():
        problems.append(f"VAULT_PATH no es un directorio: {VAULT_PATH}")
    elif not (VAULT_PATH / ".git").exists():
        problems.append(f"VAULT_PATH no es repo git, no se puede verificar cambios: {VAULT_PATH}")
    if not Path(AGY_BIN).exists():
        problems.append(f"AGY_BIN no existe: {AGY_BIN}")
    for role in ROLES + ("orchestrator",):
        if not (PROMPTS_DIR / f"{role}.md").exists():
            problems.append(f"falta prompts/{role}.md")
    return problems
