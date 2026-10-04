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
      "command(orc-delegate)", "command(orc-mesa)", "command(orc-aprobacion)",
      "command(orc-estado)", "command(orc-diseno)",
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

Mientras el equipo trabaja ves **un mensaje de progreso que se edita en vivo**: qué
agente está activo, sobre qué proyecto, cuánto lleva y qué está haciendo:

```
⏳ Trabajando… 0:51

⏳ 🧭 Director · 0:51
   📖 lee 01 - Requerimientos Funcionales y Reglas de …
   ↪ delega en 📋 Producto

⏳ 📋 Producto · Sistema de Turnos Medicos · 0:25
   📖 lee 01 - Requerimientos Funcionales y Reglas de …
   ✏️ edita 01 - Requerimientos Funcionales y Reglas de …
```

Es un solo mensaje que se edita, así que no llena el chat ni hace sonar el celular. La
respuesta final llega aparte, como mensaje nuevo, para que sí notifique.

Cómo funciona: `agy` corre con `--output-format stream-json`, que emite un evento por
cada paso (qué herramienta usa y sobre qué archivo). El orquestador recibe el id del
trabajo en `ORC_JOB_ID`, `orc-delegate` lo reenvía en el header `X-Orc-Job`, y así los
pasos de cada sub-agente se suman al mensaje correcto.

| Comando | Qué hace |
|---------|----------|
| `/reset` | Conversación nueva: el orquestador olvida el contexto |
| `/estado` | Qué está haciendo el equipo ahora |
| `/ayuda` | Ayuda |

Si mandás un mensaje mientras otro se procesa, queda en cola ("📥 Lo anoto") y se
atiende después: dos turnos en paralelo sobre la misma conversación se pisarían.

## El proceso y los roles

Los proyectos nuevos siguen un proceso por fases, con aprobación del usuario al cerrar
cada una (detalle completo en el vault: `05 - Proceso de Desarrollo de Productos`):

| Fase | Quién trabaja | Puerta |
|------|---------------|--------|
| 1 · Descubrimiento | producto, qa | alcance |
| 2 · Mesa técnica | lider_tecnico, dba, producto (por rondas) | cada ADR, y stack |
| 3 · Propuesta y contrato | team_leader, lider_tecnico, legal | contrato |
| 5 · Diseño y datos | ux, ui, dba, lider_tecnico, seguridad | diseño |
| 6 · Planificación | team_leader, qa | plan |

La fase 4 (repos) y de la 7 en adelante (desarrollo, release, operación) son de la
etapa 3 del sistema, todavía no implementada.

**Todos los proyectos son webs**, mobile first pero funcionando en celular, tablet y
escritorio (nada de apps nativas): Stitch genera cada pantalla en celular y escritorio, y
QA prueba en los tres tamaños.

**Todo proyecto tiene dos ambientes, desarrollo y producción**: el mismo código con
distinta configuración, una base por ambiente, datos de prueba solo en desarrollo y
nunca datos reales ahí. A desarrollo se despliega solo; a producción, únicamente con la
aprobación del usuario (puerta «release»). Los roles lo tienen en sus prompts y el
detalle está en el vault (documento 05, §7 y §8).

**El gateway impone las puertas**, no el orquestador: delegar en un rol de una fase
futura devuelve 409 con lo que falta aprobar. El estado de cada proyecto vive en el
vault, en `Estado del Proyecto.md` (legible, con los datos en un bloque JSON al final),
y cada cambio se commitea. Los proyectos anteriores al proceso no tienen ese archivo y
trabajan sin fases.

| Rol | Escribe | Modelo |
|-----|---------|--------|
| orquestador | — (coordina) | `gemini-3.8-flash-high` |
| `producto` | 00 visión, 01 requerimientos con MVP y backlog | `gemini-3.8-flash-high` |
| `qa` | 11 plan de pruebas | `gemini-3.8-flash-high` |
| `lider_tecnico` | ADRs, 02 arquitectura, 04 API (OpenAPI) | `gemini-3.1-pro-high` |
| `dba` | 03 modelo y funciones almacenadas | `gemini-3.1-pro-high` |
| `legal` | 09 propuesta y contrato (borrador) | `gemini-3.1-pro-high` |
| `team_leader` | 08 estimaciones, 10 plan de trabajo | `gemini-3.8-flash-high` |
| `ux` | 05, sección UX | `gemini-3.8-flash-high` |
| `ui` | 05, sección UI | `gemini-3.8-flash-high` |
| `seguridad` | 12 revisión de seguridad | `gemini-3.1-pro-high` |
| `nestjs` | Desarrollo backend (etapa 3) | `gemini-3.1-pro-high` |

Cada modelo se cambia con `MODEL_<ROL>` en el `.env`.

### Comandos del orquestador

| Comando | Qué hace |
|---------|----------|
| `orc-estado <proyecto>` | Fase, qué hacer ahora, qué espera al usuario |
| `orc-delegate <rol> <proyecto> "<instrucción>"` | Delega y espera el resultado |
| `orc-mesa <proyecto> "<tema>" [rondas]` | Convoca la mesa técnica, en segundo plano |
| `orc-diseno <proyecto> [editar <id> "<cambio>"]` | Genera o corrige pantallas en Stitch y te manda las capturas |
| `orc-aprobacion <proyecto> <puerta> "<resumen>"` | Le manda al usuario el pedido con botones |

