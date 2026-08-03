# Coding Agent Guidelines

## Working Style

Before changing code, inspect the relevant files and follow the existing patterns. Keep changes surgical: do not add new frameworks, broad refactors, or speculative features. Prefer the smallest change that directly supports the requested outcome.

This repository is a Dockerized service. Do not assume a local Python environment is available. Runtime commands should go through Docker Compose unless the task is purely static inspection.

## Project Overview

Short Video Analyzer analyzes short-form videos and related commerce/social data. The web UI runs on port `4000+` and exposes several workflows:

- video upload, TikTok/Douyin download, analysis, translation, and DeepSeek postprocess
- daily TikTok hot-video report
- TikTok Shop product/storefront extraction and DeepSeek analysis
- social-video metrics lookup
- Amazon scraper page backed by the `amazon-scraper` Docker image
- chat UI backed by local session storage and tool configuration

Two Compose services are defined:

- `analyzer`: one-shot/manual jobs, default command `bash`
- `web`: persistent HTTP server, command `python scripts/web_app.py`

Always include the Compose project name `-p short-video-analyzer` to avoid collisions with other Docker projects on the same host.

## Critical Commands

Build:

```bash
docker compose -p short-video-analyzer build
```

Start the web UI directly:

```bash
docker compose -p short-video-analyzer up web
```

Start the web UI with automatic port selection and Amazon scraper setup:

```bash
bash scripts/run_web.sh
```

Run analyzer mode:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash scripts/analyze_one.sh test.mp4
```

Run direct-video mode:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/direct_video_analyze.py test.mp4
```

