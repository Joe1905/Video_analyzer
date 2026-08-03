# FastMoss direct Skill refactor — implementation log

> Scope: `codex/ui-beautification-4004` only. No source, deployment, or configuration changes are permitted for 4002/4003, SellerSprite, SociaVault, or FastMoss presets other than `fm-product-scout`.

## Baseline

- Started: 2026-08-03 (Asia/Shanghai).
- Branch: `codex/ui-beautification-4004`.
- Initial HEAD: `32c7a3344ea25cf0fcc6c37cc1ddeee22f76318b`.
- Initial remote divergence: `0 ahead / 0 behind` vs `origin/codex/ui-beautification-4004`.
- Initial user-owned untracked paths preserved: `.workbuddy/`, `docs/refactor-plan-decouple-normalize-2026-08-01.md`.
- No secrets or raw paid API results are recorded in this log.

## Baseline call graph (CodeGraph)

- HTTP `/api/chat/ask` accepts `officialPresetId`, then starts `run_chat_deepseek`.
- FastMoss currently resolves a preset/Skill, exposes tools, executes through `execute_prefixed_tool`, and routes evidence-bearing answers through the V3 `complete_fastmoss_answer → synthesize_fastmoss_report_from_packet` path.
- Current V3 path includes decision-packet construction, a high-reasoning JSON decision request, optional verifier/finalizer behavior, and deterministic full-table fallback.
- SellerSprite reference path keeps a Skill instruction in the chat context and uses the shared `SemanticToolRenderer` family to make tool results readable before final synthesis. It is reference-only for this refactor.
- Existing stdio MCP lifecycle (`sellersprite_mcp_chat/stdio_mcp_client.js`) performs `initialize → tools/list/tools/call`, maintains a runtime tool cache, and recovers from process failure. It is retained.

## Phase record

### Phase 0 — baseline and safety checks

- Commands: `git status --short --branch`, `git rev-parse HEAD`, `git fetch origin`, divergence check, CodeGraph call-graph exploration.
- Result: baseline established; no edits made outside 4004 checkout.

### Phase 1 — direct local Skill and per-tool evidence

- Changed `scripts/fastmoss_lightweight_skill.py` so `fm-product-scout` has one authority: `scripts/skills/fastmoss-product-scout/SKILL.md`. It no longer reads `FASTMOSS_SKILL_SOURCE` or downloads/selects an official FastMoss Skill for this preset.
- Added the local Skill with the FastMoss-only capability boundary, US default, real-ID provenance, same-market/period comparison, candidate-only deep dives, stop conditions, evidence interpretation, final decision format, and explicit unsupported conclusions.
- `scripts/web_app.py` now renders each `fastmoss__*` result through `render_fastmoss_tool_evidence`; subsequent Planner turns receive bounded semantic evidence instead of raw payload JSON.
- `complete_fastmoss_answer` now ends through the collecting Planner's direct answer. It does not call the V3 semantic decision report. `finalize_fastmoss_answer` no longer invokes a verifier or fallback model.
- Added execution-time `allowed_tool_ids` enforcement to `execute_prefixed_tool` and pass the active FastMoss preset whitelist at both deterministic and model-driven calls. Unknown FastMoss preset IDs now fail closed before schema exposure/execution.
- Removed `fastmoss__fastmoss_detail_url_examples` from Product Scout's whitelist.
- Local static verification: `python -m py_compile scripts/web_app.py scripts/fastmoss_lightweight_skill.py` and `git diff --check` passed. Local unittest could not import `requests` from the bundled desktop Python; it will be run in the deployed container (environment limitation, not a test pass).
