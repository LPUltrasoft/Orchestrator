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
# Subir el vault a GitHub solo: cada VAULT_PUSH_INTERVAL segundos, si hay commits sin
# subir. Si falla durante VAULT_PUSH_ALERT_AFTER segundos seguidos, avisa por Telegram.
VAULT_AUTOPUSH = os.environ.get("VAULT_AUTOPUSH", "true").lower() == "true"
VAULT_PUSH_INTERVAL = int(os.environ.get("VAULT_PUSH_INTERVAL", "30"))
VAULT_PUSH_ALERT_AFTER = int(os.environ.get("VAULT_PUSH_ALERT_AFTER", "1800"))

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
    "imagenes":       ("agy",    "gemini-3.1-pro-high",   None),
    "revisor_ux_ui":  ("claude", "claude-sonnet-5-5",     "medium"),
    "qa":             ("claude", "claude-opus-5-5",       "medium"),
    "lider_tecnico":  ("claude", "claude-opus-5-5",       "high"),
    "dba":            ("claude", "claude-opus-5-5",       "high"),
    "team_leader":    ("claude", "claude-opus-5-5",       "high"),
    "seguridad":      ("claude", "claude-opus-5-5",       "high"),
    "backend":        ("claude", "claude-opus-5-5",       "medium"),
    "frontend":       ("claude", "claude-sonnet-5-5",     "medium"),
    "devops":         ("claude", "claude-opus-5-5",       "high"),
}
ENGINES = {r: os.environ.get(f"ENGINE_{r.upper()}", e) for r, (e, _, _) in _DEFAULT_AGENTS.items()}
MODELS = {r: os.environ.get(f"MODEL_{r.upper()}", m) for r, (_, m, _) in _DEFAULT_AGENTS.items()}
EFFORTS = {r: os.environ.get(f"EFFORT_{r.upper()}", f) or None for r, (_, _, f) in _DEFAULT_AGENTS.items()}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", shutil.which("claude") or "claude")
# Drive de los backups: antes de cada backup se revisa el espacio (POST /drive/revision) y
# se avisa por Telegram si la cuenta llegó a DRIVE_ALERT_GB (los 15 GB gratis se
# comparten con Gmail y Fotos).
RCLONE_BIN = os.environ.get("RCLONE_BIN", shutil.which("rclone") or str(Path.home() / ".local/bin/rclone"))
DRIVE_REMOTE = os.environ.get("DRIVE_REMOTE", "drive-backups")
DRIVE_ALERT_GB = float(os.environ.get("DRIVE_ALERT_GB", "14"))
# Etapa 3: código de los proyectos. Los clones de trabajo (donde programan los agentes,
# en el sandbox) van en PROJECTS_CODE_DIR/<proyecto>/{front,back}; los espejos con el
# remoto de GitHub (los únicos con credenciales), en MIRRORS_DIR.
PROJECTS_CODE_DIR = Path(os.environ.get("PROJECTS_CODE_DIR", Path.home() / "Proyectos"))
MIRRORS_DIR = Path(os.environ.get("MIRRORS_DIR", Path.home() / ".local/share/orchestrator/repos"))
# Ramas de los repos que crea el usuario (todos vienen con las dos): producción y desarrollo.
PROD_BRANCH = os.environ.get("PROD_BRANCH", "master")
DEV_BRANCH = os.environ.get("DEV_BRANCH", "develop")
# Puertos de cada proyecto: un bloque de PORTS_BLOCK desde PORTS_BASE (la mitad para
# desarrollo, solo en 127.0.0.1; la otra para producción), asignado al registrar los repos.
PORTS_BASE = int(os.environ.get("PORTS_BASE", "18100"))
PORTS_BLOCK = int(os.environ.get("PORTS_BLOCK", "20"))
# Validar los repos con `gh` y abrir PRs. Apagado solo en pruebas con remotos locales.
GITHUB_CHECKS = os.environ.get("GITHUB_CHECKS", "true").lower() == "true"
# Roles que programan en los repos, con Bash dentro del sandbox de Claude Code.
CODE_ROLES = ("backend", "frontend", "qa", "devops")
# A qué repos entra cada uno: el suyo, o los dos.
CODE_REPOS = {"backend": ("back",), "frontend": ("front",), "qa": ("front", "back"), "devops": ("front", "back")}
# Dominios a los que puede salir el Bash del sandbox: solo registros de paquetes.
SANDBOX_DOMAINS = tuple(d.strip() for d in os.environ.get(
    "SANDBOX_DOMAINS",
    "registry.npmjs.org,repo.maven.apache.org,repo1.maven.org,pypi.org,files.pythonhosted.org",
).split(",") if d.strip())
# Toolchains instalados en el home (el sandbox oculta el home): solo lectura.
SANDBOX_TOOLCHAINS = tuple(p for p in (Path.home() / ".nvm",) if p.exists())

