#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

expected_branch="${DEV_BRANCH:-developer}"
current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$expected_branch" && "${ALLOW_NON_DEV_BRANCH:-0}" != "1" ]]; then
  echo "Refusing to deploy the beta service from branch '$current_branch'; expected '$expected_branch'." >&2
  exit 2
fi

project_name="${COMPOSE_PROJECT_NAME:-video_analyzer-dev}"
env_file="${DEV_ENV_FILE:-.env.dev}"
web_port="${WEB_PORT:-4003}"
dev_data_dir="$repo_root/data-dev"
proxy_db="$dev_data_dir/proxy_pool.sqlite"

if [[ ! -f "docker-compose.yml" || ! -f "docker-compose.dev.yml" ]]; then
  echo "Run this script from the repository root." >&2
  exit 2
fi

if [[ ! -d "$dev_data_dir" ]]; then
  echo "Missing development data directory: $dev_data_dir" >&2
  echo "Refusing to create a new empty data directory during deployment." >&2
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

existing_container_id="$(
  docker ps -aq \
    --filter "label=com.docker.compose.project=$project_name" \
    --filter "label=com.docker.compose.service=web" |
    head -n 1
)"
if [[ -n "$existing_container_id" ]]; then
  existing_mounted_data="$(
    docker inspect "$existing_container_id" \
      --format '{{range .Mounts}}{{if eq .Destination "/workspace/data"}}{{.Source}}{{end}}{{end}}'
  )"
  if [[ -n "$existing_mounted_data" && "$(readlink -f "$existing_mounted_data")" != "$(readlink -f "$dev_data_dir")" ]]; then
    echo "Refusing to switch the existing beta service from '$existing_mounted_data' to '$dev_data_dir'." >&2
    echo "Run this deployment from the checkout already mounted by the beta service." >&2
    exit 2
  fi
fi

proxy_counts() {
  python3 - "$proxy_db" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
tables = ("proxy_profiles", "tiktok_accounts", "collect_jobs", "publish_jobs")
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
except sqlite3.Error:
    print("0 0 0 0")
    raise SystemExit
try:
    counts = []
    for table in tables:
        try:
            counts.append(int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
        except sqlite3.Error:
            counts.append(0)
    print(" ".join(str(value) for value in counts))
finally:
    conn.close()
PY
}

read -r before_pools before_accounts before_collect before_publish < <(proxy_counts)
echo "Development Proxy data before deploy: pools=$before_pools accounts=$before_accounts collect_jobs=$before_collect publish_jobs=$before_publish"

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

if [[ -f "$proxy_db" ]]; then
  backup_name="proxy_pool.sqlite.bak-deploy-$(date +%Y%m%d%H%M%S)"
  docker run --rm --network none \
    -v "$dev_data_dir:/data" \
    --entrypoint python \
    short-video-analyzer-dev:latest - "$backup_name" <<'PY'
import sqlite3
import sys
from pathlib import Path

source = Path("/data/proxy_pool.sqlite")
backup_dir = Path("/data/backups")
backup_dir.mkdir(parents=True, exist_ok=True)
destination = backup_dir / sys.argv[1]
with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
print(f"Development Proxy backup created: {destination}")
PY
fi

echo "Replacing only the beta web container..."
if (( legacy_compose )); then
  echo "Legacy Docker Compose detected; removing the old web container to avoid the Docker 29 recreate bug."
  "${compose[@]}" "${compose_args[@]}" rm -s -f web
fi
"${compose[@]}" "${compose_args[@]}" up -d --no-deps --no-build web

health_url="${HEALTHCHECK_URL:-http://127.0.0.1:${web_port}/healthz}"
echo "Waiting for ${health_url}..."
healthy=0
for attempt in $(seq 1 45); do
  if curl --fail --silent --max-time 3 "$health_url" >/dev/null; then
    echo "Beta web service is healthy."
    healthy=1
    break
  fi
  if (( attempt < 45 )); then
    sleep 2
  fi
done

if (( ! healthy )); then
  echo "Beta web service did not become healthy within 90 seconds." >&2
  "${compose[@]}" "${compose_args[@]}" ps web >&2 || true
  exit 1
fi

container_id="$("${compose[@]}" "${compose_args[@]}" ps -q web)"
mounted_data="$(docker inspect "$container_id" --format '{{range .Mounts}}{{if eq .Destination "/workspace/data"}}{{.Source}}{{end}}{{end}}')"
if [[ "$(readlink -f "$mounted_data")" != "$(readlink -f "$dev_data_dir")" ]]; then
  echo "Beta web data mount mismatch: expected '$dev_data_dir', got '$mounted_data'." >&2
  exit 1
fi

proxy_state_url="${PROXY_STATE_URL:-http://127.0.0.1:${web_port}/api/proxy/pools}"
proxy_state="$(curl --fail --silent --max-time 8 "$proxy_state_url")"
read -r api_pools api_accounts < <(
  python3 -c 'import json,sys; data=json.load(sys.stdin); print(len(data.get("pools") or []), len(data.get("accounts") or []))' <<<"$proxy_state"
)
echo "Development Proxy API after deploy: pools=$api_pools accounts=$api_accounts"
if (( before_pools > 0 && api_pools == 0 )); then
  echo "Proxy pool data disappeared from the beta API after deploy; backup retained in $dev_data_dir/backups." >&2
  exit 1
fi
if (( before_accounts > 0 && api_accounts == 0 )); then
  echo "Proxy account data disappeared from the beta API after deploy; backup retained in $dev_data_dir/backups." >&2
  exit 1
fi
