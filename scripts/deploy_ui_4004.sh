#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

expected_branch="${UI4004_BRANCH:-codex/ui-beautification-4004}"
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$expected_branch" && "${ALLOW_NON_UI4004_BRANCH:-0}" != "1" ]]; then
  echo "Refusing 4004 deployment from '$current_branch'; expected '$expected_branch'." >&2
  exit 2
fi

project_name="${COMPOSE_PROJECT_NAME:-short-video-analyzer-ui-4004}"
env_file="${UI4004_ENV_FILE:-.env.ui-4004}"
image_name="${ANALYZER_IMAGE:-short-video-analyzer-ui-4004:latest}"
web_port="${WEB_PORT:-4004}"

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Copy .env.ui-4004.example and merge only the required API settings from the server's existing .env." >&2
  exit 2
fi
if ! grep -Eq '^SOCIAVAULT_API_KEY=.+$' "$env_file"; then
  echo "Missing non-empty SOCIAVAULT_API_KEY in $env_file." >&2
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

mkdir -p data-dev videos-dev output-dev data-dev/sing-box data-dev/mcp_tool_cache_shared

compose_args=(-p "$project_name" --env-file "$env_file" -f docker-compose.yml -f docker-compose.ui-4004.yml)
echo "Building $image_name for isolated 4004 preview..."
build_proxy="${UI4004_BUILD_PROXY:-http://127.0.0.1:7890}"
docker build --network host \
  --build-arg HTTP_PROXY="$build_proxy" \
  --build-arg HTTPS_PROXY="$build_proxy" \
  --build-arg http_proxy="$build_proxy" \
  --build-arg https_proxy="$build_proxy" \
  --build-arg NO_PROXY="127.0.0.1,localhost" \
  --build-arg no_proxy="127.0.0.1,localhost" \
  -t "$image_name" .

echo "Starting only the isolated 4004 web service..."
if (( legacy_compose )); then
  "${compose[@]}" "${compose_args[@]}" rm -s -f web >/dev/null 2>&1 || true
fi
"${compose[@]}" "${compose_args[@]}" up -d --no-deps --no-build web

health_url="http://127.0.0.1:${web_port}/healthz"
for attempt in $(seq 1 45); do
  if curl --fail --silent --max-time 3 "$health_url" >/dev/null; then
    echo "UI 4004 is healthy: $health_url"
    exit 0
  fi
  sleep 2
done

echo "UI 4004 did not become healthy." >&2
"${compose[@]}" "${compose_args[@]}" ps web >&2 || true
exit 1
