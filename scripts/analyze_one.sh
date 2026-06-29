#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[analyze_one] %s\n' "$*" >&2
}

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
output_dir="${ANALYSIS_OUTPUT_DIR:-output/${video_name}}"

if [ ! -f "$video_path" ]; then
  echo "Video file not found: $video_path"
  exit 1
fi

mkdir -p "$output_dir"
log "video_name=${video_name}"
log "video_path=${video_path}"
log "output_dir=${output_dir}"
log "VISION_API_URL=${VISION_API_URL}"
log "VISION_MODEL=${VISION_MODEL}"
log "MAX_FRAMES=${MAX_FRAMES:-20}"
log "WHISPER_MODEL=${WHISPER_MODEL:-small}"
log "LANGUAGE=${LANGUAGE:-zh}"
if command -v ffprobe >/dev/null 2>&1; then
  log "ffprobe input follows"
  ffprobe -v error \
    -show_entries format=duration,size:stream=index,codec_type,codec_name,width,height,duration,nb_frames,r_frame_rate \
    -of json "$video_path" >&2 || log "ffprobe failed"
else
  log "ffprobe unavailable"
fi
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

log "starting video-analyzer"
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
log "video-analyzer finished"
log "output_dir file list after analyzer"
find "$output_dir" -maxdepth 2 -type f -printf '%p %s bytes\n' 2>/dev/null | sort >&2 || true
if [ -d "${output_dir}/frames" ]; then
  log "output_dir frames count=$(find "${output_dir}/frames" -type f | wc -l | tr -d ' ')"
fi
if [ -d output/frames ]; then
  log "legacy output/frames count=$(find output/frames -type f | wc -l | tr -d ' ')"
fi

if [ ! -f "${output_dir}/analysis.json" ] && [ -f output/analysis.json ]; then
  log "moving legacy output artifacts into ${output_dir}"
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
log "standardizing analysis elapsed_seconds=${elapsed_seconds}"
python scripts/standardize_analysis.py "$output_dir" --mode analyzer --elapsed-seconds "$elapsed_seconds"
log "standardization finished"

if [ "${ANALYZER_DIRECT_FALLBACK:-1}" != "0" ]; then
  zero_frame_analysis="$(
    python - "$output_dir/analysis.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
frames_extracted = int(metadata.get("frames_extracted") or 0)
frames_processed = int(metadata.get("frames_processed") or 0)
timeline_count = len(data.get("timeline") or [])
summary = str(data.get("summary") or "")
if frames_extracted <= 0 and frames_processed <= 0 and timeline_count <= 0 and summary:
    print("1")
else:
    print("0")
PY
  )"
  if [ "$zero_frame_analysis" = "1" ]; then
    log "zero-frame analyzer result detected; falling back to direct video input analysis"
    if [ -f "${output_dir}/analysis.json" ]; then
      cp "${output_dir}/analysis.json" "${output_dir}/analysis_analyzer_zero_frames.json"
    fi
    if [ -f "${output_dir}/analysis_raw.json" ]; then
      cp "${output_dir}/analysis_raw.json" "${output_dir}/analysis_raw_analyzer_zero_frames.json"
    fi
    direct_args=("$video_name" --output-dir "$output_dir")
    if [ "${ANALYSIS_PROMPT_FILE:-}" != "" ] && [ -f "$ANALYSIS_PROMPT_FILE" ]; then
      direct_args+=(--prompt-file "$ANALYSIS_PROMPT_FILE")
    fi
    python scripts/direct_video_analyze.py "${direct_args[@]}"
    log "direct video fallback finished"
  fi
else
  log "ANALYZER_DIRECT_FALLBACK=0; skipping direct video fallback check"
fi
