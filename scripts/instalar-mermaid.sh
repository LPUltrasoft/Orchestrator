#!/usr/bin/env bash
# Instala la herramienta oficial de Mermaid (mmdc) para dibujar diagramas en PNG, por
# ejemplo el DER que escribe el DBA y que el gateway manda por Telegram. Sin sudo: va a
# ~/.local/share/orchestrator/mermaid y usa el Chromium del sistema (no baja otro).
set -euo pipefail
VERSION=12.0.0
DEST="$HOME/.local/share/orchestrator/mermaid"
NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
[[ -x "$NODE_BIN/npm" ]] || { echo "No encuentro npm (Node de nvm)." >&2; exit 1; }
CHROMIUM=$(command -v chromium || command -v google-chrome-stable || true)
[[ -n "$CHROMIUM" ]] || { echo "Falta Chromium (sudo pacman -S chromium)." >&2; exit 1; }
mkdir -p "$DEST"
cd "$DEST"
[[ -f package.json ]] || echo '{"name": "orchestrator-mermaid", "private": true}' > package.json
PUPPETEER_SKIP_DOWNLOAD=1 PATH="$NODE_BIN:$PATH" npm install --no-fund --no-audit "@mermaid-js/mermaid-cli@$VERSION"
cat > puppeteer.json <<JSON
{"executablePath": "$CHROMIUM", "headless": "shell"}
JSON
echo "✅ mmdc $VERSION en $DEST (Node: $NODE_BIN, Chromium: $CHROMIUM)"
