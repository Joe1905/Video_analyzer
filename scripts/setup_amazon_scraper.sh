#!/usr/bin/env bash
set -euo pipefail

image_name="${AMAZON_SCRAPER_IMAGE:-amazon-scraper}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required to install ${image_name}." >&2
  exit 127
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker Engine is not reachable. Start Docker before installing ${image_name}." >&2
  exit 1
fi

if docker image inspect "${image_name}:latest" >/dev/null 2>&1 || docker image inspect "${image_name}" >/dev/null 2>&1; then
  echo "${image_name} image already exists."
  exit 0
fi

skill_dir="${OPENCLAW_HOME:-$HOME/.openclaw}/workspace/skills/amazon-scraper"

if [ ! -d "${skill_dir}" ] && ! command -v openclaw >/dev/null 2>&1; then
  cat >&2 <<MSG
${image_name} image is missing, and openclaw is not installed on this server.
Install OpenClaw CLI or build the ClawHub skill manually, then rerun:
  openclaw skills install amazon-scraper
MSG
  exit 127
fi

if [ ! -d "${skill_dir}" ]; then
  echo "Installing ClawHub skill: amazon-scraper"
  openclaw skills install amazon-scraper
else
  echo "ClawHub skill already exists at ${skill_dir}."
fi

if ! docker image inspect "${image_name}:latest" >/dev/null 2>&1 && ! docker image inspect "${image_name}" >/dev/null 2>&1; then
  if [ ! -f "${skill_dir}/Dockerfile.sh" ]; then
    echo "Missing ${skill_dir}/Dockerfile.sh; cannot build ${image_name}." >&2
    exit 1
  fi
  sed -i 's/"playwright": "[^"]*"/"playwright": "1.52.0"/' "${skill_dir}/package.json"
  cp "${skill_dir}/Dockerfile.sh" "${skill_dir}/Dockerfile"
  sed -i '/^WORKDIR /a ENV HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= ALL_PROXY= all_proxy= npm_config_proxy= npm_config_https_proxy= PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright\nRUN npm config delete proxy || true\nRUN npm config delete https-proxy || true\nRUN npm config set registry https://registry.npmmirror.com' "${skill_dir}/Dockerfile"
  sed -i 's|^RUN npx playwright install chromium|RUN npx playwright --version|' "${skill_dir}/Dockerfile"
  base_image="${AMAZON_SCRAPER_BASE_IMAGE:-}"
  if [ -z "${base_image}" ] && ! docker image inspect mcr.microsoft.com/playwright:v1.40.0-jammy >/dev/null 2>&1; then
    base_image="$(docker images mcr.microsoft.com/playwright --format '{{.Repository}}:{{.Tag}}' | head -n 1 || true)"
  fi
  if [ -n "${base_image}" ]; then
    echo "Using Playwright base image: ${base_image}"
    sed -i "1s|^FROM .*|FROM ${base_image}|" "${skill_dir}/Dockerfile"
  fi
  echo "Building ${image_name} image from ${skill_dir}"
  docker build -t "${image_name}" "${skill_dir}"
  mkdir -p "$HOME/scrapes"
fi

if docker image inspect "${image_name}:latest" >/dev/null 2>&1 || docker image inspect "${image_name}" >/dev/null 2>&1; then
  echo "${image_name} image installed."
  exit 0
fi

echo "openclaw install completed, but ${image_name} image was not found. Expected setup script: ${skill_dir}/scripts/setup.sh" >&2
exit 1
