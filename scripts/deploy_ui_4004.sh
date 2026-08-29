#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

legacy_preview="${UI4004_LEGACY_PREVIEW:-0}"
if [[ "$legacy_preview" != "0" ]]; then
  echo "Legacy 4004 preview deployment is disabled; use the canonical v2 environment." >&2
  exit 2
fi

env_file="${UI4004_ENV_FILE:-.env}"
expected_image="short-video-analyzer-ui-4004:latest"
image_name="${ANALYZER_IMAGE:-$expected_image}"
if [[ "$image_name" != "$expected_image" ]]; then
  echo "Refusing non-4004 analyzer image: $image_name" >&2
  exit 2
fi
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "v2" ]]; then
  echo "Refusing 4004 deployment from '$current_branch'; expected 'v2'." >&2
  exit 2
fi

project_name="${COMPOSE_PROJECT_NAME:-short-video-analyzer-ui-4004}"
web_port="${WEB_PORT:-4004}"
if [[ "$project_name" != "short-video-analyzer-ui-4004" ]]; then
  echo "Refusing non-4004 Compose project: $project_name" >&2
  exit 2
fi
if [[ "$web_port" != "4004" ]]; then
  echo "Refusing non-4004 web port: $web_port" >&2
  exit 2
fi

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file for the v2 deployment." >&2
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

mkdir -p data videos output data/sing-box data/mcp_tool_cache_shared
compose_args=(-p "$project_name" --env-file "$env_file" -f docker-compose.yml)

echo "Building $image_name for isolated 4004 v2..."
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
  ANALYZER_IMAGE="$image_name" WEB_PORT="$web_port" "${compose[@]}" "${compose_args[@]}" rm -s -f web >/dev/null 2>&1 || true
fi
ANALYZER_IMAGE="$image_name" WEB_PORT="$web_port" "${compose[@]}" "${compose_args[@]}" up -d --no-deps --no-build web

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
