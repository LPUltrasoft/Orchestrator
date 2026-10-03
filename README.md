# Orchestrator · Sistema Multi-Agente Supervisado

Equipo de agentes de software que se maneja por Telegram y usa un vault de
Obsidian como memoria compartida.

```
Telegram ◄──── long polling ────► Gateway (host, systemd) ──► agy (orquestador)
                                        │                          │
                                        │                     orc-delegate
                                        ▼                          ▼
                          git: verificación + commit      sub-agentes agy
                                        │               producto / dba / nestjs
                                        ▼                          │
                               Vault de Obsidian ◄─────────────────┘
                                 (memoria compartida)
```

## Por qué está armado así

- **Telegram por long polling, sin webhook.** El gateway sale a buscar los mensajes
  a la Bot API. No hace falta URL pública, túnel ni certificado, y **ningún puerto
  del sistema queda expuesto a internet**. Gateway en `127.0.0.1`.
- **El orquestador es `agy`** (CLI de Antigravity). Trae el patrón supervisor nativo
  y corre con la sesión ya autenticada del host. Solo modelos Gemini.
- **La memoria por chat es el `conversation_id` de `agy`**, persistido en
  `state/sessions.json` y reanudado con `--conversation`.
- **Obsidian se conecta con `--add-dir`.** Los agentes corren en el host y tienen
  filesystem real: no hace falta plugin ni volúmenes.
- **No se le cree al agente: se verifica con git.** El gateway compara el estado de
  git antes y después de cada sub-agente y reporta `files_changed` real.

## Puesta en marcha

### 1. Configurar

```bash
cp .env.example .env
sed -i "s|^GATEWAY_TOKEN=.*|GATEWAY_TOKEN=$(openssl rand -hex 24)|" .env
chmod 600 .env
$EDITOR .env    # VAULT_PATH, ALLOWED_CHAT_IDS, TELEGRAM_BOT_TOKEN
```

- `TELEGRAM_BOT_TOKEN`: lo da **@BotFather** con `/newbot`.
- `ALLOWED_CHAT_IDS`: tu chat_id. A cualquier otro chat el bot no le contesta nada.
- `MODEL_*`: `agy models` lista los disponibles. El gateway los valida al arrancar.

### 2. Permisos de `agy` (obligatorio)

En headless, `agy` **auto-deniega** cualquier comando shell sin allow-rule: la
respuesta vuelve vacía con `status: SUCCESS` y un campo `denied_actions`.

En `~/.gemini/antigravity-cli/settings.json` (hacé backup antes, porque `agy` lo
sobrescribe si no lo puede parsear):

```json
{
  "permissions": {
    "allow": [
      "command(orc-delegate)",
      "command(which)", "command(ls)", "command(cat)", "command(head)", "command(grep)",
      "command(git log)", "command(git status)", "command(git diff)"
    ]
  }
}
```

Las reglas matchean **por prefijo**, y `agy` analiza los comandos compuestos:
`ls && touch x` se deniega porque `touch` no está permitido (verificado).

Si un agente intenta un comando que no está en la lista, el gateway **no corta el
turno**: reanuda la conversación con una indicación para que use sus herramientas
nativas, hasta dos veces. Por eso la lista puede quedar corta. **No agregues `find`**:
puede borrar (`-delete`) o ejecutar (`-exec`) dentro de un solo comando.

> ⚠️ **No permitas `command(git config)`**: con `core.fsmonitor` o `core.hooksPath`
> un agente con una instrucción inyectada puede plantar un comando que después
> ejecuta cualquier `git status`. El gateway anula esas dos claves en sus propias
> llamadas, pero tus otras herramientas no.

### 3. Levantar el gateway como servicio

```bash
uv sync --project gateway
cp systemd/orchestrator-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now orchestrator-gateway
curl -s localhost:8787/health | jq      # "status": "ok"
```

Para que arranque al bootear sin iniciar sesión: `loginctl enable-linger $USER`.

Si `/health` dice `misconfigured`, el campo `problems` dice exactamente qué falta.

## Uso

Le escribís al bot en lenguaje natural:

> Tengo una idea para automatizar avisos de WhatsApp para turnos médicos

El bot contesta "👀 Tomado", muestra "escribiendo…" mientras el equipo trabaja, y al
terminar te resume qué quedó escrito en Obsidian y propone el siguiente paso.

