#!/usr/bin/env bash
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "Usage: bash scripts/analyze_one.sh <video-file-name>"
  echo "Example: bash scripts/analyze_one.sh test.mp4"
  exit 2
fi

if ! command -v video-analyzer >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    exec docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh "$@"
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    exec docker-compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh "$@"
  fi

  echo "video-analyzer is not installed. Run through Docker Compose:"
  echo "docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh $1"
  exit 127
fi

if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
fi

VISION_API_URL="${VISION_API_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
VISION_MODEL="${VISION_MODEL:-qwen3-vl-flash}"
HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_ENDPOINT

required_vars=(VISION_API_KEY VISION_API_URL VISION_MODEL)
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
rm -f output/analysis.json output/audio.wav
rm -rf output/frames

start_epoch="$(date +%s)"
prompt_args=()
if [ "${ANALYSIS_PROMPT_FILE:-}" != "" ] && [ -f "$ANALYSIS_PROMPT_FILE" ]; then
  analyzer_help="$(video-analyzer --help 2>&1 || true)"
  if printf "%s" "$analyzer_help" | grep -q -- "--prompt-file"; then
    prompt_args+=(--prompt-file "$ANALYSIS_PROMPT_FILE")
  elif printf "%s" "$analyzer_help" | grep -q -- "--prompt"; then
    prompt_args+=(--prompt "$(cat "$ANALYSIS_PROMPT_FILE")")
  else
    echo "video-analyzer does not expose a prompt option; using default analyzer prompt."
  fi
fi

video-analyzer "$video_path" \
  --client openai_api \
  --api-key "$VISION_API_KEY" \
  --api-url "$VISION_API_URL" \
  --model "$VISION_MODEL" \
  --output "$output_dir" \
  --max-frames "${MAX_FRAMES:-20}" \
  --keep-frames \
  --whisper-model "${WHISPER_MODEL:-small}" \
  --language "${LANGUAGE:-zh}" \
  "${prompt_args[@]}"

if [ ! -f "${output_dir}/analysis.json" ] && [ -f output/analysis.json ]; then
  mv output/analysis.json "$output_dir/"
  if [ -f output/audio.wav ]; then
    mv output/audio.wav "$output_dir/"
  fi
  if [ -d output/frames ]; then
    rm -rf "${output_dir}/frames"
    mv output/frames "$output_dir/"
  fi
fi

elapsed_seconds="$(( $(date +%s) - start_epoch ))"
python scripts/standardize_analysis.py "$output_dir" --mode analyzer --elapsed-seconds "$elapsed_seconds"
