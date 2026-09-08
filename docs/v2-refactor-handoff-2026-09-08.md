# V2 重构交接文档

更新时间：2026-09-08
交接分支：`v2`
文档用途：让没有当前对话上下文的后续主智能体可以安全接手 Phase 5，并继续完成 Phase 6、Phase 7 与最终全量门禁。

后续范围更新：2026-09-08 用户在 Phase 5 明确删除整个淘宝采集功能，当前现场以主计划 §9.4 为准；本文早期功能保留和端口作用域盘点属于历史快照。现存页面数量改为 13，正式套件仍为 56 项。既有补偿缺陷已由用户决定仅落档，不再等待设计决策；不得将子批完成作为主线结束。

## 1. 一页结论

- 功能对齐已先于重构完成，Phase 0～Phase 4 已完成并通过阶段审计。
- 本交接的代码与计划基线为 `c5e16b0`；该提交只是 Phase 4 文档收口。最后一个运行时代码提交是 `218e671`。交接文档自身提交后 HEAD 会继续前进，接手者应以现场 Git 为准。
- 4004 已部署 `218e671` 对应镜像 `sha256:b0cccd7a700a45f8d603537d8ff792dfd54a46d22378755ded7046409632b42e`，启动时间为 `2026-09-08T07:35:03.261963607Z`。
- Phase 4 最终审计结果为 P0/P1/P2 全 0。`routes/services/jobs/core → web_app`、`services → routes`、`core → 领域模块` 的反向依赖均为 0。
- 下一步不是继续 Phase 4，而是 **Phase 5 代理子系统**。之后依次为 Phase 6 聊天/LLM、Phase 7 前端资源；完整 56 项基线和全局黑盒仅在最终候选阶段首次运行，若失败则修复后整套重跑。
- 当前不需要用户决策。只有触及本文“必须暂停”的高风险边界时才询问用户。

按阶段编号看，主体已完成到 Phase 4；剩余 Phase 5～7 和最终候选门禁。剩余三个阶段包含代理事务、聊天权限和前端交互，不能按“只剩三个编号”简单估算工作量。

## 2. 接手后必须按顺序阅读

1. 仓库根目录的 `AGENTS.md`。
2. `docs/refactor-execution-requirements.md`：最高优先级，规定“怎么做”。
3. 本交接文档：只描述 2026-09-08 的现场快照和接手路径。
4. `docs/refactor-plan-decouple-normalize-2026-08-01.md` 的当前阶段、验收标准、§十三和§十四：规定“做什么”。
5. `git status --short --branch` 与 `git log --oneline -20`：文档不能代替现场检查。

若本文与执行要求或主计划冲突，以执行要求优先，其次是主计划；接手者应先修正文档漂移再实施。本交接稿是仓库内的导航和现场快照，不设计成脱离上述两个权威文件单独驱动实施，否则三份规则会持续漂移。

## 3. 前因后果

### 3.1 为什么先对齐再重构

用户明确要求先比较正式版、开发版与 V2，把 V2 尚缺的活动功能补齐，再做结构重构。功能对齐已经完成，后续重构的默认目标是行为等价，不是重新设计产品。

保持不变的外部契约包括：

- 活动页面和 URL；
- API 状态码、JSON 字段与 SSE 帧；
- 文件名、输出目录、数据库和任务生命周期；
- 鉴权、owner/session 归属、Range、HEAD、流式和下载安全；
- 当前 UI 行为和 Compose 环境隔离。

### 3.2 已退役能力的处理原则

用户要求彻底删除，不增加任何专用兼容。当前规则是运行时、路由、别名、配置、测试特判、工具、目录、文档和探针均零残留；未知外部 provider 统一 fail-closed。

活动聊天 provider 只有 Home、SellerSprite、出海匠。不要在新代码、测试、计划、总结或黑盒请求中重新引入任何已退役专名或专用分支。

### 3.3 为什么改变测试频率

早期每个小阶段都运行完整回归，耗时过高。用户后来明确要求：

