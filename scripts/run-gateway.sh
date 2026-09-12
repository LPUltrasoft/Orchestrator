#!/usr/bin/env bash
# Levanta el gateway en primer plano (para desarrollo).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec uv run --project gateway python -m gateway.main
