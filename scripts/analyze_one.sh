#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "Usage: bash scripts/analyze_one.sh <video-file-name>"
  echo "Example: bash scripts/analyze_one.sh test.mp4"
  exit 2
fi

if ! command -v video-analyzer >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    exec docker compose run --rm analyzer bash scripts/analyze_one.sh "$@"
  fi

  echo "video-analyzer is not installed. Run through Docker Compose:"
  echo "docker compose run --rm analyzer bash scripts/analyze_one.sh $1"
  exit 127
fi

if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
fi

required_vars=(GEMINI_API_KEY GEMINI_API_URL GEMINI_MODEL)
for name in "${required_vars[@]}"; do
  if [ "${!name:-}" = "" ]; then
    echo "Missing required environment variable: $name"
    exit 1
  fi
done

video_name="$1"
video_path="videos/${video_name}"
output_dir="output/${video_name}"

if [ ! -f "$video_path" ]; then
  echo "Video file not found: $video_path"
  exit 1
fi

mkdir -p "$output_dir"

video-analyzer "$video_path" \
  --client openai_api \
  --api-key "$GEMINI_API_KEY" \
  --api-url "$GEMINI_API_URL" \
  --model "$GEMINI_MODEL" \
  --output "$output_dir" \
  --max-frames "${MAX_FRAMES:-20}" \
  --keep-frames \
  --whisper-model "${WHISPER_MODEL:-small}" \
  --language "${LANGUAGE:-zh}"
