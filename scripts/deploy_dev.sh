#!/usr/bin/env bash
set -euo pipefail

project_name="${COMPOSE_PROJECT_NAME:-short-video-analyzer-dev}"
env_file="${DEV_ENV_FILE:-.env.dev}"
web_port="${WEB_PORT:-4003}"

if [[ ! -f "docker-compose.yml" || ! -f "docker-compose.dev.yml" ]]; then
  echo "Run this script from the repository root." >&2
  exit 2
fi

if [[ ! -f "$env_file" ]]; then
  echo "Missing development environment file: $env_file" >&2
  exit 2
fi

configured_port="$(sed -n 's/^WEB_PORT=//p' "$env_file" | tail -n 1 | tr -d '\r\"' | xargs)"
if [[ -n "$configured_port" ]]; then
  web_port="$configured_port"
fi
if [[ ! "$web_port" =~ ^[0-9]+$ ]] || (( web_port < 1 || web_port > 65535 )); then
  echo "Invalid WEB_PORT: $web_port" >&2
  exit 2
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
  legacy_compose=0
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
  legacy_compose=1
else
  echo "Docker Compose is required." >&2
  exit 127
fi

compose_args=(
  -p "$project_name"
  --env-file "$env_file"
  -f docker-compose.yml
  -f docker-compose.dev.yml
)

if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  echo "Skipping the beta image build; using the existing image with mounted scripts."
else
  echo "Building beta web image while the current container stays online..."
  docker build --network host \
    --build-arg HTTP_PROXY \
    --build-arg HTTPS_PROXY \
    --build-arg http_proxy \
    --build-arg https_proxy \
    --build-arg ALL_PROXY \
    --build-arg all_proxy \
    --build-arg NO_PROXY \
    --build-arg no_proxy \
    -t short-video-analyzer-dev:latest .
fi

echo "Replacing only the beta web container..."
if (( legacy_compose )); then
  echo "Legacy Docker Compose detected; removing the old web container to avoid the Docker 29 recreate bug."
  "${compose[@]}" "${compose_args[@]}" rm -s -f web
fi
"${compose[@]}" "${compose_args[@]}" up -d --no-deps --no-build web

health_url="${HEALTHCHECK_URL:-http://127.0.0.1:${web_port}/healthz}"
echo "Waiting for ${health_url}..."
for attempt in $(seq 1 45); do
  if curl --fail --silent --max-time 3 "$health_url" >/dev/null; then
    echo "Beta web service is healthy."
    exit 0
  fi
  if (( attempt < 45 )); then
    sleep 2
  fi
done

echo "Beta web service did not become healthy within 90 seconds." >&2
"${compose[@]}" "${compose_args[@]}" ps web >&2 || true
exit 1
