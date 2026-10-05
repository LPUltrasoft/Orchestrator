#!/usr/bin/env bash
# Abre el ambiente de desarrollo de todos los proyectos (web, API y bases de datos) a las
# IPs (o redes) que digas. Los compose de desarrollo publican en ${ORC_DEV_BIND:-127.0.0.1}:
# este script define ORC_DEV_BIND=0.0.0.0 en /etc/orchestrator/desarrollo.env (lo leen
# Jenkins y scripts/autenticacion.sh), así cada despliegue escucha en todas las interfaces,
# y el firewall abre el rango de desarrollo (18100-18499) solo para esas IPs. Producción
# (19100-19499) no se toca. Los ambientes ya desplegados toman el cambio en el próximo
# despliegue de develop.
#
# Uso: sudo scripts/desarrollo-red-local.sh 192.168.1.117 192.168.0.200   (reemplaza las anteriores)
#      sudo scripts/desarrollo-red-local.sh --cerrar                       (vuelve a dejarlo solo local)
set -euo pipefail
export LC_ALL=C   # las salidas de ufw que se leen abajo, sin traducir

[[ $EUID -eq 0 ]] || { echo "Correlo con sudo: sudo $0 <IP> [<IP>...]" >&2; exit 1; }
[[ $# -ge 1 ]] || { echo "Indicá las IPs o redes que pueden entrar (por ejemplo 192.168.1.117), o --cerrar" >&2; exit 1; }
PORTS="${ORC_DEV_PORTS:-18100:18499}"   # PORTS_DEV_BASE hasta + PORTS_PER_ENV × PORTS_MAX_PROJECTS - 1
ENV_FILE="${JENKINS_ENV_FILE:-/etc/orchestrator/jenkins.env}"
DEV_ENV="${ORC_DESARROLLO_ENV:-/etc/orchestrator/desarrollo.env}"
DROPIN=/etc/systemd/system/jenkins.service.d/orchestrator.conf

borrar_reglas() {  # las reglas de este script: todas las del rango de desarrollo
  while rule=$(ufw status numbered | grep -m1 " $PORTS/tcp" | sed -E 's/^\[ *([0-9]+)\].*/\1/'); [[ -n "$rule" ]]; do
    ufw --force delete "$rule" >/dev/null
  done
}

reiniciar_jenkins() {
  # Jenkins lee desarrollo.env aparte de jenkins.env: setup-orc-ci.sh reescribe ese, no este.
  if ! grep -qF "EnvironmentFile=-$DEV_ENV" "$DROPIN"; then
    echo "EnvironmentFile=-$DEV_ENV" >> "$DROPIN"
  fi
  sed -i '/^ORC_DEV_BIND=/d' "$ENV_FILE"   # donde la dejaba la versión anterior de este script
  systemctl daemon-reload
  echo "▶ Reinicio Jenkins para que los builds tomen ORC_DEV_BIND (un build en curso se corta)"
  systemctl restart jenkins
  for _ in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:8090/login" && break; sleep 2; done
}

if [[ "$1" == "--cerrar" ]]; then
  borrar_reglas
  rm -f "$DEV_ENV"
  reiniciar_jenkins
  echo "✅ Desarrollo vuelve a ser solo local: el firewall ya no deja entrar, y el próximo"
  echo "   despliegue de cada proyecto vuelve a escuchar solo en 127.0.0.1."
  exit 0
fi

for ip in "$@"; do
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] || { echo "IP o red inválida: $ip" >&2; exit 1; }
done
ufw status verbose | grep -q "deny (incoming)" || {
  echo "⚠️  ufw no está activo o no rechaza lo entrante por defecto: así el filtro por IP no sirve." >&2
  echo "   Revisá «sudo ufw status verbose» antes de seguir." >&2
  exit 1
}

echo "▶ Firewall: puertos de desarrollo $PORTS, solo desde $*"
borrar_reglas
for ip in "$@"; do
  ufw allow from "$ip" to any port "$PORTS" proto tcp comment "Orchestrator: desarrollo en la red local" >/dev/null
done

echo "▶ Jenkins: ORC_DEV_BIND=0.0.0.0 (los compose de desarrollo publican en todas las interfaces)"
# No es un secreto: lo puede leer cualquiera (también orc-ci, para los despliegues a mano).
echo "ORC_DEV_BIND=0.0.0.0" > "$DEV_ENV"
chmod 644 "$DEV_ENV"
reiniciar_jenkins

IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}')
echo
echo "✅ Listo. Desde $*: http://$IP:<puerto de desarrollo del front de cada proyecto>"
echo "   Las bases, en $IP y el puerto de desarrollo que dice el 06 de cada proyecto."
echo "   Los ambientes que ya estaban desplegados lo toman en su próximo despliegue de develop."
ufw status numbered | grep " $PORTS/tcp"
