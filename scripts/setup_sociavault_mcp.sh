#!/usr/bin/env bash
# Run on the Docker host; pass the environment's mounted data directory.
# 4003: bash scripts/setup_sociavault_mcp.sh data-dev
set -euo pipefail
data_dir="${1:-data}"
npm install --prefix "$data_dir/sociavault-mcp" --omit=dev --ignore-scripts --no-audit --no-fund sociavault-mcp@2.0.0