Run DeepSeek postprocess:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/deepseek_postprocess.py output/test.mp4
```

Open a shell in the analyzer container:

```bash
docker compose -p short-video-analyzer run --rm analyzer bash
```

## Verification

There is no pytest/tox/lint/typecheck/CI configuration. Do not invent commands.

Use the relevant ad-hoc scripts when touched code maps to them:

```bash
docker compose -p short-video-analyzer run --rm analyzer python scripts/test_api_cache.py
docker compose -p short-video-analyzer run --rm analyzer python scripts/test_chat_tool_normalization.py
```

For web changes, prefer starting the web service and manually/API-checking the affected endpoint or page. If Docker is unavailable or required API keys are missing, say that clearly in the final response.

## Environment

Copy `.env.example` to `.env` and edit locally. `.env` is gitignored and must not be committed.

Common required keys:

- `VISION_API_KEY`: Qwen/OpenAI-compatible vision API
- `DEEPSEEK_API_KEY`: DeepSeek postprocess and reports
- `SOCIAVAULT_API_KEY`: TikTok/TikTok Shop/social data extraction

Important optional settings:

- `VISION_API_URL`, `VISION_MODEL`
- `ANALYSIS_MODE`, `DIRECT_VIDEO_MODEL`, `DIRECT_VIDEO_FPS`, `DIRECT_VIDEO_AUDIO_MODE`
- `TIKTOK_PROXY_URL`, `TIKTOK_COOKIE`, `DOUYIN_PROXY_URL`, `DOUYIN_COOKIE`
- `SOCIAVAULT_REGION`, `SOCIAVAULT_MAX_PAGES`, `SOCIAVAULT_REVIEW_PAGES`
- `HOT_VIDEO_RECENT_DAYS`, `HOT_VIDEO_POPULAR_SORTS`, `HOT_VIDEO_POPULAR_MAX_PAGES`
- `REPORT_JOB_TIMEOUT`, `REPORT_DEEPSEEK_TIMEOUT`
- `AMAZON_PROXY`, `AMAZON_PROXIES`, `AMAZON_MAX_PAGES`
- `API_CACHE_ENABLED`, `API_CACHE_TTL_SECONDS`
- `WEB_PORT`, `HF_ENDPOINT`

API responses are cached in `data/api_cache.sqlite` by default with a 7-day TTL. Set `API_CACHE_ENABLED=0` to bypass cache.

## Repository Structure

- `Dockerfile`: builds the runtime image with video analysis, ffmpeg/Whisper, Playwright-related support, and Python dependencies.
- `docker-compose.yml`: defines `analyzer` and `web`, binds `videos/`, `output/`, `data/`, and mounts `scripts/` read-only.
- `analysis_prompt.txt`: Chinese prompt template used by DeepSeek postprocess for TikTok/script analysis.
- `scripts/`: Python and shell scripts. In containers this directory is mounted read-only, so generated data must go to `videos/`, `output/`, or `data/`.
- `scripts/static/`: HTML templates loaded by `web_app.py` (`web_index.html`, `report.html`, `shop.html`, `metrics.html`, `amazon.html`, `chat.html`).
- `videos/`: input videos, gitignored.
- `output/`: job outputs, gitignored.
- `data/`: SQLite state and caches, gitignored.

## Key Scripts

- `scripts/web_app.py`: HTTP server, routes, background jobs, SSE event streams, and page wiring.
- `scripts/analyze_one.sh`: key-frame `video-analyzer` workflow.
- `scripts/direct_video_analyze.py`: direct full-video Qwen analysis.
- `scripts/standardize_analysis.py`: normalizes analyzer output to the shared `analysis.json` schema.
- `scripts/translate_analysis.py`: translates analyzer/audit JSON into Simplified Chinese.
- `scripts/deepseek_postprocess.py`: creates `audit_result.json` and `audit_result_zh.json`.
- `scripts/tiktok_download.py`: TikTok/Douyin downloader.
- `scripts/sociavault_tiktok.py`: SociaVault TikTok video/social extraction helpers.
- `scripts/sociavault_tiktok_shop.py`: TikTok Shop product/storefront extraction.
- `scripts/deepseek_shop_analyze.py`: Chinese product/content analysis for TikTok Shop output.
- `scripts/hot_video_report.py`: daily hot-video report collection, persistence, and analysis.
- `scripts/social_video_metrics.py`: social-video metrics extraction.
- `scripts/api_cache.py`: SQLite-backed API response cache.
- `scripts/chat_session.py`: chat session persistence.
- `scripts/tools.py`: chat/tool normalization and execution support.
- `scripts/video_queue.py`, `scripts/video_registry.py`, `scripts/entity_registry.py`: local queue, video, and entity metadata helpers.
- `scripts/setup_amazon_scraper.sh`: checks/installs the remote Amazon scraper image.
- `scripts/run_web.sh`: prepares dependencies and starts the web service on the first available port.

## Web Pages and Main Endpoints

Pages:

- `/` or `/chat`: main analyzer/chat landing behavior from `web_app.py`
- `/report`: daily hot-video report
- `/shop`: TikTok Shop extraction
- `/metrics`: social-video metrics
- `/amazon`: Amazon scraper

Common API groups:

- analyzer: `/api/upload`, `/api/analyze`, `/api/postprocess`, `/api/translate`, `/api/result`, `/api/files`, `/api/delete`
- downloads: `/api/download`, `/api/download-job`, `/api/download-events`
- reports: `/api/report/today`, `/api/report/history`, `/api/report/run`, `/api/report/settings`, `/api/report/events`
- shop: `/api/shop-extract`, `/api/shop-job`, `/api/shop-events`
- metrics: `/api/video-metrics`, `/api/video-metrics-job`, `/api/video-metrics-events`
- Amazon: `/api/amazon-scrape`, `/api/amazon-job`, `/api/amazon-events`
- chat/tools: `/api/chat/sessions`, `/api/chat/ask`, `/api/chat/tools`, `/api/chat/tool-config`

## Output Conventions

Video analysis writes to:

```text
output/<video-file-name>/
```

Main files:

- `analysis.json`: normalized analyzer/direct-video output
- `analysis_zh.json`: translated analysis where available
- `audit_result.json`: DeepSeek postprocess output
- `audit_result_zh.json`: translated DeepSeek output

TikTok Shop jobs write under:

```text
output/tiktok_shop/<job-id>/
```

Amazon jobs write under:

```text
output/amazon/<job-id>/result.json
```

Hot-video report data is persisted in:

```text
data/hot_video_report.sqlite
```

Chat/session/cache state lives under `data/` and should not be committed.

## Hot Report Known Failure Modes

- Daily summary generation must pass an explicit `max_tokens` value into `deepseek_postprocess.call_deepseek`; otherwise reports can finish video analysis but fail before writing the LLM summary.
- TikTok Photo Mode/image posts can expose music or audio-only media while lacking a real video stream. Hot-report collection should filter these before download/analysis instead of treating `.mp3`/`.m4a` output as an encoding problem.
- Hot reports currently can be marked `complete` with fewer than the configured `analysis_limit` videos because generation starts when successful videos are `>= 3`. This should be fixed so the target ingested/successful video count is exposed globally to the backend and UI, collection/processing continues until the target is met, and the report is only completed after that target is satisfied. The report job timeout can be relaxed to support this.
- Hot-report single-video deep-dive cache entries in `hot_report_videos.insight_json` can contain failure placeholders, for example old DeepSeek endpoint errors. These placeholders must not be treated as valid cache hits; later report regeneration should detect `error` or "generated failed" placeholder content, recompute the single-video insight, and then regenerate the daily summary.

## Analysis Schema

Both `analyzer` and `direct_video` modes should produce the shared `analysis.json` schema:

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

`usage` records token counts, API call count, elapsed seconds, and estimated cost when available.

## HTML Template Rules

When adding or changing a page, put page HTML in `scripts/static/` and load it from `web_app.py` via `SCRIPTS_DIR / "static" / "<page>.html"`.

`scripts/web_index.html` is the fallback legacy index. Prefer `scripts/static/web_index.html` for the main template when the server already uses the static template path.

Keep user-facing Chinese text encoded as UTF-8. Avoid editing files with tools that corrupt non-ASCII text.

## Docker and Data Boundaries

Inside Compose:

- `/workspace/scripts` is read-only
- `/workspace/videos`, `/workspace/output`, and `/workspace/data` are writable bind mounts
- `web` uses `network_mode: host`
- `web` mounts `/var/run/docker.sock` so it can launch the Amazon scraper container

Do not write generated files into `scripts/` at runtime. Do not commit files from `videos/`, `output/`, `data/`, or `.env`.

## Server-Direct Development and Deployment

### Server ports, deployed trees, and local worktrees

The three long-running web ports on `192.168.1.254` are separate environments.
Treat the port named by the user as an explicit environment selector; do not
silently substitute another port, checkout, Compose project, or data directory.

| Port | Meaning | Compose project / web container | Server checkout | Matching Windows worktree / long-lived branch |
| --- | --- | --- | --- | --- |
| `4002` | 正式版 / production | `short-video-analyzer` / `short-video-analyzer_web_1` | `/home/openclaw/Video_analyzer` | `C:\Users\admin\Documents\Video_analyzer` / `master` |
| `4003` | 开发版 / development | `short-video-analyzer-dev` / `short-video-analyzer-dev_web_1` | `/home/openclaw/Video_analyzer-dev` | `C:\Users\admin\Documents\Video_analyzer-dev-merge` / `developer` |
| `4004` | 2.0 / isolated UI and chat validation | `short-video-analyzer-ui-4004` / `short-video-analyzer-ui-4004_web_1` | `/home/openclaw/Video_analyzer-ui-4004` | `C:\Users\admin\Documents\Video_analyzer-ui-4004` / `codex/ui-beautification-4004` |

The checkout paths, long-lived branches, and Compose project names are the
durable mapping. Commit positions must still be rechecked with
`git status --short --branch` and `git rev-parse --short HEAD` before changes or
deployment. A Windows worktree is not automatically deployed; synchronize the
intended branch through GitHub before treating it as a port's deployed source.

When the user says "4004", they mean the 2.0 environment at
`http://192.168.1.254:4004/`, its Windows worktree
`C:\Users\admin\Documents\Video_analyzer-ui-4004`, and its server checkout
`/home/openclaw/Video_analyzer-ui-4004`. Inspect, test, build, restart, and read
logs only in the `short-video-analyzer-ui-4004` Compose project unless the user
explicitly expands the scope. Never rebuild, stop, or deploy the 4002 production
service while working on a 4004 request. Apply the same isolation rule to 4003.

