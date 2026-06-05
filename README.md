# Short Video Analyzer

Dockerized short-video analysis service with two processing modes:

- `analyzer`: key-frame extraction through [`byjlw/video-analyzer`](https://github.com/byjlw/video-analyzer).
- `direct_video`: sends the full video directly to a Qwen OpenAI-compatible vision API.

Videos are read from `videos/`. Results are written to `output/<video-file-name>/`. The web UI can also download public TikTok or Douyin videos into `videos/` before analysis. Both processing modes produce the same normalized `analysis.json` schema, so DeepSeek postprocess works the same way for both.

## Files

- `Dockerfile`: builds the analyzer image and installs `video-analyzer`, Whisper, ffmpeg, requests, and `yt-dlp`.
- `docker-compose.yml`: runs the service with local `videos/` and `output/` mounts.
- `scripts/analyze_one.sh`: runs the existing key-frame `video-analyzer` flow.
- `scripts/direct_video_analyze.py`: sends a small full video to Qwen using `video_url` content.
- `scripts/tiktok_download.py`: downloads a public TikTok or Douyin video into `videos/` (`yt-dlp` for TikTok, Playwright media capture for Douyin).
- `scripts/standardize_analysis.py`: normalizes `video-analyzer` output to the shared schema.
- `scripts/translate_analysis.py`: translates analyzer or audit JSON output into Simplified Chinese.
- `scripts/deepseek_postprocess.py`: reads `analysis.json` and writes `audit_result.json`.
- `scripts/web_app.py`: serves the upload/analyze/result web UI.
- `scripts/run_web.sh`: starts the web UI on port `4000`, or the next available port.
- `.env.example`: template for runtime settings.

## Environment

Create `.env` from the example:

```bash
cp .env.example .env
nano .env
```

Required and commonly used values:

```env
VISION_API_KEY=your-vision-api-key
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-flash

ANALYSIS_MODE=analyzer
DIRECT_VIDEO_MODEL=qwen3-vl-flash
DIRECT_VIDEO_FPS=2
DIRECT_VIDEO_AUDIO_MODE=whisper
DIRECT_VIDEO_UPLOAD_MODE=auto
TIKTOK_MAX_BYTES=2147483648
TIKTOK_PROXY_URL=
DOUYIN_PROXY_URL=
DOUYIN_COOKIE=

DEEPSEEK_API_KEY=your-deepseek-api-key
HF_ENDPOINT=https://hf-mirror.com
```

Optional cost estimation:

```env
VISION_INPUT_PRICE_PER_1M=0
VISION_OUTPUT_PRICE_PER_1M=0
```

`.env`, `videos/`, and `output/` are ignored by Git and should not be committed.

## Ubuntu Server Setup

Clone and build:

```bash
cd /home/openclaw
git clone https://github.com/Joe1905/Video_analyzer.git
cd Video_analyzer
mkdir -p videos output
cp .env.example .env
nano .env
docker compose -p short-video-analyzer build
```

If the server uses legacy Compose:

```bash
docker-compose -p short-video-analyzer build
```

Run Compose with `-p short-video-analyzer` to keep containers and networks isolated from other Docker applications on the same server.

## Web UI

Start the web UI:

```bash
bash scripts/run_web.sh
```

The script starts at port `4000` and automatically advances to the next available port if needed. Open the printed URL in your browser.

The page supports:

- downloading a public TikTok or Douyin video URL into `videos/`
- uploading a video into `videos/`
- choosing `关键帧提取模式（video-analyzer）` or `直接视频理解模式（Qwen）`
- showing and editing the analysis prompt before a run
- optional DeepSeek postprocess
- showing processing mode, model, token usage, estimated cost, and total elapsed time
- viewing `提取内容（中文）` and `分析结果（中文）`
- switching each result tab back to original JSON with `显示原文`

## TikTok / Douyin Download

The downloader is exposed on the same web port as the analyzer but uses separate endpoints:

```text
POST /api/download
GET /api/download-job?id=<job-id>
```

Example API call:

```bash
curl -X POST http://127.0.0.1:4000/api/download \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://v.douyin.com/xxxxxx/"}'
```

The API accepts only `http` or `https` URLs whose host is under `tiktok.com`, `tiktokv.com`, `douyin.com`, or `iesdouyin.com`. TikTok uses `yt-dlp`; Douyin uses Playwright to open the page and capture the largest media response. Downloaded videos are saved as `videos/shortvideo_<platform>_<id>.mp4` when possible and then appear in the existing uploaded-video list.

Size limit is controlled by:

```env
TIKTOK_MAX_BYTES=2147483648
```

TikTok may require a US-region proxy. In a Docker bridge container, use the Docker host gateway instead of `127.0.0.1` for a proxy running on the server host:

```env
TIKTOK_PROXY_URL=http://172.17.0.1:7890
```

`DOUYIN_PROXY_URL` is optional and usually should stay empty for China-region Douyin access.

Some Douyin links require fresh browser cookies even when Playwright is used. Export a normal browser cookie header for `douyin.com` and put it in `.env` when needed:

```env
DOUYIN_COOKIE=passport_csrf_token=...; sid_guard=...; ...
```

Do not commit `.env`.

## Processing Modes

### `analyzer`

Default mode. It uses `video-analyzer` to extract key frames, call Qwen on frames, keep frames, and run Whisper transcription. This is better for larger videos because it does not send the whole video payload to the vision API.

Run it directly:

```bash
bash scripts/analyze_one.sh test.mp4
```

The script uses:

- `--client openai_api`
- `--api-url "$VISION_API_URL"`
- `--model "$VISION_MODEL"`
- `--output "output/test.mp4"`
- `--max-frames 20`
- `--keep-frames`
- `--whisper-model small`
- `--language zh`

Override defaults:

```bash
MAX_FRAMES=30 WHISPER_MODEL=medium LANGUAGE=zh bash scripts/analyze_one.sh test.mp4
```

### `direct_video`

Direct-video mode sends the full video to the OpenAI-compatible Qwen API using content type `video_url`.

For files under 7MB, it embeds the video as a Base64 data URL:

```bash
python scripts/direct_video_analyze.py test.mp4
```

Override defaults:

```bash
DIRECT_VIDEO_FPS=1 DIRECT_VIDEO_MODEL=qwen3-vl-flash python scripts/direct_video_analyze.py test.mp4
```

For files over 7MB, Base64 mode fails with a clear error. Automatic OSS upload is not implemented yet. A public URL hook is reserved:

```bash
python scripts/direct_video_analyze.py test.mp4 --public-url "https://example.com/test.mp4"
```

Current audio mode support:

```env
DIRECT_VIDEO_AUDIO_MODE=whisper
```

## Analysis Schema

Both modes write:

```text
output/test.mp4/analysis.json
```

The shared schema includes:

- `schema_version`
- `processing_mode`
- `vision_model`
- `audio_mode`
- `metadata`
- `summary`
- `transcript`
- `timeline`
- `visual_evidence`
- `raw_model_output`
- `usage`

`usage` records:

- `input_tokens`
- `output_tokens`
- `total_tokens`
- `api_calls`
- `elapsed_seconds`
- `estimated_cost_usd`

For `analyzer`, token counts are `0` unless the upstream tool exposes token usage; API call count and elapsed time are still recorded.

## DeepSeek Postprocess

After `analysis.json` is generated:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4
```

With legacy Compose:

```bash
docker-compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4
```

Outputs:

```text
output/test.mp4/audit_result.json
output/test.mp4/audit_result_zh.json
```

## Direct Compose Usage

Run analyzer mode inside the container:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh test.mp4
```

Run direct-video mode inside the container:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/direct_video_analyze.py test.mp4
```

Open a shell in the container:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash
```
