# Short Video Analyzer

Dockerized short-video analysis service based on [`byjlw/video-analyzer`](https://github.com/byjlw/video-analyzer).

It reads videos from `videos/`, writes analyzer output to `output/<video-file-name>/`, and can optionally post-process `analysis.json` with the DeepSeek API into `audit_result.json`.

## Files

- `Dockerfile`: builds the analyzer image and installs `video-analyzer`.
- `docker-compose.yml`: runs the service with local `videos/` and `output/` mounts.
- `scripts/analyze_one.sh`: analyzes one video with Gemini through the OpenAI-compatible API client.
- `scripts/deepseek_postprocess.py`: reads `analysis.json` and writes `audit_result.json`.
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
GEMINI_API_KEY=your-gemini-api-key
GEMINI_API_URL=https://generativelanguage.googleapis.com/v1beta/openai
GEMINI_MODEL=gemini-2.5-flash
DEEPSEEK_API_KEY=your-deepseek-api-key
```

Build the Docker image:

```bash
docker compose -p short-video-analyzer build
```

If the server uses legacy Compose, use `docker-compose -p short-video-analyzer build`.

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
- `--api-url "$GEMINI_API_URL"`
- `--model "$GEMINI_MODEL"`
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
