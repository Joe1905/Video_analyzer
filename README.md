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
- `scripts/sociavault_tiktok_shop.py`: extracts TikTok Shop product, review, or storefront data through SociaVault.
- `scripts/deepseek_shop_analyze.py`: turns TikTok Shop extraction JSON into a Chinese DeepSeek analysis report.
- `scripts/web_app.py`: serves the upload/analyze/result web UI.
- `scripts/run_web.sh`: starts the web UI on port `4000`, or the next available port.
- `scripts/setup_amazon_scraper.sh`: checks or installs the ClawHub `amazon-scraper` Docker image on the remote server.
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

SOCIAVAULT_API_KEY=your-sociavault-api-key
SOCIAVAULT_API_BASE=https://api.sociavault.com
SOCIAVAULT_REGION=US
SOCIAVAULT_MAX_PAGES=1
SOCIAVAULT_REVIEW_PAGES=1

AMAZON_PROXY=
AMAZON_PROXIES=
AMAZON_MAX_PAGES=1

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
bash scripts/setup_amazon_scraper.sh
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

## Daily Hot Video Report

A daily hot-video report page is available on the same web port:

```text
http://<server>:4000/report
```

The home page shows a banner for today's report. The v1 report manually collects TikTok hot-video candidates through SociaVault, stores the daily snapshot in local SQLite, and displays the ranked list. It does not generate a knowledge base or DeepSeek strategy summary yet.

The report API uses separate endpoints:

```text
GET /api/report/today
GET /api/report/history
POST /api/report/run
```

Results are stored in:

```text
data/hot_video_report.sqlite
```

It uses the existing SociaVault settings, especially `SOCIAVAULT_API_KEY`, `SOCIAVAULT_API_BASE`, and `SOCIAVAULT_REGION`.
The primary hot-video source is SociaVault `videos-popular`, using `days`, `page`, `count`, and `sort_by`.
`HOT_VIDEO_POPULAR_SORTS` controls the sort list (default: `views,likes`), and `HOT_VIDEO_POPULAR_MAX_PAGES` caps dynamic pagination (default: `5`).
The report target count is the report analysis count setting (default: `10`), so displayed and analyzed videos stay aligned.
Each collection round fetches `ceil(remaining_target * 2)` total videos across configured sorts, then advances the API page until the target is filled or max pages is reached.
If `videos-popular` is unavailable, the report falls back to the older `trending` and `search-top` sources so the daily job can still attempt to run.

Recent-window filtering for hot-videos is controlled by `HOT_VIDEO_RECENT_DAYS` (default: `7`), so only videos published in the last N days are considered by default.
Expired videos that are older than this window are cleaned from report records on report queries/sync runs, so old cards disappear end-to-end.

Report pipeline safeguards:
- `REPORT_JOB_TIMEOUT` (seconds, default `1800`) limits total time spent on a single `/api/report/run` task.
- `REPORT_DEEPSEEK_TIMEOUT` (seconds, default `180`) limits each per-video deep-dive audit call.

## Amazon Scraper

A separate Amazon scraper page is available at:

```text
http://<server>:4000/amazon
```

It calls the ClawHub `jiafar/amazon-scraper` skill through its Docker image:

```bash
docker run --rm --network host -e AMAZON_PROXY -e AMAZON_PROXIES amazon-scraper node assets/amazon_handler.js "<amazon-url>" --pages 1
```

The page supports Amazon URLs, ASINs, and keyword searches. It intentionally uses only `assets/amazon_handler.js`; the skill's generic non-Amazon scraping mode is not exposed.

Results are saved under:

```text
output/amazon/<job-id>/result.json
```

`bash scripts/run_web.sh` runs `scripts/setup_amazon_scraper.sh` before starting Compose. The setup script is idempotent: if the `amazon-scraper` image already exists it exits successfully; otherwise it runs `openclaw skills install amazon-scraper`. If the remote server does not have the OpenClaw CLI, install it or build the skill manually before starting the web service.

The web container needs access to the host Docker socket so it can start the scraper container:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```

Optional proxy settings:

```env
AMAZON_PROXY=http://user:pass@host:port
AMAZON_PROXIES=http://u:p@h1:8001,http://u:p@h2:8002
AMAZON_MAX_PAGES=1
```

The scraper container uses `--network host` so `AMAZON_PROXY` can reference `127.0.0.1`. If the host proxy is on `127.0.0.1:7890`, set:

```env
AMAZON_PROXY=http://127.0.0.1:7890
```

Docker bridge networking on some hosts cannot forward container-to-host traffic. `--network host` is the reliable workaround.

## TikTok Shop Extraction

A separate TikTok Shop page is available at:

```text
http://<server>:4000/shop
```

It calls SociaVault with `SOCIAVAULT_API_KEY` to extract either a TikTok Shop product detail plus reviews, or a storefront product list. If `DEEPSEEK_API_KEY` is configured, it can also generate a Chinese product and content analysis report.

Results are saved under:

```text
output/tiktok_shop/<job-id>/
```

The page uses separate endpoints and does not change the existing video analyzer page:

```text
POST /api/shop-extract
GET /api/shop-events?id=<job-id>
GET /api/shop-job?id=<job-id>
```

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