From Windows, connect to the server as `openclaw@192.168.1.254` with the dedicated
private key `C:\Users\admin\.ssh\openclaw_codex_rsa`:

```powershell
ssh -i "$env:USERPROFILE\.ssh\openclaw_codex_rsa" openclaw@192.168.1.254
```

Use `openclaw_codex_rsa`, not its `.pub` companion. Never print, copy, upload, or
commit the private key. The key authorizes operational SSH access only; source
code synchronization from Windows must still go through GitHub as described
below, never SCP/SFTP/rsync.

The production server checkout is:

```text
/home/openclaw/Video_analyzer
```

When the current working tree is already the server checkout under `/home/openclaw/`, treat it as both the development workspace and the deployed checkout; do not SSH back into the same server or create a second checkout.

When working from a Windows development checkout:

- Source code synchronization must go through GitHub: push from Windows, then pull the same branch in the server checkout.
- SSH to `192.168.1.254` is allowed for read-only inspection, `git status`/`git pull`, Docker tests, deployment commands, logs, process inspection, and health checks.
- Do not use SSH command input, SCP, SFTP, rsync, archive piping, or copy/paste to transfer or rewrite source files on the server.
- Use the dedicated deployment SSH key when one is configured; never expose or commit private keys.

GitHub repo:

```text
https://github.com/Joe1905/Video_analyzer.git
```

