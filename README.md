# Short Video Analyzer

Dockerized short-video analysis service based on [`byjlw/video-analyzer`](https://github.com/byjlw/video-analyzer).

It reads videos from `videos/`, writes analyzer output to `output/<video-file-name>/`, and can optionally post-process `analysis.json` with the DeepSeek API into `audit_result.json`.

## Files

- `Dockerfile`: builds the analyzer image and installs `video-analyzer`.
- `docker-compose.yml`: runs the service with local `videos/` and `output/` mounts.
- `scripts/analyze_one.sh`: analyzes one video with an OpenAI-compatible vision API client.
- `scripts/translate_analysis.py`: translates `analysis.json` into `analysis_zh.json`.
- `scripts/deepseek_postprocess.py`: reads `analysis.json` and writes `audit_result.json`.
- `scripts/web_app.py`: serves the upload/analyze/result web UI.
- `scripts/run_web.sh`: starts the web UI on port `4000`, or the next available port.
- `.env.example`: template for required API settings.

## Ubuntu Server Setup

Clone the repository on the server:

```bash
cd /home/openclaw
git clone https://github.com/Joe1905/Video_analyzer.git
cd Video_analyzer
```

Create local runtime directories:

```bash
mkdir -p videos output
```

Create `.env` from the example:

```bash
cp .env.example .env
nano .env
```

Set these values:

```env
VISION_API_KEY=your-vision-api-key
VISION_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-flash
DEEPSEEK_API_KEY=your-deepseek-api-key
HF_ENDPOINT=https://hf-mirror.com
```

Build the Docker image:

```bash
docker compose -p short-video-analyzer build
```

If the server uses legacy Compose, use `docker-compose -p short-video-analyzer build`.

## Web UI

Start the web UI:

```bash
bash scripts/run_web.sh
```

The script starts at port `4000` and automatically advances to the next available port if needed. Open the printed URL in your browser.

The page supports:

- uploading a video into `videos/`
- starting `video-analyzer`
- optional DeepSeek postprocess
- viewing `analysis.json`, `analysis_zh.json`, `audit_result.json`, and runtime logs

## Analyze One Video

Copy a video into `videos/`:

```bash
cp /path/to/test.mp4 videos/test.mp4
```

Run the analyzer:

```bash
bash scripts/analyze_one.sh test.mp4
```

The script uses these defaults:

- `--client openai_api`
- `--api-url "$VISION_API_URL"`
- `--model "$VISION_MODEL"`
- `--output "output/test.mp4"`
- `--max-frames 20`
- `--keep-frames`
- `--whisper-model small`
- `--language zh`

To override defaults:

```bash
MAX_FRAMES=30 WHISPER_MODEL=medium LANGUAGE=zh bash scripts/analyze_one.sh test.mp4
```

Analyzer output is written to:

```text
output/test.mp4/analysis.json
```

## DeepSeek Postprocess

After `analysis.json` is generated, run:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4
```

With legacy Compose, use `docker-compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4`.

The audit result is written to:

```text
output/test.mp4/audit_result.json
```

## Direct Compose Usage

You can also run the analyzer script explicitly inside the container:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh test.mp4
```

Open a shell in the container:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash
```

Run Compose with `-p short-video-analyzer` to keep the project, containers, and network isolated from other Docker applications on the same server. The Compose file does not publish any host ports and does not set fixed container or network names, while still allowing outbound API calls.

## Git Notes

The following local runtime paths are ignored and should not be committed:

- `.env`
- `videos/`
- `output/`