# Jenkins (nativo, como orc-ci, solo en 127.0.0.1). El token de API lo carga el usuario
# en el .env; la credencial de GitHub, en Jenkins (por ID).
JENKINS_URL = os.environ.get("JENKINS_URL", "").rstrip("/")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_TOKEN = os.environ.get("JENKINS_TOKEN", "")
JENKINS_CREDENTIALS_ID = os.environ.get("JENKINS_CREDENTIALS_ID", "github")

# Autoprueba de los motores (al arrancar y una vez por día, a SELFTEST_HOUR hora local),
# con el modelo más barato de cada uno.
SELFTEST_AGY_MODEL = os.environ.get("SELFTEST_AGY_MODEL", "gemini-3.8-flash-low")
SELFTEST_CLAUDE_MODEL = os.environ.get("SELFTEST_CLAUDE_MODEL", "claude-haiku-4-5")
SELFTEST_HOUR = int(os.environ.get("SELFTEST_HOUR", "9"))
# Donde agy guarda lo que genera cada conversación (por ejemplo, las imágenes).
AGY_BRAIN_DIR = Path(os.environ.get("AGY_BRAIN_DIR", Path.home() / ".gemini/antigravity-cli/brain"))

# MCP que puede usar cada rol de Claude: solo estos, los del usuario no se cargan nunca.
# Cada uno declara las herramientas que se le permiten, de a una. Se cambia con
# MCP_<ROL>=context7 en el .env (vacío = ninguno).
MCP_SERVERS = {
    # Documentación actual de librerías y frameworks (context7.com). Solo lectura, sin cuenta.
    "context7": {
        "config": {"type": "http", "url": "https://mcp.context7.com/mcp"},
        "tools": ("resolve-library-id", "query-docs"),
    },
}
_DEFAULT_MCPS = {"lider_tecnico": "context7", "dba": "context7", "devops": "context7"}
MCPS = {
    r: tuple(m.strip() for m in os.environ.get(f"MCP_{r.upper()}", _DEFAULT_MCPS.get(r, "")).split(",") if m.strip())
    for r in _DEFAULT_AGENTS
}

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
    for role, servers in MCPS.items():
        for server in servers:
            if server not in MCP_SERVERS:
                problems.append(f"MCP desconocido para '{role}': {server}")
        if servers and ENGINES[role] != "claude":
            problems.append(f"'{role}' tiene MCP pero corre con {ENGINES[role]}: solo los roles de Claude los usan")
    if any(e == "claude" for e in ENGINES.values()) and not shutil.which(CLAUDE_BIN):
        problems.append(f"hay roles con Claude pero no encuentro el CLI: {CLAUDE_BIN}")
    if KNOWN_MODELS:
        for role, model in MODELS.items():
            if ENGINES[role] == "agy" and model not in KNOWN_MODELS:
                problems.append(
                    f"el modelo de '{role}' ({model}) no existe en agy. Ver `agy models`."
                )
    return problems
