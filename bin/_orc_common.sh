# Funciones comunes de los comandos orc-*. Se incluye con `source`, no se ejecuta.

# Solo el orquestador usa estos comandos: si delega un sub-agente, dispara otro
# sub-agente. El gateway marca quién invoca cada agy en ORC_CALLER.
orc_require_orchestrator() {
  if [[ -n "${ORC_CALLER:-}" && "$ORC_CALLER" != "orchestrator" ]]; then
    echo "{\"status\":\"ERROR\",\"error\":\"$1 es solo para el orquestador; vos sos el agente ${ORC_CALLER}. Hacé tu tarea con tus herramientas de archivos.\"}" >&2
    exit 77
  fi
  if [[ -z "${ORC_TOKEN:-}" ]]; then
    echo '{"status":"ERROR","error":"ORC_TOKEN no está en el entorno"}' >&2
    exit 78
  fi
  ORC_GATEWAY="${ORC_GATEWAY_URL:-http://127.0.0.1:8787}"
}

# JSON a partir de pares clave=valor, sin problemas de comillas ni acentos.
orc_json() {
  python3 -c '
import json, sys
pairs = (arg.split("=", 1) for arg in sys.argv[1:])
print(json.dumps({k: int(v) if v.isdigit() and k == "rounds" else v for k, v in pairs}, ensure_ascii=False))
' "$@"
}

orc_post() {  # orc_post <ruta> <json>
  curl -sS -X POST "$ORC_GATEWAY$1" \
    -H "Content-Type: application/json" \
    -H "X-Orc-Token: $ORC_TOKEN" \
    -H "X-Orc-Job: ${ORC_JOB_ID:-}" \
    --data "$2"
  echo
}

orc_get() {  # orc_get <ruta>
  curl -sS "$ORC_GATEWAY$1" -H "X-Orc-Token: $ORC_TOKEN"
  echo
}