- 中间阶段只测本次重构模块、直接共享边界和受影响黑盒；
- 修复后从本阶段影响清单第一项重跑，不跑无关套件；
- Phase 4～7 全部完成并审计后，最终候选只运行一次完整门禁。

当前冻结的完整基线是 54 项确定性回归加 2 项 Playwright，共 56 项。Phase 4 收口时没有运行这 56 项，这是遵循策略，不是遗漏。

### 3.4 为什么强调脚本 TTL

用户发现历史改动中代码增量过大，怀疑一次性测试/调试脚本不断累积。治理后规则为：

- 优先复用既有测试文件；
- 临时脚本只能放在 `scripts/temporary/`；
- 创建时必须同步登记 `scripts/script_lifecycle.json`，最长 TTL 14 天；
- 阶段完成或到期时取更早者删除或转正；
- 不得先用后补登记，也不得把临时脚本命名为 `test_*.py`。

当前 `script_lifecycle.json` 为 `active_phase=null`、`scripts=[]`，`scripts/temporary/` 只有说明文件。

## 4. 环境边界

| 环境 | 端口 | Windows 工作树 / 分支 | 服务器 checkout | Compose 项目 | 权限 |
| --- | ---: | --- | --- | --- | --- |
| 正式版 | 4002 | `C:\Users\admin\Documents\Video_analyzer` / `master` | `/home/openclaw/Video_analyzer` | `short-video-analyzer` | 只读健康/隔离核对 |
| 开发版 | 4003 | `C:\Users\admin\Documents\Video_analyzer-dev-merge` / `developer` | `/home/openclaw/Video_analyzer-dev` | `short-video-analyzer-dev` | 只读健康/隔离核对 |
| V2 | 4004 | `C:\Users\admin\Documents\Video_analyzer-ui-4004` / `v2` | `/home/openclaw/Video_analyzer-ui-4004` | `short-video-analyzer-ui-4004` | 唯一实施和部署目标 |

硬规则：

- 不在 Windows 本地构建 Docker。
- 不构建、重启、部署或改写 4002/4003。
- 源码必须经 GitHub 同步；禁止 SCP、SFTP、rsync、archive pipe 或 SSH 输入复制源码。
- 服务器只用 `bash scripts/deploy_ui_4004.sh` 构建和部署 4004。
- 当前服务器使用 legacy `docker-compose`；不要擅自替换部署入口或启用 overlay。

## 5. 当前仓库与部署现场

### 5.1 Git 状态

- 本交接编写前，本地和服务器 `v2` 基线均为 `c5e16b0`，与 `origin/v2` 对齐，服务器 checkout clean。交接文档提交会自然产生一个更晚的文档-only HEAD。
- 运行镜像来自最后一个代码提交 `218e671`；`c5e16b0` 为纯文档提交，因此没有重复构建镜像。

当前 Windows 工作树存在下列用户既有资产，后续智能体不得擅自修改、暂存、移动、登记 TTL 或删除：

```text
M  README.md
?? .reasonix/
?? .workbuddy/
?? docs/4004-global-feishu-user-isolation-plan.md
?? docs/proxy-redesign-mockup.html
?? docs/report-overlays-refined-static.html
?? docs/report-player-refined-static.html
?? docs/report-redesign-mockup.html
?? docs/report-refined-static.html
?? docs/tool-v2-refined-static.html
?? reasonix.toml
?? scripts/inspect_report_data.py
```

尤其注意：`scripts/inspect_report_data.py` 是用户未跟踪资产，不属于本重构创建的临时脚本，不能擅自纳管或清理。接手后新出现且无法证明属于当前任务的任何脏文件，也一律先按用户资产保护；上表不是可覆盖文件白名单。

### 5.2 服务器证据

Phase 4 最终部署后：

