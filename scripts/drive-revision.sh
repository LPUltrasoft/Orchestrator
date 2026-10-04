#!/usr/bin/env bash
# Revisión del espacio del Drive, para correr justo antes de subir un backup. El gateway
# avisa por Telegram si la cuenta llegó al umbral (DRIVE_ALERT_GB). Imprime el resultado
# en JSON y sale con 2 si no hay lugar para un backup del tamaño indicado (más 100 MB).
#
# Uso: scripts/drive-revision.sh [bytes del backup que se va a subir]
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
TOKEN=$(grep '^GATEWAY_TOKEN=' "$DIR/.env" | cut -d= -f2-)
PORT=$(grep '^GATEWAY_PORT=' "$DIR/.env" | cut -d= -f2- || true)
OUT=$(curl -fsS -X POST "http://127.0.0.1:${PORT:-8787}/drive/revision" -H "X-Orc-Token: $TOKEN")
echo "$OUT"
NEED=${1:-0}
FREE=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["libre"])' <<<"$OUT")
if (( FREE < NEED + 100 * 1024 * 1024 )); then
  echo "No hay lugar en el Drive para este backup: liberá espacio antes de subirlo." >&2
  exit 2
fi
