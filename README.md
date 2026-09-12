# Orchestrator · Sistema Multi-Agente Supervisado

Equipo de agentes de software que se maneja por Telegram y usa un vault de
Obsidian como memoria compartida.

```
Telegram ──► n8n (Docker) ──► Gateway HTTP (host) ──► agy (orquestador)
   ▲              │                    │                      │
   │              │                    │              orc-delegate
   └──────────────┘                    ▼                      ▼
      callback              git commit + verificación   sub-agentes
                                       │            producto / dba / nestjs
                                       ▼                      │
                          Vault de Obsidian ◄─────────────────┘
                            (memoria compartida)
```

## Por qué está armado así

- **El orquestador es `agy`, no un nodo de n8n.** `agy` trae el supervisor pattern
  nativo y corre con tu sesión ya autenticada, así que no hace falta una API key
  aparte para el cerebro que rutea.
- **La memoria por chat es el `conversation_id` de `agy`.** Se guarda el mapa
  `chat_id → conversation_id` y se reanuda con `--conversation`. Reemplaza al
  Window Buffer Memory.
- **Obsidian se conecta con `--add-dir`.** Los agentes corren en el host y tienen
  filesystem real: no hace falta el plugin Local REST API ni montar volúmenes.
- **Todo es asíncrono.** Un turno tarda entre 1 y 5 minutos; Telegram y el nodo
  HTTP de n8n no esperan tanto. El gateway contesta `202 {job_id}` al instante y
  devuelve el resultado por callback.
- **No se le cree al agente: se verifica con git.** `agy` puede decir que escribió
  un archivo sin haberlo hecho. El gateway compara el estado de git antes y después
  y reporta `files_changed` real. Si el agente dice que escribió y git no vio nada,
  marca `suspect_no_writes: true`.

## Puesta en marcha

### 1. Configurar

```bash
cp .env.example .env   # ya está creado con un token random
$EDITOR .env           # revisá VAULT_PATH y los modelos
```

`agy models` lista los modelos disponibles para `MODEL_*`.

### 2. Levantar el gateway

```bash
uv sync --project gateway
./scripts/run-gateway.sh              # en primer plano, para probar
curl -s localhost:8787/health | jq    # "status": "ok"
```

Como servicio permanente:

```bash
cp systemd/orchestrator-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now orchestrator-gateway
journalctl --user -u orchestrator-gateway -f
```

### 3. Levantar n8n

```bash
docker compose up -d
```

Entrá a http://localhost:5678 y creá la cuenta local.

### 4. Importar el workflow

1. **Workflows → Import from File** → `n8n/workflow-telegram-orchestrator.json`.
2. Creá la credencial **Telegram account** con el token que te da @BotFather y
   seleccionala en los tres nodos de Telegram (vienen con `REEMPLAZAR`).
3. En el nodo **Gateway · POST /chat**, pegá tu `GATEWAY_TOKEN` en el header
   `X-Orc-Token`.
4. Activá el workflow. Copiá la **Production URL** del nodo Webhook y ponela en
   `N8N_CALLBACK_URL` del `.env`, después reiniciá el gateway.

> Un solo bot por token: si otro proceso ya está consumiendo ese bot, Telegram
> corta una de las dos conexiones.

## Uso

Le escribís al bot en lenguaje natural:

> Tengo una idea para automatizar avisos de WhatsApp para turnos médicos al estilo Sinergia

El orquestador entiende, delega al Agente Producto, y te resume qué quedó escrito
en Obsidian más el siguiente paso sugerido. Respondés "sí, dale" y delega al DBA.

## Sub-agentes

| Rol | Escribe en el vault | Modelo por defecto |
|-----|---------------------|--------------------|
| `producto` | `00 - Índice`, `01 - Requerimientos` | Gemini 3.8 Flash |
| `dba` | `03 - Modelo de Datos` | Claude Sonnet 4.6 |
| `nestjs` | `02 - Arquitectura`, `04 - API REST` | Claude Sonnet 4.6 |

Los roles se definen en `prompts/*.md`: editá esos archivos para cambiar el
comportamiento. Para agregar un rol nuevo, creá `prompts/<rol>.md`, agregalo a
`ROLES` y a `MODELS` en `gateway/config.py`, y mencionalo en
`prompts/orchestrator.md`.

## API del gateway

Todo pide el header `X-Orc-Token`, menos `/health`.

| Método | Ruta | Para qué |
|--------|------|----------|
| `GET` | `/health` | Estado y problemas de configuración |
| `POST` | `/chat` | Mensaje de Telegram → `202 {job_id}` |
| `POST` | `/agents/{rol}` | Invocar un sub-agente directo (síncrono) |
| `GET` | `/jobs/{id}` | Estado de un job |
| `POST` | `/sessions/{chat_id}/reset` | Borrar la memoria de ese chat |
| `GET` | `/projects` | Proyectos en el vault |

Probar un sub-agente sin pasar por Telegram:

```bash
TOKEN=$(grep '^GATEWAY_TOKEN=' .env | cut -d= -f2)
curl -sS -X POST localhost:8787/agents/producto \
  -H 'Content-Type: application/json' -H "X-Orc-Token: $TOKEN" \
  -d '{"project":"Mi Proyecto","instruction":"Creá el 00 y el 01."}' | jq
```

## Operación

- **El vault se commitea solo.** Cada turno de sub-agente genera un commit
  (`VAULT_AUTOCOMMIT=false` lo desactiva). Para deshacer lo último:
  `git -C "$VAULT_PATH" reset --hard HEAD~1`.
- **Un agente por proyecto a la vez.** Hay lock por proyecto; un segundo pedido
  sobre el mismo proyecto devuelve `409` en vez de corromper los documentos.
- **La conversación se recicla** cada `ORCHESTRATOR_MAX_TURNS` turnos, porque el
  input crece turno a turno.
- **Logs:** `state/jobs.jsonl` (histórico) y `journalctl --user -u orchestrator-gateway`.

## Gotcha importante

`agy -p` **sin `--mode accept-edits` no ejecuta herramientas pero responde como si
las hubiera ejecutado.** Por eso `runner.py` siempre lo pasa. Si algún día los
agentes "dicen que escriben" y el vault no cambia, es lo primero que hay que mirar.