GitHub access rules:

- From a Windows development checkout, GitHub `fetch`, `pull`, `push`, and `clone` operations must use the local proxy at `http://127.0.0.1:7892`.
- From the production server checkout, GitHub operations must use the server proxy at `http://127.0.0.1:7890`.
- Prefer explicit `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` / lowercase environment variables, or scoped `git -c http.proxy=... -c https.proxy=...`. Do not commit proxy credentials.
- If GitHub HTTPS fails during the initial handshake, record the concrete error and proxy state, then switch to the proxy path or another approved authenticated GitHub path. Do not blindly retry direct HTTPS.

Server-direct workflow (when Codex is already running in the server checkout):

1. Check the current server checkout with `git status --short --branch` and preserve unrelated changes.
2. Make the change directly in `/home/openclaw/Video_analyzer`.
3. Verify the change in Docker Compose using the project name `-p short-video-analyzer`.
4. Commit the verified change in the current checkout.
5. Push the commit to GitHub through the server proxy.
6. Rebuild or restart the affected service directly on the current server, then verify the endpoint or workflow.

All production changes must still be committed and synchronized through GitHub. Do not leave manual-only or uncommitted code changes in the server checkout. If GitHub push is blocked by proxy or authentication, stop and report the blocker instead of treating the deployment as complete.

Before changing or deploying files, check the current checkout:

```bash
git status --short --branch
```

Example rebuild/restart on the current server:

```bash
cd /home/openclaw/Video_analyzer
docker compose -p short-video-analyzer build
docker compose -p short-video-analyzer up -d web
```

Do not use SCP, SFTP, rsync, archive piping, or a second checkout to synchronize code. From a Windows checkout, SSH may be used only for the operational commands listed above; all source changes must reach the server through GitHub.

## Agent Safety Rules

- Check `git status --short --branch` before edits and before finalizing.
- Preserve user changes; never reset or overwrite unrelated work.
- Use `rg`/`rg --files` for search when available.
- Use `apply_patch` for manual file edits.
- Keep secrets out of logs, commits, and final summaries.
- When API keys, network access, Docker, or external services are unavailable, document the limitation and verify what can be verified locally.

## 4004 Current Project Memory (updated 2026-07-31)

This section is the current-state supplement for the isolated 4004 worktree. It takes
precedence over older generic port assumptions above when working in
`Video_analyzer-ui-4004`.

### Scope and deployment boundary

