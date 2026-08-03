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

### Phase 4 — regression recovery and deployment evidence

- Commit for Phase 3: `bc8e1bd` (`docs: consolidate fastmoss product scout skill`).
- First container run of `scripts/test_chat_tool_normalization.py` exposed an isolation regression: the initial V3 deletion range also contained SellerSprite's shared semantic report helpers. The failing missing symbol was `_naturalize_and_log_semantic_braces`.
- Restored exactly the SellerSprite/shared helper range from pre-cleanup commit `0134d79` onto V3-removal commit `846e08d`: SellerSprite dossier, semantic inline/braces renderer, report synthesis and its notice/logger/completion functions. No FastMoss V3, verifier, packet or fallback symbols were restored.
- This recovery is intentionally a separate reviewable commit before rerunning the full container regression and redeploying 4004.
- The next full regression found a second narrow dependency inside the retained live FastMoss evidence manifest: `_fastmoss_report_data_value`, `_fastmoss_requested_l3_id`, and `_fastmoss_report_scope_conflicts`. They normalize raw MCP data and fence returned rows outside a requested L3; they do not generate reports or call a model. They are restored separately, while all V3 finalizer/packet/verifier/fallback symbols remain absent.

### Final verification and deployment (2026-08-03 16:53 Asia/Shanghai)

- Commits in order: `0134d79`, `846e08d`, `bc8e1bd`, `37a07fc`, `d9c6381`. The deployed runtime source commit is `d9c638115d4a3e40e5fa8237cfb0d8a3290177ad`.
- Static checks: `python -m py_compile` for changed Python modules and `git diff --check` passed before each relevant commit.
- Container tests, final image: `python -m scripts.test_fastmoss_presets_boundary` (12 tests, pass), `python scripts/test_fastmoss_evidence_renderer.py` (pass), `node scripts/test_mcp_bridge_cache.js` (pass), `python scripts/test_chat_tool_normalization.py` (pass), and `python scripts/test_ui_contract.py` (12 tests, pass). The first full chat run found the two retained-helper regressions described above; the final full run passed.
- Real controlled MCP check (not mock/replay): `2026-08-03`, endpoint `127.0.0.1:4102/mcp`, JSON-RPC `tools/list`, HTTP 200. The live FastMoss directory returned official read-only tool schemas (sample names: `ad_data_overview`, `ad_search`, `agency_creator_analysis`, `agency_product_analysis`, `agency_product_list`). No business `tools/call`, paid social query, raw business result, key, session/message content, or report model request was issued for this acceptance check; therefore there is no fabricated model timing or chat session ID to report.
- Runtime log/source proof: the deployed `scripts/web_app.py` has no `synthesize_fastmoss_report_from_packet`, `verify_fastmoss_final_answer`, `fastmoss_data_first_fallback`, or `FASTMOSS_LLM_VERIFIER_ENABLED` occurrence. The direct end path logs `FastMoss final route=planner_direct`.
- Deployment: server pulled the same branch through `127.0.0.1:7890` and only `scripts/deploy_ui_4004.sh` was executed. Final 4004 container ID `4c233f9711bb9b902c3a160556f4f14eaf70173e2183f0e572d77a420cb6332d`, running/healthy; `/healthz` returned `{ "status": "ok", "ui_test_mode": false }`.
- Isolation evidence before and after deployment: 4002 `2c853f80e6ce18e186d72209895c2069a23aacccf3948a59601b227a36c1ee68` (`short-video-analyzer-dev_web_1`) running/healthy; 4003 `54b4a7c6f0b7dd32fdd3d0e88b8e8ea6739fcceeab3782bdafc0b17e9188446e` (`short-video-analyzer_web_1`) running/healthy. Neither ID changed.
- Remaining risk: a full business Product Scout chat was deliberately not started because it can trigger paid MCP calls. The live no-cost tools/list proves bridge credentials and discovery; the direct Planner/semantic/white-list lifecycle is covered by container regression with controlled fake tool data. A user-initiated Product Scout query is still needed to measure real final-answer latency and business recommendation quality.

### Phase 5 — retired evidence pipeline correction and cleanup (2026-08-03)

- Follow-up CodeGraph and full-repository reference tracing corrected an earlier Phase 2/4 assumption: the FastMoss `evidence_manifest`, `evidence_envelope`, and `evidence_facts` branch had no production caller after the direct local-Skill refactor. Current Planner evidence reads the MCP business payload directly through `render_fastmoss_tool_evidence`; only `evidence_metadata` and compact product-ID records remain live. The earlier log wording that called the manifest/envelope live is therefore superseded by this correction.
- Commit `3b87e5b` (`refactor: remove retired fastmoss evidence pipeline`) removed the dead manifest, envelope/facts builders, entity bundles, coverage/metric/conflict derivation, complete FastMoss dossier renderer, unused catalogs/imports, and their legacy-only tests: 1,915 deleted lines and 7 added lines. `_fastmoss_call_arguments_for_result` was intentionally retained because SellerSprite's report dossier still calls it.
- SellerSprite comparison used for the decision: SellerSprite has unknown-tool/strict-field-contract isolation and empty/error handling, but its successful business data is recursively rendered without a row, character, or token budget. It has no deterministic final-answer verifier; official direct presets rely on the official Skill plus per-call Semantic evidence. No new gate or verifier was added to either provider in this cleanup.
- Local checks passed: Python compilation, `git diff --check`, FastMoss per-tool Semantic tests, dual-provider Chinese Semantic tests, and API cache tests. Server checks passed: 12 FastMoss preset-boundary tests, FastMoss renderer tests, dual-provider Semantic tests, API cache tests, MCP Bridge cache tests, full chat-tool normalization, and 12 UI contract tests.
- The first host-side full chat run started a SellerSprite test Bridge without loading `.env.ui-4004`; `tools/list` correctly failed for a missing Key. The orphaned test PID was identified by port, parent, start time, and cgroup, terminated without touching container processes, then the test was rerun with the 4004 environment and passed. No secret value was printed.
- Only `scripts/deploy_ui_4004.sh` was executed. Final 4004 container ID is `5007795c94c50ef81701fee512d42350c65079ae3b55b67bb020d71a4959c654`, running/healthy; `/healthz` returned `status=ok` and `ui_test_mode=false`. Deployed source has zero occurrences of `fastmoss_evidence_manifest`, `fastmoss_tool_evidence_envelope`, `fastmoss_tool_evidence_facts`, and `render_fastmoss_evidence_document`.
- 4002 and 4003 remained untouched and healthy with unchanged IDs `2c853f80e6ce18e186d72209895c2069a23aacccf3948a59601b227a36c1ee68` and `54b4a7c6f0b7dd32fdd3d0e88b8e8ea6739fcceeab3782bdafc0b17e9188446e`.
