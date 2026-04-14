#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="${SCRIPT_DIR}/../certs"
OPENSSL_CONFIG="${CERTS_DIR}/openssl-san.cnf"
CRT_PATH="${CERTS_DIR}/server.crt"
KEY_PATH="${CERTS_DIR}/server.key"

mkdir -p "${CERTS_DIR}"

DEFAULT_HOSTNAME="$(hostname).local"

read -r -p "Enter IP address for certificate: " CERT_IP
if [[ -z "${CERT_IP}" ]]; then
  echo "IP address is required."
  exit 1
fi

read -r -p "Enter domain [${DEFAULT_HOSTNAME}]: " CERT_DOMAIN
CERT_DOMAIN="${CERT_DOMAIN:-$DEFAULT_HOSTNAME}"

cat > "${OPENSSL_CONFIG}" <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = ${CERT_DOMAIN}

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = ${CERT_DOMAIN}
IP.1 = ${CERT_IP}
EOF

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout "${KEY_PATH}" \
  -out "${CRT_PATH}" \
  -config "${OPENSSL_CONFIG}"

chmod 600 "${KEY_PATH}"

echo
echo "Certificate generated:"
echo "  Cert: ${CRT_PATH}"
echo "  Key : ${KEY_PATH}"
echo
echo "Valid for:"
echo "  https://${CERT_DOMAIN}:8443"
echo "  https://${CERT_IP}:8443"