- Active branch: `codex/ui-beautification-4004`.
- Windows worktree: `C:\\Users\\admin\\Documents\\Video_analyzer-ui-4004`.
- Server worktree: `/home/openclaw/Video_analyzer-ui-4004`.
- Deploy only with `scripts/deploy_ui_4004.sh`; its Compose project is
  `short-video-analyzer-ui-4004` and it replaces only the 4004 web container.
- Preserve 4002 (`short-video-analyzer_web_1`) and 4003
  (`video_analyzer-dev_web_1`) container IDs and health. Do not run their Compose
  commands as part of 4004 work.
- Windows GitHub traffic uses `127.0.0.1:7892`; server GitHub traffic uses
  `127.0.0.1:7890`.

### Current blocking state — fix before any deployment

- Last GitHub and server checkout observed: `328133ad` (`fix: remove duplicated
  stream_events body from web_app.py`).
- At this revision `scripts/web_app.py` does not compile: the `except
  (json.JSONDecodeError, ValueError) as exc:` near line 15849 in `handle_download`
  has no indented body. `python -m py_compile scripts/web_app.py` fails with
  `IndentationError`.
- On the same verification, `short-video-analyzer-ui-4004_web_1` was
  `restarting / unhealthy` and `http://127.0.0.1:4004/healthz` was unavailable.
- Do not deploy feature work until the handler is restored, static compilation
  passes, the 4004 container is healthy, and `/healthz` responds. Treat the series
  of recent duplicate-definition/handler commits as a merge-sensitive area; do not
  reintroduce duplicate `execute_prefixed_tool`, `stream_events`, or handler
  exception blocks.

### Current chat architecture

- UI test/mock mode is off by default; normal 4004 chat uses live APIs.
- User-level tool selection, masks, and `enabledToolMasks` are removed/ignored.
  Tool catalog remains read-only for diagnostics with `selectionEnabled: false`.
- Home chat uses SociaVault MCP tools. The `SocialToolRoute` platform/capability
  router supports `off|shadow|enforce`; uncertain, invalid, empty, or mismatched
  routing must fail open to the complete SociaVault MCP catalog. No SociaVault chat
  REST fallback is allowed. `check_credits` is not cached; retain all other cache
  keys and TTL behavior.
- Amazon chat uses SellerSprite MCP. SellerSprite official Skills are pinned to
  version `0.1.17`, commit `afea6ad232b3bcae38704b1e5a5953f82492bdf1`, and a
  verified archive hash. `SELLERSPRITE_OFFICIAL_PRESETS` now covers all 27 UI
  presets. A selected preset has a stable `officialPresetId`, loads only its one
  official Skill document, exposes only its allowlisted `sellersprite__*` tools,
  and validates execution against that request-scoped allowlist.
- FastMoss now has seven configured preset routes in
  `FASTMOSS_OFFICIAL_PRESETS`, plus three quick actions and a general action. Keep
  their allowlists restricted to `fastmoss__*`, preserve their existing region/entity
  guardrails, and distinguish project-defined route groupings from any upstream
  official Skill wording.
- The chat UI shows the selected official preset as a removable chip above the
  composer. Tool invocation cards are capped at five visible rows with scrolling;
  do not restore the old tool-selection modal.

### Verification and documentation

- Minimum static guard after editing `web_app.py`: `python -m py_compile
  scripts/web_app.py` before Docker deployment.
- Relevant regressions include `scripts/test_chat_tool_normalization.py`,
  `scripts/test_fastmoss_presets_boundary.py`, `scripts/test_social_tool_router.py`,
  `scripts/test_ui_contract.py`, `scripts/test_semantic_chinese_rendering.py`,
  `scripts/test_fastmoss_evidence_renderer.py`, and `scripts/test_api_cache.py`.
- The implementation handoff and source-of-truth rules are in
  `docs/official-skill-tool-routing-handoff.md`. Read it before expanding presets
  or changing cross-provider routing.
- Validate tool boundaries separately from answer quality. Derived metrics,
  parent-vs-child entity scope, sample scope, timestamp conflicts, and investment/IP
  inferences require explicit evidence or an inference label.