| Comando | Qué hace |
|---------|----------|
| `/reset` | Conversación nueva: el orquestador olvida el contexto |
| `/estado` | Qué está haciendo el equipo ahora |
| `/ayuda` | Ayuda |

Si mandás un mensaje mientras otro se procesa, queda en cola ("📥 Lo anoto") y se
atiende después: dos turnos en paralelo sobre la misma conversación se pisarían.

## Sub-agentes

| Rol | Escribe en el vault | Modelo |
|-----|---------------------|--------|
| orquestador | — (delega) | `gemini-3.8-flash-high` |
| `producto` | `00 - Índice`, `01 - Requerimientos` | `gemini-3.8-flash-high` |
| `dba` | `03 - Modelo de Datos` | `gemini-3.1-pro-high` |
| `nestjs` | `02 - Arquitectura`, `04 - API REST` | `gemini-3.1-pro-high` |

**Solo el orquestador puede delegar.** El gateway marca quién invoca cada `agy` en
`ORC_CALLER`, y `orc-delegate` se niega si lo llama un sub-agente.

Los roles se definen en `prompts/*.md`. Para agregar uno: `prompts/<rol>.md`, sumarlo
a `ROLES` y `MODELS` en `gateway/config.py`, y mencionarlo en
`prompts/orchestrator.md`.

## API del gateway

Todo pide el header `X-Orc-Token`, menos `/health`. Solo escucha en `127.0.0.1`.

| Método | Ruta | Para qué |
|--------|------|----------|
| `GET` | `/health` | Estado, problemas de configuración, trabajos en vuelo |
| `POST` | `/chat` | Mensaje por HTTP (pruebas) → `202 {job_id}` |
| `POST` | `/agents/{rol}` | Invocar un sub-agente directo (síncrono) |
| `GET` | `/jobs/{id}` | Estado de un trabajo |
| `POST` | `/sessions/{chat_id}/reset` | Borrar la memoria de ese chat |
| `GET` | `/projects` | Proyectos en el vault |

## Operación

- **Reiniciar es seguro.** La unit usa `KillMode=mixed`: el gateway deja de tomar
  mensajes, espera hasta 280 s a que terminen los trabajos en curso, entrega las
  respuestas y recién ahí sale. Verificado con SIGTERM a mitad de un turno.
- **Mensajes que llegan con el gateway caído** se atienden al volver: Telegram los
  guarda 24 h y el offset está persistido en `state/telegram_offset`.
- **El vault se commitea solo.** Un sub-agente que termina mal deja un commit marcado
  `[INCOMPLETO]` y el error llega a Telegram. Deshacer lo último:
  `git -C "$VAULT_PATH" reset --hard HEAD~1`.
- **Un agente por proyecto a la vez** (lock por proyecto, devuelve 409).
- **Logs:** `journalctl --user -u orchestrator-gateway -f` y `state/jobs.jsonl`.

## Cómo falla `agy` en silencio (y cómo lo detecta el gateway)

| Síntoma | Causa | Detección |
|---------|-------|-----------|
| Dice "escribí X" y no escribió nada | Falta `--mode accept-edits` | `runner.py` siempre lo pasa; git verifica |
| `response` vacío, `status: SUCCESS` | Comando shell sin allow-rule | Lee `denied_actions` y nombra la regla |
| Se queda esperando y devuelve vacío | Sesión OAuth vencida | Marcadores de login en la salida |
| `status: ERROR` con JSON prolijo | Corte a mitad de camino | Error explícito y commit `[INCOMPLETO]` |
| Falla con lista de modelos | Antigravity retiró el modelo | Validación de modelos al arrancar |

Y una más que no es falla pero se le parece: **Gemini ignora las reglas de formato del
prompt** y responde con `**negritas**` y links `file://`. `telegram.to_plain_text()`
limpia el Markdown antes de enviar.

## Probar sin tocar nada real

Un script que llame a `gateway.runner` hereda el entorno real: si el agente decide
delegar, `orc-delegate` le pega al gateway y al vault de verdad (pasó). Aislalo siempre:

```bash
GATEWAY_PORT=9 GATEWAY_TOKEN=invalido ORC_STATE_DIR=/tmp/orc-test \
  uv run --project gateway python mi_prueba.py
```

## Alternativa: n8n como puerta de Telegram

Los workflows están en `n8n/` y el contenedor en `docker-compose.yml` (en la raíz,
porque toma los tokens del `.env`). Requiere un túnel HTTPS público para el Telegram
Trigger. Ver [`n8n/README.md`](n8n/README.md).
