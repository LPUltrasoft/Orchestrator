# Alternativa: n8n como puerta de Telegram

El camino principal es el long polling del gateway (ver el README raíz). Esta
carpeta deja lista la alternativa con n8n, por si en algún momento se quiere el
lienzo visual o integrar otros nodos.

## Qué hace falta para que funcione el Telegram Trigger

El Telegram Trigger **no hace polling: registra un webhook en Telegram**. Y Telegram
solo acepta webhooks que cumplan todo esto:

1. **URL pública con HTTPS**, en el puerto 443, 80, 88 u 8443. Con
   `WEBHOOK_URL=http://localhost:5678/`, al publicar el workflow Telegram responde
   *"Bad Request: bad webhook: An HTTPS URL must be provided for webhook"*.
2. **Un solo webhook por bot.** Por eso:
   - El long polling del gateway y el Telegram Trigger son **excluyentes**: con el
     webhook activo, `getUpdates` devuelve 409. El gateway lo detecta y lo reporta en
     `/health`.
   - La URL de test y la de producción de n8n se pisan entre sí: probar con
     "Listen for test event" mientras el workflow está publicado rompe producción.
     Hay que despublicarlo para probar, o usar un segundo bot para test.

Para tener HTTPS público desde esta PC hace falta un túnel:

| Opción | Requiere | URL | Exposición |
|--------|----------|-----|------------|
| Cloudflare Tunnel con nombre | Cuenta + dominio propio en Cloudflare | Estable | Se puede limitar a una sola ruta con reglas de `ingress` |
| Cloudflare Quick Tunnel (`trycloudflare.com`) | Nada | **Cambia en cada arranque**: hay que actualizar `WEBHOOK_URL` y reiniciar n8n | Expone n8n entero, sin reglas de ruta |
| ngrok | Cuenta (un dominio estático gratis) | Estable | n8n entero salvo que se configuren políticas de tráfico |

`cloudflared` ya está instalado en esta PC (`~/.local/bin/cloudflared`), pero sin
cuenta configurada.

## Seguridad, verificada en el código de n8n 2.41.6

- **El Telegram Trigger sí verifica el origen.** Registra un `secret_token` al crear
  el webhook y rechaza con 403 todo request sin el header
  `X-Telegram-Bot-Api-Secret-Token` correcto (comparación en tiempo constante). Un
  tercero no puede falsificar mensajes.
- **El riesgo está en el resto de n8n.** Un túnel sin reglas de ruta publica también
  la UI y los demás webhooks. Por eso el workflow de salida exige el header
  `X-Orc-Token`: sin él responde 403 y no manda nada.
- Lo ideal es que el túnel exponga **solo** la ruta del trigger:
  `/webhook/orc-telegram-in/webhook`.

## Los workflows

Dos workflows separados, porque se activan en momentos distintos:

| Archivo | Flujo | Necesita túnel |
|---------|-------|----------------|
| `workflow-entrada.json` | Telegram Trigger → filtro de chat → acuse → `POST /chat` | Sí |
| `workflow-salida.json` | Webhook → verifica `X-Orc-Token` → Telegram | No |

Ninguno lleva secretos: leen `ORC_GATEWAY_TOKEN` y `ORC_ALLOWED_CHAT_IDS` de las
variables de entorno, que `docker-compose.yml` toma del `.env`.

El filtro de chat está en n8n y no solo en el gateway para que a un extraño no le
llegue ni el "👀 Tomado".

## Levantar la alternativa

```bash
cd ~/Documentos/Orchestrator
docker compose up -d
for f in entrada salida; do
  docker cp n8n/workflow-$f.json n8n:/tmp/wf-$f.json
  docker exec n8n n8n import:workflow --input=/tmp/wf-$f.json
done
```

Después:

1. Crear el owner en `http://localhost:5678` y la credencial **Telegram** con el
   token del bot, y asignarla a los nodos de Telegram.
2. Levantar el túnel y poner su URL HTTPS en `WEBHOOK_URL` del `docker-compose.yml`.
3. En el `.env`: `N8N_CALLBACK_URL=http://localhost:5678/webhook/orchestrator-reply`
   y `TELEGRAM_BOT_TOKEN=` vacío (si no, el gateway hace polling y choca con el webhook).
4. Publicar los dos workflows y reiniciar el gateway.

## Detalles de n8n 2.x que costaron

- **Red del host, no bridge.** Con ufw activo, el tráfico contenedor → host (el
  gateway en el 8787) se descarta en silencio. Y los puertos publicados por Docker
  se saltean ufw y quedan abiertos a la red local. Con `network_mode: host` y
  `N8N_LISTEN_ADDRESS=127.0.0.1` no pasa ninguna de las dos cosas.
- **Volumen con nombre, no bind mount.** Docker crea `./n8n_data` como root y n8n
  (uid 1000) muere en loop con `EACCES: permission denied, open '/home/node/.n8n/config'`.
- **`import:workflow` exige un campo `id`** de 16 caracteres en el JSON. Sin él:
  `SQLITE_CONSTRAINT: NOT NULL constraint failed: workflow_entity.id`.
- En n8n 2.x, "activar" un workflow se llama **publicar** (`n8n publish:workflow`).
