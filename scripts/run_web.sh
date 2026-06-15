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

bash scripts/setup_amazon_scraper.sh || echo "Warning: amazon-scraper setup skipped" >&2

IMAGE="short-video-analyzer:latest"
echo "Building ${IMAGE} with host networking for proxy access..."
docker build --network host \
  --build-arg HTTP_PROXY \
  --build-arg HTTPS_PROXY \
  --build-arg http_proxy \
  --build-arg https_proxy \
  --build-arg ALL_PROXY \
  --build-arg all_proxy \
  --build-arg NO_PROXY \
  --build-arg no_proxy \
  -t "${IMAGE}" .

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  exec docker compose -p short-video-analyzer up web
fi

if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -p short-video-analyzer up web
fi

echo "Docker Compose is required to run the web UI."
exit 127