- 4002、4003、4004 的 `/healthz` 均为 200；
- 4002 镜像 `sha256:96276f9b9bbd5ced4816094a4cc5c49cc9baf1d635c217b8153ed202a5c29dd1`，启动时间 `2026-09-08T02:31:54.859650217Z`；
- 4003 镜像 `sha256:b97c695d2d50db94b6b2088e7b7b5ce0392899fadc2f82495b41698c5bf1fb9a`，启动时间 `2026-09-08T07:28:23.909720319Z`；
- 4004 镜像和启动时间见本文件第一节；
- 4004 自最后部署以来没有本阶段 traceback/exception。

4004 启动日志有一条既有状态提示：三个静态代理条目当前对应系统选择为 `DIRECT`，因此未完成静态配置同步。它没有阻断健康检查或 Phase 4 请求，但应作为 Phase 5 的现场输入调查；不要直接修改真实代理数据来“消掉日志”。

### 5.3 GitHub 网络路径

- Windows GitHub 操作使用 `http://127.0.0.1:7892`。
- 服务器 GitHub 操作使用 `http://127.0.0.1:7890`。
- 最近一次服务器 HTTPS 拉取经 7890 先后出现 HTTP/2 framing 和 TLS 非正常终止。不要盲目重复同一路径。
- 已验证的备用路径是：使用现有服务器 GitHub deploy key，通过 7890 的 HTTP CONNECT 访问 `ssh.github.com:443`。该路径已成功把 `origin/v2` 与服务器 checkout 同步到 `c5e16b0`。
- 绝不打印、复制、上传或提交私钥内容。

正常路径优先使用：

```powershell
git -c http.proxy=http://127.0.0.1:7892 -c https.proxy=http://127.0.0.1:7892 push origin v2
```

```bash
cd /home/openclaw/Video_analyzer-ui-4004
git -c http.proxy=http://127.0.0.1:7890 \
    -c https.proxy=http://127.0.0.1:7890 pull --ff-only origin v2
```

可复用的服务器备用同步形式：

```bash
cd /home/openclaw/Video_analyzer-ui-4004
GIT_SSH_COMMAND="ssh -i /home/openclaw/.ssh/video_analyzer_github_write \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o ProxyCommand='nc -X connect -x 127.0.0.1:7890 %h %p' -p 443" \
  git fetch ssh://git@ssh.github.com:443/Joe1905/Video_analyzer.git \
  v2:refs/remotes/origin/v2
git merge --ff-only origin/v2
```

只在正常 HTTPS 握手失败后使用备用路径；记录失败时间、协议、代理和具体错误。

## 6. 已完成进度

### Phase 0～0.5：基线、功能边界与资产治理

- 建立 HTTP smoke、UI/浏览器基线和核心 workflow 生命周期契约。
- 完成共享能力中性化、应用数据清理、仓库与运行时零残留治理。
- 建立脚本 TTL 清单和正式 56 项基线。

### Phase 1：基础设施

- `core/http.py`：响应、文件与 SSE 基础能力。
- `core/config.py`：路径和导入期配置归一。
- `core/json_store.py`：原子 JSON 读写和锁语义。
- 收口 4004 镜像隔离、HTTP 断连和 Amazon 原子写漏洞。

### Phase 2：Router 与无业务状态路由

- 建立纯 Router、exact/template/GET prefix 冲突与优先级契约。
- 迁移健康、页面、固定证书和静态资源路由。
- Range、授权附件、封面、视频流等不同响应边界保持分离。

### Phase 3：任务快照与 Registry

- 四类任务已使用明确的 snapshot adapter 与 `JobRegistry`。
- 创建、更新、GET、POST、SSE 和日志归入单一 Registry 真源。
- 明确否决通用 JobService、共享业务基类和通用 SSE。

### Phase 4：业务 service/route 垂直切片

已完成并审计：

- Shop、Metrics、Amazon；
- Download、Upload、Analyze、Translate、Postprocess；
- Files、Result、Delete、Video Stream；
- analyzer execution；
- 日报 API、飞书报告适配、报告封面；
- 淘宝 API 与 LAN Chat API/SSE/文件边界。

最后一批 LAN Chat 服务器专项通过情况：

