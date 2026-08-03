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

### Phase 2 — retired V3/report/verifier cleanup

- Commit for Phase 1: `0134d79` (`feat: use direct local skill for fastmoss product scout`).
- Before deletion, CodeGraph traced `synthesize_fastmoss_report_from_packet`, verifier, decision packet, dossier and report packet as a legacy-only branch after the Phase 1 direct finalizer change. No production caller remains for the two `_deprecated_*` tool helpers.
- Removed the entire legacy FastMoss report-only block: report prompts/dossier/packet rendering, V3 decision packet and high-reasoning retry, data-first full-table fallback, claim verifier/editor, integrity logging and entity-ID report rewriting.
- Removed the now-unused `FASTMOSS_LLM_VERIFIER_ENABLED` reader and both `_deprecated_build_prefixed_model_tools` / `_deprecated_execute_prefixed_tool` implementations.
- Retained `annotate_fastmoss_tool_result` and its normalized envelope because the live execution path still annotates MCP result state for UI/SSE and tool workflow state; per-tool Planner evidence is nevertheless emitted by `fastmoss_evidence_renderer`, not the retired report packet.
- Removed only V3/verifier/report-packet-specific regression cases and replaced them with a direct Planner path assertion. Remaining FastMoss evidence-envelope tests continue to protect result-state and field preservation.

### Phase 3 — single local Product Scout Skill

- Commit for Phase 2: `846e08d` (`refactor: remove fastmoss v3 report pipeline`).
- Removed the superseded `scripts/skills/fastmoss/BASE.md` and `scripts/skills/fastmoss/fm-product-scout.md`; the new `SKILL.md` is now the only local Product Scout prompt source.
- Removed the no-longer-read `FASTMOSS_SKILL_SOURCE` and retired verifier configuration from the 4004 example/Compose environment. `FASTMOSS_LIGHTWEIGHT_SKILL_MAX_ROUNDS` remains because it still bounds the active local workflow.
- Updated the manual five-preset mock-boundary script so Product Scout proves its local Skill is loaded while other presets retain their official Skill selection. It now also verifies execution-time whitelist rejection.
- Added a focused unittest assertion that a forbidden FastMoss tool is rejected before any MCP call.
