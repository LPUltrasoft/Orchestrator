#!/usr/bin/env bash
# Pone el client ID propio de Google en el remoto de backups de rclone y lo vuelve a
# autorizar. El client_id compartido de rclone deja de funcionar durante 2026.
#
# Uso: scripts/drive-client-id.sh [client_secret_….json]
#   Sin argumento, toma el client_secret_*.json más nuevo de ~/Descargas o ~/Downloads
#   (el que se descarga al crear el ID de cliente en Google Cloud).
#
# No muestra el secreto ni el token: la salida de `rclone config` los incluye.
set -euo pipefail

REMOTE="${DRIVE_REMOTE:-drive-backups}"
RCLONE="${RCLONE_BIN:-$(command -v rclone || echo "$HOME/.local/bin/rclone")}"
JSON="${1:-$(ls -t "$HOME"/Descargas/client_secret_*.json "$HOME"/Downloads/client_secret_*.json 2>/dev/null | head -1 || true)}"

if [[ -z "$JSON" || ! -f "$JSON" ]]; then
  echo "No encuentro el JSON del ID de cliente (client_secret_….json). Pasalo como argumento." >&2
  exit 1
fi

# "installed" es el formato de las apps de escritorio; "web", por si se eligió otro tipo.
read -r CLIENT_ID CLIENT_SECRET < <(python3 - "$JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
client = data.get("installed") or data.get("web") or {}
if not client.get("client_id") or not client.get("client_secret"):
    sys.exit("el JSON no tiene client_id y client_secret")
print(client["client_id"], client["client_secret"])
PY
)
[[ "$CLIENT_ID" == *.apps.googleusercontent.com ]] || { echo "Ese client_id no parece de Google: $CLIENT_ID" >&2; exit 1; }

echo "Cargo el ID de cliente ${CLIENT_ID%%-*}… en «$REMOTE»."
echo "Se abre el navegador: elegí la cuenta lpalmieri.ultrasoft y permití el acceso."
# config_refresh_token: vuelve a pedir la autorización con el ID nuevo (abre el navegador).
"$RCLONE" config update "$REMOTE" client_id "$CLIENT_ID" client_secret "$CLIENT_SECRET" \
  scope drive.file config_refresh_token true > /dev/null

if "$RCLONE" lsf "$REMOTE:" 2>&1 >/dev/null | grep -q "shared Google Drive client_id"; then
  echo "⚠️ rclone sigue usando el client_id compartido: algo no se guardó." >&2
  exit 1
fi
"$RCLONE" about "$REMOTE:" > /dev/null
echo "✅ Listo: el remoto «$REMOTE» usa tu propio ID de cliente."
echo "Borrá el JSON descargado ($JSON): tiene el secreto del ID de cliente."
