# Coding Agent Guidelines

## Project Overview

Dockerized short-video analysis service. Python scripts run **inside** a Docker container. There is no local Python environment — everything goes through `docker compose`.

Two Compose services:
- `analyzer`: manual one-shot video analysis (default command: `bash`)
- `web`: persistent HTTP server on port 4000+ (command: `python scripts/web_app.py`)

## Critical Commands

Always include the project name `-p short-video-analyzer` to avoid cross-project collisions:

```bash
docker compose -p short-video-analyzer build
docker compose -p short-video-analyzer up web
docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh test.mp4
docker compose -p short-video-analyzer run --rm analyzer python scripts/direct_video_analyze.py test.mp4
docker compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4
```

Start the web UI (auto-finds next available port starting at 4000):

```bash
bash scripts/run_web.sh
```

## Tests

No test framework (no pytest, tox). Run individual test scripts ad-hoc:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/test_api_cache.py
docker compose -p short-video-analyzer run --rm analyzer python scripts/test_chat_tool_normalization.py
```

## No Lint / Typecheck / CI

There is no linting, type checking, or CI pipeline configured. Do not guess at commands — they don't exist.

## Environment

Copy `.env.example` to `.env` and edit. `.env` is gitignored. Required keys: `VISION_API_KEY`, `DEEPSEEK_API_KEY` (for postprocess), `SOCIAVAULT_API_KEY` (for TikTok extraction).

API responses are cached in a local SQLite DB (`data/api_cache.sqlite`) with a 7-day TTL by default. Cache is enabled by default; set `API_CACHE_ENABLED=0` to bypass.

## Code Structure

- `scripts/` — all Python and shell scripts. Mounted **read-only** (`:ro`) in both Compose services. HTML templates for web pages live in `scripts/static/`.
- `videos/` and `output/` — Docker bind mounts, gitignored. Videos go in `videos/`, results in `output/<video-name>/`.
- `data/` — persistent SQLite data (API cache, hot video report, chat sessions). Gitignored.
- `analysis_prompt.txt` — the custom Chinese-language prompt template used by DeepSeek postprocess for TikTok script analysis.

## Remote Server

Production runs on `openclaw@192.168.1.254:~/Video_analyzer/`. GitHub repo: `https://github.com/Joe1905/Video_analyzer.git`.

## GitHub and Server Sync

Remote server changes must be synchronized through GitHub. Do not make or keep manual-only code changes on the production/server checkout. If a server-side hotfix is unavoidable, copy the exact change back into this repository, commit it, push it to GitHub, and then update the server from GitHub.

Before deploying or changing server files, check both local and server `git status --short --branch`. If the server has local modifications or untracked source files, reconcile them into GitHub first instead of overwriting them.

Deploy scripts via scp (example from `.claude/settings.local.json`):

```bash
scp -i ~/.ssh/openclaw_codex_rsa scripts/web_app.py openclaw@192.168.1.254:~/Video_analyzer/scripts/
```

Then on the server, rebuild and restart:

```bash
ssh openclaw@192.168.1.254 "cd ~/Video_analyzer && docker compose -p short-video-analyzer build && docker compose -p short-video-analyzer up -d web"
```

## HTML Templates

Web page HTML is loaded from `scripts/static/` (e.g. `shop.html`, `amazon.html`, `metrics.html`, `chat.html`). The fallback `web_index.html` is at `scripts/web_index.html`. When adding a new page, put its HTML in `scripts/static/` and reference it in `web_app.py` via `SCRIPTS_DIR / "static" / "page.html"`.
