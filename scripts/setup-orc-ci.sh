#!/usr/bin/env bash
# Prepara el usuario `orc-ci` con Docker SIN root. Ahí corre todo lo que construye o
# ejecuta código escrito por los agentes: Jenkins, los tests de integración y los
# ambientes de desarrollo y producción. Si algo escapara de un contenedor, queda
# encerrado en ese usuario: sin acceso al home del usuario, sus credenciales ni el vault.
#
# Correr UNA vez, desde tu usuario:   sudo ~/Documentos/Orchestrator/scripts/setup-orc-ci.sh
# Se puede volver a correr: lo que ya está hecho, lo saltea.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Correlo con sudo: sudo $0" >&2; exit 1; }
OWNER="${SUDO_USER:?Correlo con sudo desde tu usuario, no como root directo}"
CI=orc-ci
DOCKER_VERSION=29.8.2   # la misma versión del Docker instalado
EXTRAS_URL="https://download.docker.com/linux/static/stable/x86_64/docker-rootless-extras-${DOCKER_VERSION}.tgz"
EXTRAS_SHA256=707ebf6a5afd88104086e7b6749997b2366e816aeaf2c3ef2305b08fde9ee007
GIT_DIR=/srv/orc-git     # espejos de los repos: del usuario, solo lectura para orc-ci
SUBIDS=165536-231071     # a continuación de los del usuario (100000-165535)

paso() { printf '\n▶ %s\n' "$*"; }

paso "Paquetes: slirp4netns (red de los contenedores sin root)"
pacman -S --needed --noconfirm slirp4netns

paso "Usuario $CI"
if ! id "$CI" >/dev/null 2>&1; then
  useradd --create-home --shell /usr/bin/nologin --comment "Orchestrator CI (Docker sin root)" "$CI"
fi
chmod 700 "/home/$CI"
grep -q "^$CI:" /etc/subuid || usermod --add-subuids "$SUBIDS" "$CI"
grep -q "^$CI:" /etc/subgid || usermod --add-subgids "$SUBIDS" "$CI"
# Que sus servicios corran sin que nadie inicie sesión con ese usuario.
loginctl enable-linger "$CI"

paso "Docker sin root ${DOCKER_VERSION} (scripts oficiales de Docker, verificados por SHA256)"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$EXTRAS_URL" -o "$tmp/extras.tgz"
echo "$EXTRAS_SHA256  $tmp/extras.tgz" | sha256sum -c -
tar -xzf "$tmp/extras.tgz" -C "$tmp"
install -m 755 "$tmp/docker-rootless-extras/dockerd-rootless.sh" \
               "$tmp/docker-rootless-extras/dockerd-rootless-setuptool.sh" \
               "$tmp/docker-rootless-extras/rootlesskit" /usr/local/bin/

paso "orc-ci-docker: docker contra el demonio de $CI"
cat > /usr/local/bin/orc-ci-docker <<'SH'
#!/bin/sh
# docker contra el demonio sin root de orc-ci. Uso: sudo -u orc-ci orc-ci-docker ps
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
exec /usr/bin/docker "$@"
SH
chmod 755 /usr/local/bin/orc-ci-docker

paso "Servicio de Docker de $CI (systemd del usuario)"
unit_dir="/home/$CI/.config/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/docker.service" <<'UNIT'
[Unit]
Description=Docker sin root de orc-ci (Orchestrator)

[Service]
Environment=PATH=/usr/local/bin:/usr/bin:/bin
# Red y puertos con slirp4netns: más lento que el predeterminado, pero conserva la IP
# de origen (el mapa de tráfico y la IP real que manda el nginx externo la necesitan).
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns
ExecStart=/usr/local/bin/dockerd-rootless.sh
ExecReload=/bin/kill -s HUP $MAINPID
Type=notify
NotifyAccess=all
Delegate=yes
KillMode=mixed
Restart=always
RestartSec=2
TimeoutSec=0
LimitNOFILE=infinity
LimitNPROC=infinity
TasksMax=infinity

[Install]
WantedBy=default.target
UNIT
chown -R "$CI:$CI" "/home/$CI/.config"
# El administrador de servicios del usuario puede tardar un momento en arrancar.
for _ in $(seq 1 20); do systemctl --user --machine="$CI@.host" is-system-running >/dev/null 2>&1 && break; sleep 0.5; done
systemctl --user --machine="$CI@.host" daemon-reload
systemctl --user --machine="$CI@.host" enable --now docker.service

paso "Espejos de los repos en $GIT_DIR (de $OWNER, solo lectura para $CI)"
mkdir -p "$GIT_DIR"
chown "$OWNER:$CI" "$GIT_DIR"
chmod 2750 "$GIT_DIR"   # setgid: lo que se cree adentro hereda el grupo $CI

paso "sudo: $OWNER puede actuar como $CI (no al revés)"
rule="/etc/sudoers.d/orchestrator-ci"
echo "$OWNER ALL=($CI) NOPASSWD: ALL" > "$rule.tmp"
chmod 440 "$rule.tmp"
visudo -cf "$rule.tmp" >/dev/null && mv "$rule.tmp" "$rule"

paso "Verificación"
for _ in $(seq 1 30); do sudo -u "$CI" /usr/local/bin/orc-ci-docker info >/dev/null 2>&1 && break; sleep 1; done
sudo -u "$CI" /usr/local/bin/orc-ci-docker info --format '  Docker {{.ServerVersion}} · {{.SecurityOptions}}'
sudo -u "$CI" /usr/local/bin/orc-ci-docker run --rm hello-world | grep -m1 "Hello from Docker"
echo
echo "✅ Listo. $CI tiene Docker sin root; $OWNER lo maneja con: sudo -u $CI orc-ci-docker <comando>"
