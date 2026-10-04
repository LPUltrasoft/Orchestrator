"""Configuración del gateway, leída del entorno (.env)."""
import os
import shutil
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

HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "8787"))
TOKEN = os.environ.get("GATEWAY_TOKEN", "")

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "")).expanduser()
PROJECTS_SUBDIR = os.environ.get("PROJECTS_SUBDIR", "Proyectos")
VAULT_AUTOCOMMIT = os.environ.get("VAULT_AUTOCOMMIT", "true").lower() == "true"

AGY_BIN = os.environ.get("AGY_BIN", "agy")
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "900"))
ORCHESTRATOR_MAX_TURNS = int(os.environ.get("ORCHESTRATOR_MAX_TURNS", "25"))

# Telegram por long polling: el gateway habla directo con la Bot API.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
TELEGRAM_POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "50"))

# Cuota de agy: con este porcentaje restante o menos (95% usado), el trabajo se pausa
# hasta que la ventana se renueva, y se retoma solo.
QUOTA_MIN_REMAINING = int(os.environ.get("QUOTA_MIN_REMAINING", "5"))
# Claude comparte la suscripción con el uso propio del usuario: sus roles se pausan antes,
# al 90% usado, para dejarle margen (decisión del usuario).
QUOTA_MIN_REMAINING_CLAUDE = int(os.environ.get("QUOTA_MIN_REMAINING_CLAUDE", "10"))
# Margen después de la hora de renovación antes de retomar.
QUOTA_RESUME_BUFFER = int(os.environ.get("QUOTA_RESUME_BUFFER", "60"))

# Stitch (Google Labs): diseño de pantallas. La API key se crea en Stitch → Settings →
# API Keys. Sin ella, todo funciona menos la generación de pantallas.
STITCH_API_KEY = os.environ.get("STITCH_API_KEY", "")
STITCH_URL = os.environ.get("STITCH_URL", "https://stitch.googleapis.com/mcp")
STITCH_MODEL = os.environ.get("STITCH_MODEL", "GEMINI_3_8_FLASH")

# Alternativa: n8n como puerta de Telegram (requiere túnel HTTPS, ver n8n/README.md).
N8N_CALLBACK_URL = os.environ.get("N8N_CALLBACK_URL", "")

# Solo estos chat_id de Telegram pueden dar órdenes. Vacío = cualquiera (no recomendado).
ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}

# Motor, modelo y esfuerzo de cada rol (definidos por el usuario el 4/10/2026). Claude se
# usa solo con su propio CLI (`claude`), nunca dentro de agy. Cada valor se puede cambiar
# en el .env con ENGINE_<ROL>, MODEL_<ROL> y EFFORT_<ROL>.
_DEFAULT_AGENTS = {
    #  rol               motor     modelo                    esfuerzo
    "orchestrator":   ("agy",    "gemini-3.8-flash-high", None),
    "producto":       ("agy",    "gemini-3.1-pro-high",   None),
    "legal":          ("agy",    "gemini-3.1-pro-high",   None),
    "ux":             ("agy",    "gemini-3.1-pro-high",   None),
    "ui":             ("agy",    "gemini-3.1-pro-high",   None),
    "revisor_ux_ui":  ("claude", "claude-sonnet-5-5",     "medium"),
    "qa":             ("claude", "claude-opus-5-5",       "medium"),
    "lider_tecnico":  ("claude", "claude-opus-5-5",       "high"),
    "dba":            ("claude", "claude-opus-5-5",       "high"),
    "team_leader":    ("claude", "claude-opus-5-5",       "high"),
    "seguridad":      ("claude", "claude-opus-5-5",       "high"),
    "nestjs":         ("claude", "claude-opus-5-5",       "medium"),
}
ENGINES = {r: os.environ.get(f"ENGINE_{r.upper()}", e) for r, (e, _, _) in _DEFAULT_AGENTS.items()}
MODELS = {r: os.environ.get(f"MODEL_{r.upper()}", m) for r, (_, m, _) in _DEFAULT_AGENTS.items()}
EFFORTS = {r: os.environ.get(f"EFFORT_{r.upper()}", f) or None for r, (_, _, f) in _DEFAULT_AGENTS.items()}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", shutil.which("claude") or "claude")

ROLES = tuple(role for role in MODELS if role != "orchestrator")

# Modelos que agy ofrece hoy. Se llena al arrancar: Antigravity retira modelos sin
# aviso (Sonnet 4.6 desapareció entre septiembre y octubre de 2026).
KNOWN_MODELS: set[str] | None = None

STATE_DIR = Path(os.environ.get("ORC_STATE_DIR", ROOT / "state"))
LOCK_DIR = STATE_DIR / "locks"
PROMPTS_DIR = ROOT / "prompts"
BIN_DIR = ROOT / "bin"

for _d in (STATE_DIR, LOCK_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def projects_root() -> Path:
    return VAULT_PATH / PROJECTS_SUBDIR


def prompt_for(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8")


def chat_allowed(chat_id: str) -> bool:
    return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS


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
    if not TELEGRAM_BOT_TOKEN and not N8N_CALLBACK_URL:
        problems.append(
            "sin canal de Telegram: definí TELEGRAM_BOT_TOKEN (token de @BotFather)"
        )
    for role, engine in ENGINES.items():
        if engine not in ("agy", "claude"):
            problems.append(f"motor desconocido para '{role}': {engine} (agy o claude)")
    if any(e == "claude" for e in ENGINES.values()) and not shutil.which(CLAUDE_BIN):
        problems.append(f"hay roles con Claude pero no encuentro el CLI: {CLAUDE_BIN}")
    if KNOWN_MODELS:
        for role, model in MODELS.items():
            if ENGINES[role] == "agy" and model not in KNOWN_MODELS:
                problems.append(
                    f"el modelo de '{role}' ({model}) no existe en agy. Ver `agy models`."
                )
    return problems