- `test_router.py`：25 项；
- `test_web_workflow_lifecycle.py`：通过；
- `test_lan_chat_primary_account.py`：1 项；
- `test_lan_chat_group_management.py`：8 项；
- `test_lan_chat_file_transfer.py`：12 项；
- `test_lan_chat_avatar_profile.py`：6 项。

Phase 4 最终 Terra 交叉审计与主审：P0/P1/P2 全 0。

## 7. 当前代码结构

当前主依赖方向是：

```text
web_app composition root
  → routes/<domain>.py
    → services/<domain>.py
      → jobs/core 或既有稳定领域接口
```

已经确认不存在：

```text
routes/services/jobs/core → web_app
services → routes
core → 领域模块
```

主要已落地模块：

- `scripts/core/`：HTTP、配置、JSON store；
- `scripts/routes/router.py`：路由解析；
- `scripts/jobs/registry.py` 与领域 snapshot adapter；
- `scripts/routes/`：各领域 HTTP 参数、错误映射和响应；
- `scripts/services/`：各领域业务编排；
- `scripts/services/analyzer_execution.py`：视频子进程执行能力。

仍然较大的文件是：

- `scripts/web_app.py`：约 10,099 行；
- `scripts/proxy_pool.py`：约 4,918 行；
- `scripts/static/proxy.html`：约 1,855 行。

这不是要求机械拆小。`web_app.py < 800` 只是最终观察指标；不得为了行数制造 facade、空 service、跨领域抽象或小文件碎片。接下来只有在 Phase 5～7 的真实边界迁出后，它才会继续自然缩小。

## 8. 下一目标：Phase 5 代理子系统

### 8.1 Phase 5 的完成目标

按事务边界把 `proxy_pool.py` 拆为：

1. `proxy/repository.py`：schema、migration、查询、事务；
2. `proxy/nodes.py`：VLESS/VMess/static/direct 解析、`port_scope` 与序列化；
3. `proxy/runtime.py`：mihomo/sing-box 配置、启动、健康和清理；
4. `proxy/accounts.py`：账号、代理绑定、会话与预检；
5. `proxy/publishing.py`、`proxy/collection.py`：任务状态机；
6. `routes/proxy.py`：最后接入稳定 facade，不直接操作 SQLite 或外部进程。

现有 `scripts/proxy_state.py` 已实现 mihomo selector 状态读取/切换、美国节点关键词筛选、按业务选择代理与探测 URL、HTTP 可用性探测，以及“控制器不可用时返回状态而不抛出”的调用语义。Phase 5 开始前必须审计它的活动调用方，能复用就复用，不得新写第二套 selector/probe 状态机。

“不增加兼容”专指已退役能力，不能据此破坏仍在使用的代理 API 或 Python 调用方。`proxy_pool.py` 只有在主计划允许且活动调用方尚未完成切换时，才可保留一个可删除条件明确的最窄 facade；不得保留旧别名、双实现或猜测性兼容。

### 8.2 必须保持的不变量

- 删除代理池：检查活动会话 → 解绑账号 → 暂停等待任务 → 清理运行时。
- 等待代理：`status='delayed'` 且 `stage='waiting_proxy'`。
- 重新绑定：只恢复该账号的上述任务为 `status='queued'`、`stage='proxy_rebound'`，并返回发布/采集恢复数量。
- 数据库提交和外部进程操作失败时必须补偿，或进入明确可重试状态，不能静默半成功。
- TikTok 与 Instagram 登录/采集状态不能混用。
- `port_scope` 不能丢失。
- API、UI、真实数据目录和当前外部进程语义默认不变。

### 8.3 建议的首批执行顺序

#### Phase 5.0：只读盘点

先不改代码，用 CodeGraph 和现有测试列出：

