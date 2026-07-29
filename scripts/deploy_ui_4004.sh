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

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 127
fi

mkdir -p data-dev videos-dev output-dev data-dev/sing-box data-dev/mcp_tool_cache_shared

compose=(docker compose -p "$project_name" --env-file "$env_file" -f docker-compose.yml -f docker-compose.ui-4004.yml)
echo "Building $image_name for isolated 4004 preview..."
docker build --network host -t "$image_name" .

echo "Starting only the isolated 4004 web service..."
"${compose[@]}" up -d --no-deps --no-build web

health_url="http://127.0.0.1:${web_port}/healthz"
for attempt in $(seq 1 45); do
  if curl --fail --silent --max-time 3 "$health_url" >/dev/null; then
    echo "UI 4004 is healthy: $health_url"
    exit 0
  fi
  sleep 2
done

echo "UI 4004 did not become healthy." >&2
"${compose[@]}" ps web >&2 || true
exit 1
