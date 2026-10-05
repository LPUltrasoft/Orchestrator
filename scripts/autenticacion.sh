#!/usr/bin/env bash
# Levanta o actualiza el servicio de autenticación compartido de los proyectos (desarrollo),
# en el Docker de orc-ci, desde el repo de autenticación del usuario. Sin sudo de root: usa
# la regla «lpalmieri puede actuar como orc-ci». Producción llega con la etapa 3b.
#
# Uso: scripts/autenticacion.sh [rama o commit]    (por defecto, develop)
#
# Después, cada push a develop lo despliega Jenkins (job plataforma/autenticacion): este
# script es para la primera vez, o para desplegar otra rama a mano.
#
# La base se crea la primera vez solo con la estructura (sin los datos de Synergia ni de
# Grandes Pasos) más las migraciones de seguridad. Los secretos se generan al azar en
# /home/orc-ci/plataforma/autenticacion-dev/secretos.env, que solo lee orc-ci.
set -euo pipefail

REF="${1:-${AUTH_REF:-develop}}"
REPO_URL="${AUTH_REPO:-https://github.com/LucianoPal/Autenticacion.git}"
AQUI="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${XDG_DATA_HOME:-$HOME/.local/share}/orchestrator/plataforma/autenticacion"
DESTINO=/home/orc-ci/plataforma/autenticacion-dev
RED=orc-autenticacion-dev
como_ci() { sudo -n -u orc-ci "$@"; }
docker_ci() { como_ci orc-ci-docker "$@"; }
paso() { echo "▶ $*"; }

paso "Código: $REF"
if [[ -d "$SRC/.git" ]]; then
  git -C "$SRC" fetch -q --prune origin
else
  mkdir -p "$(dirname "$SRC")"
  git clone -q "$REPO_URL" "$SRC"
fi
git -C "$SRC" checkout -q --detach "origin/$REF" 2>/dev/null || git -C "$SRC" checkout -q --detach "$REF"
TAG=$(git -C "$SRC" rev-parse --short=12 HEAD)   # como el Jenkinsfile

paso "Imagen orc-autenticacion:$TAG"
git -C "$SRC" archive --format=tar HEAD | docker_ci build -q -t "orc-autenticacion:$TAG" - >/dev/null

paso "Archivos en $DESTINO"
como_ci mkdir -p "$DESTINO/initdb"
como_ci tee "$DESTINO/compose.yml" < "$AQUI/plataforma/autenticacion/compose.dev.yml" >/dev/null
# Solo la estructura: sin los INSERT de datos ni los valores de las secuencias.
grep -vE '^(INSERT INTO|SELECT pg_catalog\.setval)' "$SRC/init-db/backup.sql" | como_ci tee "$DESTINO/initdb/10-estructura.sql" >/dev/null
# La 02 no: asigna permisos a los roles admin de Synergia y Grandes Pasos (1 y 4).
cat "$SRC"/_migrations/seguridad_01_*.sql "$SRC"/_migrations/seguridad_03_*.sql "$SRC"/_migrations/seguridad_04_*.sql \
  | como_ci tee "$DESTINO/initdb/20-seguridad.sql" >/dev/null
como_ci tee "$DESTINO/initdb/90-plataforma.sh" < "$AQUI/plataforma/autenticacion/90-plataforma.sh" >/dev/null
como_ci chmod 755 "$DESTINO/initdb/90-plataforma.sh"
echo "AUTH_TAG=$TAG" | como_ci tee "$DESTINO/version.env" >/dev/null
if ! como_ci test -f "$DESTINO/secretos.env"; then
  # Se generan en el shell de orc-ci: los valores no pasan por la línea de comandos.
  como_ci sh -c 'umask 077; printf "POSTGRES_PASSWORD=%s\nAUTH_DB_APP_PASSWORD=%s\nJWT_SECRET=%s\n" \
    "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" "$(openssl rand -hex 32)" > "$1"' _ "$DESTINO/secretos.env"
  echo "  secretos nuevos generados"
fi

paso "Red compartida $RED"
docker_ci network inspect "$RED" >/dev/null 2>&1 || docker_ci network create "$RED" >/dev/null

paso "Levantando"
docker_ci compose -f "$DESTINO/compose.yml" --env-file "$DESTINO/secretos.env" --env-file "$DESTINO/version.env" \
  up -d --wait --remove-orphans

curl -fsS http://127.0.0.1:18490/health >/dev/null

# El job que lo despliega con cada push a develop (solo develop y master, como todos). La
# credencial la carga el usuario en Jenkins: un token de LucianoPal con lectura del repo.
paso "Jenkins: job plataforma/autenticacion"
(cd "$AQUI/gateway" && set -a && . ../.env && set +a && .venv/bin/python - "${AUTH_JENKINS_CREDENTIAL:-github LucianoPal}" <<'EOF'
import sys
sys.path.insert(0, "..")
from gateway import jenkins
if jenkins.configured():
    jenkins.ensure_jobs("Plataforma", {"autenticacion": {"github": "LucianoPal/Autenticacion"}}, credentials=sys.argv[1])
    print("  listo" if jenkins.scan_and_wait("Plataforma", "autenticacion") is None else "  ⚠️ Jenkins no puede leer el repo: revisar la credencial")
EOF
)
echo
echo "✅ Autenticación de desarrollo en orc-autenticacion:$TAG"
echo "   PC: http://127.0.0.1:18490 · proyectos: http://autenticacion:3001 (red $RED)"