- 所有表、schema 初始化、migration、事务/commit/rollback 点；
- 节点解析、序列化、端口分配与 `port_scope` 消费点；
- mihomo/sing-box 文件写入、reload、启动、停止、恢复与补偿；
- 账号绑定、会话、发布和采集状态机；
- `web_app.py` 的所有代理 route/Handler 和 `proxy_pool.py` 的外部调用者；
- `scripts/proxy_state.py`、`tiktok_studio_publish.py`、`tiktok_studio_collect.py`、`instagram_content_collect.py` 与淘宝边界；
- 既有 `test_proxy_pool_lifecycle.py` 能覆盖什么、缺什么。

输出互斥文件所有权和影响清单，经主审确认后再实施。这里的“主审”是当前任务的主智能体：它负责亲自核对 CodeGraph/AST、测试有效性、用户脏文件和需求边界，并先在 commentary 留下确认结果；需要固化的结论在后续独立文档收口提交中写入主计划，不要求纯只读 Phase 5.0 自身产生提交。

#### Phase 5.1：repository 基线与迁移

第一批代码只处理 repository：

1. 复制隔离 SQLite fixture，不读取或写入真实 4004 数据；
2. 先用独立测试提交冻结旧 schema 升级、重复 migration、失败回滚和升级后读取；
3. 再用结构提交迁移 schema/query/transaction；
4. 故障注入明确断言 commit 失败后的数据库状态；
5. 不在同一提交迁移 runtime、账号、发布/采集或 route。

后续按 nodes → runtime → accounts → publishing/collection → route 的顺序，每个切片都重复同样的“冻结、迁移、专项、审计、部署、总结”闭环。

Phase 5.0 还必须先找到现有 seed 注入和清理入口，证明它们只使用隔离数据目录，并定义测试后的“记录为 0、文件不存在或恢复原快照”清理断言。找不到安全入口时，不得猜测命令或对真实 4004 数据造 seed；先完成隔离 fixture 设计和审计。

### 8.4 Phase 5 专项门禁

至少包括：

- `scripts/test_proxy_pool_lifecycle.py`；
- 被触及的发布/采集专项：`test_tiktok_studio_publish_description.py`、`test_instagram_content_collect.py`；
- repository migration/rollback 隔离 fixture；
- 运行时配置清理失败、数据库提交失败、运行时重启失败三类故障注入；
- `/proxy` 受影响 API 黑盒；
- route/package 的反向依赖扫描；
- `git diff --check`、Python 语法、CodeGraph、旧符号和 TTL 扫描。

只有修改 `proxy.html` 时才运行对应 UI/Playwright；纯后端切片不应重复浏览器回归。Phase 5 最终验收才用合成 seed 检查删池、未绑定、重新绑定和任务恢复，并验证 1440×900、390×844 两个 viewport；测试后删除 seed。

故障补偿的允许状态、幂等标识和恢复入口不能由交接稿猜测。Phase 5.0 必须从现有 schema、状态机、API 和测试冻结当前可观察行为；若现状不足且补偿方案会改变外部状态或真实数据语义，按高风险边界请求用户决策。

## 9. Phase 6 和 Phase 7 的边界

### Phase 6：聊天、工具网关与 LLM transport

- provider 规范化和 public/internal session ID 只能有一个实现；
- 官方 Skill 白名单既要限制 schema 暴露，也要在执行工具前再次复核；
- 工具域不能交叉泄漏，`officialPresetId` 只能属于当前请求；
- `tools.py` 的聊天归一/schema/执行迁入 `chat/tool_gateway.py`；活动调用者切换完成前只能保留窄 facade；
- LLM 只统一 transport，不统一业务 prompt/message/解析；
- 流开始后不自动重放，带副作用工具调用不由 transport 重试；
- 日报继续显式传 `max_tokens`。

Home、SellerSprite、出海匠是展示层活动集合；进入 Phase 6 时必须从当前 provider registry、规范化函数和边界测试读取内部 ID 与映射，不得根据展示名称自行发明内部值。

不要在 Phase 5 顺手做这些工作。

### Phase 7：前端资源

- 处理 `proxy.html` 的 CSS、请求、store 和 drawer workflow；
- 使用原生 JS 小模块，不引入新框架；
- 改静态资源必须更新 `UI_ASSET_VERSION`；
- 聊天仍是三 provider 共用一个壳，不拆成三套页面；
- 每个 UI 切片只跑受影响页面的桌面/窄屏和控制台检查。

