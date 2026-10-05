#!/usr/bin/env bash
# Abre Jenkins a la red local por HTTPS (puerto 8443), solo desde la IP o la red que
# digas. El acceso local por HTTP (127.0.0.1:8090, el que usa el gateway) no cambia.
# El certificado es propio: la primera vez el navegador avisa y hay que aceptarlo.
#
# Uso: sudo scripts/jenkins-red-local.sh 192.168.1.50        (solo esa PC)
#      sudo scripts/jenkins-red-local.sh 192.168.1.0/24      (toda la red)
#      sudo scripts/jenkins-red-local.sh --cerrar            (vuelve a dejarlo solo local)
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Correlo con sudo: sudo $0 <IP o red>" >&2; exit 1; }
ALLOW="${1:?Indicá la IP o la red que puede entrar (por ejemplo 192.168.1.0/24), o --cerrar}"
CI=orc-ci
PORT=8443
ENV_FILE="${JENKINS_ENV_FILE:-/etc/orchestrator/jenkins.env}"
KEYSTORE="${JENKINS_KEYSTORE:-/home/$CI/jenkins/https.p12}"
HTTPS_ARGS=' --httpsPort=[0-9]* --httpsListenAddress=[^ "]* --httpsKeyStore=[^ "]* --httpsKeyStorePassword=[^ "]*'

quitar_https() {  # saca de la línea de comando de Jenkins lo que agregó este script
  sed -i "s/$HTTPS_ARGS//" "$ENV_FILE"
}

if [[ "$ALLOW" == "--cerrar" ]]; then
  quitar_https
  while rule=$(ufw status numbered | grep -m1 " $PORT/tcp" | sed -E 's/^\[ *([0-9]+)\].*/\1/'); [[ -n "$rule" ]]; do
    ufw --force delete "$rule" >/dev/null
  done
  rm -f "$KEYSTORE"
  systemctl restart jenkins
  echo "✅ Jenkins vuelve a escuchar solo en 127.0.0.1."
  exit 0
fi

[[ "$ALLOW" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] || { echo "IP o red inválida: $ALLOW" >&2; exit 1; }
IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}')

echo "▶ Certificado propio para $IP (y $(hostname))"
PASS=$(openssl rand -hex 16)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes -keyout "$tmp/key.pem" -out "$tmp/cert.pem" \
  -subj "/CN=Jenkins Orchestrator" -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:$(hostname)" 2>/dev/null
openssl pkcs12 -export -in "$tmp/cert.pem" -inkey "$tmp/key.pem" -out "$KEYSTORE" -passout "pass:$PASS" -name jenkins
chown "$CI:$CI" "$KEYSTORE"; chmod 600 "$KEYSTORE"

echo "▶ Jenkins: HTTPS en el puerto $PORT (el HTTP local de 127.0.0.1:8090 queda igual)"
# En todas las interfaces, para no depender de que la IP de la PC cambie: el que filtra
# quién entra es el firewall.
quitar_https
sed -i "s|--httpListenAddress=127.0.0.1\"|--httpListenAddress=127.0.0.1 --httpsPort=$PORT --httpsListenAddress=0.0.0.0 --httpsKeyStore=$KEYSTORE --httpsKeyStorePassword=$PASS\"|" "$ENV_FILE"
grep -q -- "--httpsPort=$PORT" "$ENV_FILE" || { echo "No pude agregar HTTPS a $ENV_FILE" >&2; exit 1; }
chmod 600 "$ENV_FILE"   # tiene la contraseña del certificado: lo lee systemd, como root

echo "▶ Firewall: el puerto $PORT, solo desde $ALLOW"
ufw allow from "$ALLOW" to any port "$PORT" proto tcp comment "Jenkins Orchestrator (HTTPS, red local)" >/dev/null

systemctl restart jenkins
for _ in $(seq 1 60); do curl -sk -o /dev/null "https://127.0.0.1:$PORT/login" && break; sleep 2; done
FINGERPRINT=$(openssl x509 -in "$tmp/cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)
echo
echo "✅ Listo. Desde $ALLOW: https://$IP:$PORT"
echo "   La primera vez el navegador avisa que el certificado no es de confianza: es el propio."
echo "   Para comprobar que es este, su huella SHA-256 es:"
echo "   $FINGERPRINT"
