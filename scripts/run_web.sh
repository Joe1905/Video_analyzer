#!/usr/bin/env bash
set -euo pipefail

start_port="${WEB_PORT:-4000}"
port="$start_port"

is_port_busy() {
  python - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex(("127.0.0.1", port)) == 0 else 1)
PY
}

while is_port_busy "$port"; do
  port=$((port + 1))
done

echo "Starting web UI on http://localhost:${port}"
export WEB_PORT="$port"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  exec docker compose -p short-video-analyzer up --build web
fi

if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -p short-video-analyzer up --build web
fi

echo "Docker Compose is required to run the web UI."
exit 127