不要在 Phase 5 后端拆分时提前重构前端。

## 10. 每个子阶段的标准闭环

以下闭环适用于产生代码或文档改动的子阶段。Phase 5.0 纯只读盘点不提交、不推送、不部署；只有它实际修正了计划文档时，才做独立文档提交且不触发运行时测试。

1. 从头到尾重读执行要求；再读当前计划和验收标准。
2. 运行 `git status --short --branch`，列出用户资产与本阶段文件所有权。
3. `codegraph sync .`，再用 `codegraph explore` 建影响清单。
4. 给 Terra 子智能体的任务必须要求其先读执行要求与当前计划；子智能体不得提交或部署。
5. 先以独立测试提交冻结当前行为；再以独立结构提交迁移。
6. 主代理复核 diff、调用方、依赖方向、测试是否真能失败和需求边界。
7. 只运行影响清单中的专项；失败后修复并从本阶段第一项重跑。
8. `git diff --check`、语法、CodeGraph/import、旧符号、零残留和 TTL 检查。
9. Windows 通过 7892 推送；服务器通过 7890 拉取同一 `v2` 提交。
10. 运行时代码只用 `bash scripts/deploy_ui_4004.sh` 部署 4004。
11. 只验证 `/healthz`、受影响页面/API、4004 近期日志、专属镜像、clean checkout；4002/4003 只读核对。
12. 先总结，再做需求漂移、解耦、复用、安全、测试有效性审计；审计问题收口后再更新计划。

提交必须单一性质：测试基线、行为修复、结构迁移、测试归属修正、文档收口分别提交。

## 11. Terra 子智能体使用规则

- 复用已有任务，不为同一长期工作无限创建记录。
- 并行任务必须文件所有权互斥，例如 repository、runtime、account/state-machine 只读盘点可并行，写入阶段由主代理控制合并顺序。
- 高风险集成至少“一人实现、一人交叉审计、主代理终审”。
- Terra 不提交、不推送、不部署、不修改真实数据。
- 主代理不能只转述子智能体结论，必须亲自看 diff、测试和 CodeGraph。
- 每个阶段结束释放已完成子智能体；累计任务记录不会修改代码，但无限堆积会增加上下文与管理成本。

仓库存在 `.codegraph/`，因此当前必须使用 CodeGraph。若 CodeGraph 暂时故障，先尝试 `codegraph sync .` 和 shell CLI；仍不可用时可用 `rg` 加 AST 做等价只读盘点，并明确记录降级与未覆盖风险。Terra 是用户指定的交叉审计能力；若暂时不可调用，继续安全的只读盘点和非高风险准备，记录能力缺失，但不得把主代理自审冒充已完成 Terra 交叉审计，也不得让尚未交叉审计的高风险写入越过门禁。能力恢复后再收口，不把它误报成需要用户设计决策的产品问题。

## 12. 必须暂停并请求用户决策的情况

只有以下情况暂停：

- 可能构建、重启、部署或改写 4002/4003；
- 需要破坏、迁移或不可逆修改真实数据；
- 需要改变外部 API、鉴权、owner/session 归属；
- 需要改变 Range、HEAD、流式或下载安全语义；
- 需要新增外部权限；
- 需要执行无法恢复的操作。

普通测试失败、低风险结构取舍、局部夹具修正和代理握手路径切换不需要暂停，应在既定范围自行收口。

## 13. 常见需求漂移与反模式

