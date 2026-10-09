#!/usr/bin/env bash
# gen-pki-03.sh -- lab PKI for scenario 03. Run on the lab VM with sudo.
# CA and all originals stay in $PKI (outside git, never mounted in the broker).
set -euo pipefail

PKI=/srv/lab-mosquitto/pki
CONF=/srv/docker-conf/lab-mosquitto/conf/03-tls-mutual/certs
DAYS=365
MOSQ_UID=1883     # broker user (see .env)
APP_UID=10001     # cam-api user (see .env)

install -d -m 700 "$PKI"
install -d "$CONF"
cd "$PKI"

# --- CA ---
openssl req -x509 -new -noenc \
  -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout ca.key -out ca.crt -days 730 \
  -subj "/O=FootholdLab/CN=mqlab-ca"
chmod 600 ca.key

# issue <cn> <serverAuth|clientAuth> [subjectAltName]
issue() {
  local cn=$1 eku=$2 san=${3:-}
  openssl req -new -noenc \
    -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$cn.key" -out "$cn.csr" -subj "/O=FootholdLab/CN=$cn"
  {
    echo "basicConstraints=critical,CA:FALSE"
    echo "keyUsage=critical,digitalSignature"
    echo "extendedKeyUsage=$eku"
    if [[ -n $san ]]; then echo "subjectAltName=$san"; fi
  } > "$cn.ext"
  openssl x509 -req -in "$cn.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "$DAYS" -sha256 -extfile "$cn.ext" -out "$cn.crt"
  rm -f "$cn.csr" "$cn.ext"
  chmod 600 "$cn.key"
}

# --- Server: SANs cover every name/IP clients use to reach it ---
#   mosquitto      -> cam-api and HA (Docker DNS)
#   192.168.183.10 -> client
#   127.0.0.1      -> healthcheck inside the container
issue server serverAuth "DNS:broker,DNS:mosquitto,IP:192.168.183.10,IP:127.0.0.1"

# --- Clients: CN becomes the MQTT username ---
issue frontdoor  clientAuth
issue svc_ha     clientAuth
issue svc_health clientAuth
issue client     clientAuth     # attacker: valid cert from the same CA

# --- Broker: what it needs, owned by the broker user ---
install -o "$MOSQ_UID" -g "$MOSQ_UID" -m 644 ca.crt server.crt svc_health.crt "$CONF"/
install -o "$MOSQ_UID" -g "$MOSQ_UID" -m 600 server.key svc_health.key "$CONF"/

# --- cam-api: bind-mounted from here, owned by the cam-api user ---
install -d -o "$APP_UID" -g "$APP_UID" -m 750 clients/frontdoor
install -o "$APP_UID" -g "$APP_UID" -m 644 ca.crt frontdoor.crt clients/frontdoor/
install -o "$APP_UID" -g "$APP_UID" -m 600 frontdoor.key clients/frontdoor/

# Verify the chain
openssl verify -CAfile ca.crt server.crt frontdoor.crt svc_ha.crt svc_health.crt client.crt
