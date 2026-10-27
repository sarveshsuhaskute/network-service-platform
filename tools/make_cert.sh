#!/usr/bin/env bash
# Create a self-signed certificate for the file service (valid for localhost and 127.0.0.1).
# Usage: tools/make_cert.sh [outdir]   ->  outdir/server.crt, outdir/server.key  (default: certs/)
set -euo pipefail
out=${1:-certs}
mkdir -p "$out"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 365 \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -keyout "$out/server.key" -out "$out/server.crt" 2>/dev/null
chmod 600 "$out/server.key"
echo "wrote $out/server.crt and $out/server.key"