- 不要以减少 `web_app.py` 行数为目的拆文件。
- 不要创建万能 VideoService、ProxyService、通用 SSE、通用文件下载或通用 subprocess runner。
- 不要把不同 owner/token/Range/Content-Disposition 边界合并到一个未经审计的 helper。
- 不要修改测试期望来迎合迁移后的新行为；先判断是测试归属变化还是生产回归。
- 不要用页面 200 代替任务创建、状态、SSE、结果和清理生命周期契约。
- 不要把已知测试失败笼统标成“历史问题”。
- 不要每个小阶段运行 56 项全量回归。
- 不要把一次性探针、修复器或迁移器长期留在正式 `scripts/`。
- 不要把用户未跟踪文件加入提交。
- 不要因现有 `proxy_state.py` 或 `proxy_pool.py` 内有可复用代码，就未经边界审计直接复制一份。
- 不要为已退役能力增加任何专用兼容、别名、路由或测试。

Ponytail 原则：先确认是否需要、仓库是否已有、stdlib/平台/现有依赖能否解决；只有真实边界需要时才增加最小代码。不能以“简化”为理由删掉鉴权、输入验证、错误补偿、可访问性或数据安全。

## 14. 最终重构完成条件

Phase 5～7 全部完成、各自审计和计划收口后，才首次执行最终候选门禁；“一次”指不在中间阶段重复运行。最终门禁若失败，修复后仍必须整套重跑：

1. 自动发现全部正式套件；
2. 按 `scripts/test_deploy_ui_4004_boundary.py`、当前实际发现结果和主计划维护的口径，运行登记的 49 个普通 Python、3 个特殊门禁、2 个 Node 和 2 个 Playwright；合计仍表述为 54 项确定性回归加 2 项 Playwright，如长期门禁增加则同步增加计数；不得凭本文手抄一份可能过期的文件清单；
3. 检查 13 个现存页面：`/`、`/chat`、`/amazon`、`/chuhaijiang`、`/report`、`/report/player`、`/extract`、`/shop`、`/tool`、`/metrics`、`/lan-chat`、`/proxy`、`/harness`；
4. 检查受影响业务黑盒和未知 provider fail-closed；
5. 检查三端健康、4004 日志/镜像/checkout；
6. 核对 4002/4003 未被改动；
7. 复核依赖方向、脚本 TTL、零残留、需求漂移和安全边界。

完整集合任一失败，修复后从最终门禁第 1 项重新执行。只有全部通过才可以宣布整个重构完成。

## 15. 给下一位主智能体的启动提示词

```text
接手 Video_analyzer V2 重构。先完整阅读仓库 AGENTS.md、
docs/refactor-execution-requirements.md、
docs/v2-refactor-handoff-2026-09-08.md，
再阅读 docs/refactor-plan-decouple-normalize-2026-08-01.md 的 Phase 5、
§十三和§十四，并检查 v2 工作树 git status。

Phase 0～4 已完成，当前从 Phase 5.0 代理子系统只读盘点开始。
先用 CodeGraph 列清 repository/schema、nodes、runtime、accounts、
publishing/collection、route 及现有 proxy_state.py 的活动调用方，
给出互斥文件所有权、影响清单和最小 5.1 repository 契约批次。
使用 Terra 子智能体只读交叉盘点；子智能体不得提交或部署。
没有高风险决策就持续推进，但每个子阶段必须专项测试、总结、审计并修正计划。
不跑中间全量回归，不本地 Docker，不触碰 4002/4003，不修改用户既有脏文件，
不创建无 TTL 的一次性脚本，不为已退役能力增加任何兼容。
```

## 16. 权威证据入口

- 执行规则：`docs/refactor-execution-requirements.md`
- 主计划与完整提交/门禁账本：`docs/refactor-plan-decouple-normalize-2026-08-01.md`
- Phase 4 文档收口：`c5e16b0`
- Phase 4 最后运行时代码：`218e671`
- 4004 部署入口：`scripts/deploy_ui_4004.sh`
- 代理现有主模块：`scripts/proxy_pool.py`
- 代理现有状态 helper：`scripts/proxy_state.py`
- 代理主专项：`scripts/test_proxy_pool_lifecycle.py`
- TTL 清单：`scripts/script_lifecycle.json`

本文件是交接快照，不替代 Git、执行要求和主计划。后续每完成一个阶段，应同步更新主计划；只有现场状态发生重大变化时才更新本交接文档。
