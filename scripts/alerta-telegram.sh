#!/usr/bin/env bash
# Aviso por Telegram sin pasar por el gateway: lo usa systemd cuando el gateway se cae
# y no logra volver a levantar (OnFailure=), justo cuando el gateway no puede avisar.
# Toma el token del bot y los chats del .env; nunca los imprime.
#
# Uso: scripts/alerta-telegram.sh "<texto>"
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$DIR/.env" | cut -d= -f2-)
CHATS=$(grep '^ALLOWED_CHAT_IDS=' "$DIR/.env" | cut -d= -f2-)
API="${TELEGRAM_API_BASE:-https://api.telegram.org}"
TEXT="${1:-⚠️ El gateway del Orchestrator se cayó y no pudo volver a levantar.}"
if [[ -z "$TOKEN" || -z "$CHATS" ]]; then
  echo "falta TELEGRAM_BOT_TOKEN o ALLOWED_CHAT_IDS en el .env" >&2
  exit 1
fi
IFS=',' read -ra IDS <<< "$CHATS"
for id in "${IDS[@]}"; do
  BODY=$(python3 -c 'import json, sys; print(json.dumps({"chat_id": sys.argv[1], "text": sys.argv[2]}))' "${id// /}" "$TEXT")
  curl -fsS -o /dev/null --max-time 20 -H "Content-Type: application/json" -d "$BODY" "$API/bot$TOKEN/sendMessage"
done