Todos tienen `--help` y comparten `bin/_orc_common.sh`. **Solo el orquestador puede
usarlos**: el gateway marca quién invoca cada `agy` en `ORC_CALLER`.

### Aprobaciones con botones

`orc-aprobacion` manda un mensaje con **✅ Aprobar** y **✏️ Pedir cambios**:

- **Aprobar:** el mensaje queda marcado con quién y cuándo, el proyecto avanza de fase
  (o el ADR queda marcado como aprobado en su archivo) y el gateway despierta al
  orquestador con un "(Mensaje del sistema)" que dice qué hacer ahora.
- **Pedir cambios:** el próximo mensaje del usuario le llega al orquestador como los
  cambios pedidos, para que los delegue y vuelva a pedir la aprobación.
- Un pedido nuevo de la misma puerta reemplaza al anterior; un botón viejo responde
  "ya no está vigente".

### Diseño con Stitch

En la fase 5, el rol `ui` escribe `Diseño/DESIGN.md` (sistema de diseño) y
`Diseño/Pantallas.md` (cada pantalla con su prompt, en un bloque JSON). `orc-diseno`
hace el resto, en el gateway: crea el proyecto en Stitch, carga el sistema de diseño
(`upload_design_md` + `create_design_system_from_design_md`), genera cada pantalla para
celular, baja la captura y el HTML a `Diseño/Pantallas/`, arma el índice
`Pantallas generadas.md` y te manda las capturas por Telegram en un álbum. Si una
pantalla falla, sigue con las demás. `orc-diseno <proyecto> editar <id> "<cambio>"`
corrige una pantalla con la edición nativa de Stitch.

Habla con el MCP oficial (`https://stitch.googleapis.com/mcp`, sin estado, JSON plano)
desde `gateway/stitch.py`, no desde el agente: así puede bajar las capturas y la API key
queda en el `.env` (`STITCH_API_KEY`, se crea en Stitch → Settings → API Keys). Modelo:
`STITCH_MODEL`, por defecto `GEMINI_3_8_FLASH`.

### La mesa técnica

`orc-mesa` corre las rondas en el gateway, no en el turno del orquestador (un debate de
diez minutos no entra en su timeout). Cada participante escribe su postura en un
archivo propio de `Mesa Técnica/NN - tema/`, así nadie pisa la de otro. El Líder técnico
cierra con un `Resumen.md` y los ADRs, siempre "Propuesto". Al terminar, el gateway
despierta al orquestador para que pida la aprobación de cada uno. Se pausa por cuota
como cualquier otro trabajo y retoma desde el paso donde quedó.

Para agregar un rol: `prompts/<rol>.md`, sumarlo a `_DEFAULT_MODELS` en
`gateway/config.py`, a las fases de `gateway/process.py` y al prompt del orquestador.

## API del gateway

Todo pide el header `X-Orc-Token`, menos `/health`. Solo escucha en `127.0.0.1`.

| Método | Ruta | Para qué |
|--------|------|----------|
| `GET` | `/health` | Estado, problemas de configuración, trabajos en vuelo |
| `POST` | `/chat` | Mensaje por HTTP (pruebas) → `202 {job_id}` |
| `POST` | `/agents/{rol}` | Invocar un sub-agente directo (síncrono; respeta las fases) |
| `POST` | `/aprobaciones` | Pedido de aprobación con botones (`orc-aprobacion`) |
| `POST` | `/mesa` | Convocar la mesa técnica (`orc-mesa`) |
| `POST` | `/diseno` | Generar o corregir pantallas en Stitch (`orc-diseno`) |
| `GET` | `/proyectos/{proyecto}/estado` | Fase y qué sigue (`orc-estado`) |
| `GET` | `/jobs/{id}` | Estado de un trabajo |
| `POST` | `/sessions/{chat_id}/reset` | Borrar la memoria de ese chat |
| `GET` | `/projects` | Proyectos en el vault |

## Control de cuota

Antes de cada turno del orquestador y de cada sub-agente, el gateway consulta
`agy -p "/quota"`, que informa por familia de modelos cuánto queda de la ventana de 5
horas y de la semanal, y cuándo se renueva cada una. Consultarla no consume cuota.

Si a alguna le queda `QUOTA_MIN_REMAINING`% o menos (5% por defecto, o sea 95% usado):

- **Al empezar un turno:** el pedido se guarda y llega
  "⏸️ Pausé el trabajo: se usó el 97% de la cuota de 5 horas de Gemini. Se renueva hoy a
  las 05:22; ahí retomo solo y te aviso." Los mensajes que lleguen mientras tanto quedan
  anotados.
- **A mitad de turno**, cuando el orquestador está por delegar: se rechaza la
  delegación, el orquestador cierra su turno y el gateway agenda la continuación.
- **Al renovarse:** "▶️ Se renovó la cuota. Retomo…", con todos los pedidos de ese chat
  **en el orden en que llegaron**.
- Las pausas se guardan en `state/paused.json`: **sobreviven a un reinicio** mientras
  esperan.

`/estado` en Telegram muestra lo que está en pausa y cuándo se retoma; `/health` muestra
la cuota de cada ventana.

Para probar sin agotar la cuota de verdad: `ORC_QUOTA_FILE=/ruta/quota.txt` hace que el
gateway lea la salida de `/quota` de ese archivo.

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
