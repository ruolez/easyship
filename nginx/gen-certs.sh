#!/usr/bin/env bash
# Self-signed TLS cert for the LAN app (the WebHID USB scale needs a secure context).
# Usage: gen-certs.sh [server-ip]   (pass the IP explicitly on macOS)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs

IP="${1:-}"
if [ -z "$IP" ]; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
fi
IP="${IP:-127.0.0.1}"

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout certs/easyship.key -out certs/easyship.crt \
    -subj "/CN=easyship" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${IP}"
chmod 600 certs/easyship.key
echo "Wrote nginx/certs/easyship.crt (SAN includes ${IP})"
