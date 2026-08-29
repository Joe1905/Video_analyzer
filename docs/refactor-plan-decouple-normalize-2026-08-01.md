# V2 解耦化与归一化重构执行计划

初版日期：2026-08-01
复核日期：2026-08-29
适用环境：`v2` / 4004 / `short-video-analyzer-ui-4004`
技术路线：保留 stdlib `http.server`，先建立行为基线，再按稳定边界拆分
执行原则：同一提交只做“行为修复”或“结构搬迁”中的一种

## 一、2026-08-28 对齐基线

本轮先比较了三个长期分支：

- 正式版：`master`，基线 `e7e5dda`
- 开发版：`developer`，基线 `a4f1bfd`
- V2：`v2`，对齐前基线 `38cfff9`
- V2 对齐提交：`b364276`
- V2 功能对齐完成提交：`0232f9f`

结论：`developer` 的有效补丁已被 `master` 覆盖，因此 V2 以 `master` 为功能来源，同时保留 V2 独有的统一聊天壳、出海匠、邻聊、淘宝、代理运营台和 4004 隔离配置。没有用正式版页面覆盖 V2 页面。

对齐关系可用以下命令复核；前两条应无输出，后两条应成功：

```bash
git log --oneline --right-only --cherry-pick v2...master
git log --oneline --right-only --cherry-pick v2...developer
git merge-base --is-ancestor master v2
git merge-base --is-ancestor b364276 v2
```

已补入 V2 的正式版能力：

1. 日报暂停开关、恢复执行、候选刷新、视频分析超时与进程组清理、直传视频回退、默认不自动翻译。
2. Instagram 采集相关后端能力和代理任务恢复逻辑。
3. 代理池运行时修复、sing-box Compose 服务、容器内存限制、代理池删除后的账号解绑与等待任务暂停。
4. V2 对应补齐账号重新绑定 UI；绑定成功后可恢复因代理删除而暂停的等待任务。
5. TikTok Studio 异常信息保留，避免离开 `except` 作用域后丢失。
6. 视频分析超时测试更新为当前 `Popen`/进程组终止实现。
7. 4004 代理运行时使用独立命名空间 `v2`、Mihomo 节点前缀 `v2-`、普通代理端口 `19300-19399`、淘宝代理端口 `19400-19419`；V2 web 默认端口固定为 `4004`。
8. 代理端口迁移会同步已绑定账号的 `proxy_binding.local_port` 和 `browser_settings.proxy_server`；sing-box 检测同时兼容数据库行和归一化后的 pool 字典。
9. 活动聊天 provider 收紧为 Home、SellerSprite、出海匠；本条最初盘点时存在的 `/fastmoss*` 307 重定向已在 Phase 0.5 删除，当前旧路径自然返回无 `Location` 的 404。代理页已补齐共享 `.ui-header` 契约。

4004 隔离要求保持不变：Compose 项目、sing-box 项目、测试端口和服务操作都不得落到 4002/4003。`0232f9f` 已通过 GitHub 同步并部署到 `/home/openclaw/Video_analyzer-ui-4004`；部署后 4002、4003、4004 的 `/healthz` 均返回 200。

## 二、当前代码总览

CodeGraph 已在当前运行时代码 `1c630be` 上重新同步。主要热点如下：

| 文件 | 规模 | 当前职责 | 判断 |
| --- | ---: | --- | --- |
| `scripts/web_app.py` | 13,194 行 / 609,086 字节（约 594.81 KiB） | 配置、页面装配、聊天 provider、四类临时任务、下载/店铺/指标/Amazon、全部 GET/POST 路由、后台线程启动 | 第一重构对象，但必须分批拆；HTTP response/SSE helper、导入期配置与 JSON 文件原语已完成抽取 |
| `scripts/proxy_pool.py` | 5,319 行 / 238 KB | SQLite schema、代理解析、端口分配、mihomo/sing-box、账号会话、发布、采集、运行时状态 | 独立子系统，应在自己的包内拆分 |
| `scripts/hot_video_report.py` | 4,032 行 / 178 KB | 日报采集、下载、单视频分析、LLM 摘要、恢复与持久化 | 已有清晰文件边界，先稳定接口，不优先内部大拆 |
| `scripts/tools.py` | 1,470 行 / 72 KB | 聊天工具归一与执行、视频分析子进程 | 需要把“聊天工具”和“视频执行器”分开 |
| `scripts/static/proxy.html` | 1,856 行 / 291 KB | 代理运营台 HTML/CSS/JS、数据装配、任务交互 | 前端第二个上帝文件，后端 API 稳定后再拆资源 |
| `scripts/static/chat.html` | 307 行 / 76 KB | Home/SellerSprite/出海匠共享聊天壳 | 保持单一模板，不拆成三套页面 |

### 2.1 运行主链路

```text
main()
  ├─ 初始化聊天、邻聊、代理、视频队列和日报调度器
  ├─ 启动后台 worker
  └─ ThreadingHTTPServer
       └─ Handler.do_GET / do_POST
            ├─ 页面模板与统一导航
            ├─ Home / SellerSprite / 出海匠聊天
            ├─ 视频下载、分析、翻译与结果
            ├─ 日报、Shop、Metrics、Amazon
            ├─ 代理账号、发布与采集
            └─ 邻聊、淘宝、Harness 等 V2 功能
```

当前最危险的耦合不是单纯“文件太长”，而是：

- HTTP 参数解析、业务状态变更和响应拼装写在同一方法族中。
- `web_app.py` 同时是运行入口和大量测试的导入入口。
- 模块导入时会读取环境变量、初始化全局 store，并影响测试隔离。
- 聊天工具权限、provider 会话隔离和官方 Skill 白名单属于安全边界，不能作为普通搬运处理。
- 代理子系统同时控制数据库和外部进程，错误拆分会产生“数据库显示成功、运行时未生效”的半状态。

## 三、目标结构

目标是逐步形成以下结构，不要求一次性创建所有目录：

```text
scripts/
  web_app.py                 # 兼容入口、Handler 组装、main
  semantic_evidence_renderer.py      # 中性证据渲染协议与通用格式
  sellersprite_evidence_renderer.py  # SellerSprite 指标边界 adapter
  core/
    config.py                # 仅收口稳定的模块导入期配置
    http.py                  # 无业务状态的 HTTP response/SSE 原语
    json_store.py            # 原子 JSON 文件原语，不是通用 repository
  jobs/
    model.py                 # 稳定的任务快照协议
    registry.py              # 进程内锁、注册、日志、只读快照
  chat/
    providers.py             # provider 归一、UI 配置、会话作用域
    tool_gateway.py          # schema 暴露、白名单和执行前复核
    service.py               # ask/stream 编排
  proxy/
    repository.py            # schema、查询和事务
    nodes.py                 # URI 解析与端口规划
    runtime.py               # mihomo/sing-box 生命周期
    accounts.py              # 账号、绑定、登录会话
    publishing.py            # 发布任务
    collection.py            # TikTok/Instagram 采集任务
  services/
    downloads.py
    analyzer.py
    shop.py
    metrics.py
    amazon.py
    report.py                # 只做 web 适配，核心仍在 hot_video_report.py
  routes/
    router.py
    pages.py
    chat.py
    analyzer.py
    report.py
    proxy.py
    lan_chat.py
    taobao.py
```

强制 import 方向：`web_app` 可导入 `routes`；`routes` 可导入 `services/chat/proxy/jobs`；领域模块可导入 `core`。中性证据 renderer 不导入任何 provider，provider adapter 只能单向注入活动工具规则。反向 import 一律禁止。

```text
web_app → routes → services/chat/proxy/jobs → core
```

`web_app.py` 可以在过渡期为活动功能显式 re-export 兼容符号，但禁止 `from ... import *`。每个兼容导出都必须有调用方和删除条件；已退役 provider 不得以任何兼容导出、路由、配置别名或数据迁移重新进入代码。

### 3.1 需求漂移与复用准入门禁

每个子阶段开始前建立一页变更账本，明确“冻结契约、允许变化、禁止顺手修改、回滚点”。结构搬迁默认必须行为等价；原子写入、断连静默等可靠性变化必须独立提交、独立测试，不能夹在搬迁中。出现未登记的 URL、状态码、JSON/SSE 字段、数据目录、任务状态机、权限或 provider 作用域变化时，当前阶段立即停止，不以“重构需要”为理由吸收需求。

可复用模块只有在满足以下条件时才创建或扩张：

- 已存在至少两个活动调用方，或存在一个已经由契约测试锁定、即将迁移的稳定协议；不得为猜测中的未来需求抽象。
- 共享模块只拥有真正相同的机制，领域字段、错误文案、权限、状态机和补偿策略留在 adapter/service。
- `core` 只放无业务语义、纯 stdlib、可独立测试的原语；`jobs` 不启动线程或执行命令；`routes` 不持有状态；`services` 不认识 HTTP handler。
- 新抽象必须减少真实重复或切断反向依赖；如果只是增加 facade 转发、参数透传或文件数量，则保留现状。
- 每个阶段用 CodeGraph/AST 记录迁移前后调用数和 import 方向，并用黑盒契约证明没有功能漂移。

## 四、Phase 0：冻结并修正测试基线

这是重构前置门禁，未完成不得开始批量搬迁。

### 4.1 新增 HTTP smoke

新增 `scripts/test_web_smoke.py`，使用临时数据目录和只读/测试模式启动服务，覆盖：

- 页面：`/`、`/chat`、`/amazon`、`/chuhaijiang`、`/report`、`/report/player`、`/extract`、`/shop`、`/tool`、`/metrics`、`/lan-chat`、`/proxy`、`/taobao`、`/harness`。
- 路由规范化：`/amazon/`、`/chuhaijiang/` 的规范化或重定向，以及所有未注册路径的统一 404。
- 只读 API：`/healthz`、聊天 session/tool catalog、文件列表、日报历史、代理池状态。
- 功能开关：关闭日报或代理功能时返回稳定的 404/禁用响应，不抛服务器异常。

Phase 0 允许先增加一个只在 `UI_TEST_MODE=1` 时生效的 `APP_TEST_ROOT`。测试 runner 在启动 `web_app.py` 子进程前创建该目录下的 `data/`、`videos/`、`output/`，并设置：

```text
UI_TEST_MODE=1
APP_TEST_ROOT=/tmp/v2-smoke-<run-id>
APP_TEST_PORT_FILE=/tmp/v2-smoke-<run-id>/web.port
WEB_PORT=0
HOT_VIDEO_REPORT_ENABLED=0
```

生产默认路径不变。服务绑定随机端口后，将 `server.server_address[1]` 原子写入 `APP_TEST_PORT_FILE`；runner 只从该文件取得端口，再等待 `/healthz`，结束子进程并删除临时目录。

`UI_TEST_MODE=1` 的强制副作用契约：不启动代理 core/session/publish/collect worker，不启动 video queue 和日报 scheduler，不启动 provider MCP 子进程，不运行 SociaVault 诊断线程，不访问外网；所有文件和数据库只允许落到 `APP_TEST_ROOT`。普通 POST 保持 409 拦截；确需测试本地写入的 LAN chat 必须使用临时根目录。代理页面契约使用合成 seed/stub 单独启动 `PROXY_POOL_ENABLED=1`，但仍不得启动 mihomo/sing-box 或后台 worker。

### 4.2 冻结的首批 HTTP/UI 契约

| 请求/页面 | 唯一预期 |
| --- | --- |
| `GET /amazon/` | 307，`Location: /amazon` |
| `GET /chuhaijiang/` | 307，`Location: /chuhaijiang` |
| `GET /amazon/<tail>`、`/chuhaijiang/<tail>` | 404 JSON `{"error":"Not found"}` |
| 任意未注册的 GET/POST 路径 | 走通用 404，不存在历史 provider 重定向 |
| 日报关闭时 `GET /report` | 200，历史日报仍可读，页面显示暂停生成态 |
| 日报关闭时 `POST /api/report/run` | 503 JSON `{"error":"日报功能已暂停"}` |
| 代理关闭时 `GET /proxy` | 404 text `Not found` |
| 代理关闭时 `GET/POST /api/proxy/<tail>` | 404 JSON `{"error":"Not found"}`；POST 用 Handler 单元契约验证，避免被 UI 测试模式的 409 提前拦截 |
| 所有活跃页面模板 | 原始模板内恰好一个 `.ui-header`，且位于 `.ui-app > .ui-frame`；代理页已按该契约修复 |
| Home 快捷入口 | 4 个 `.quick-prompt`，`data-chat-scene` 恰好 0；旧的“6 个 scene”断言删除 |
| 出海匠官方场景 | `data-chuhaijiang-scene` 恰好 8 |

### 4.3 先处理现有红灯

2026-08-28 功能对齐完成后曾出现的门禁状态及其处理结果如下：

| 检查 | 对齐后状态 | 处理结果 |
| --- | --- | --- |
| `test_ui_contract.py` | 15 项通过 | 保持为共享壳和代理页 DOM 门禁 |
| `test_chuhaijiang_ui_contract.py` | 7 项通过 | 保持 Home 0 个旧 scene、出海匠 8 个官方 scene 的契约 |
| `test_chuhaijiang_boundary.py` | 5 项通过 | 保持活动 provider 与工具域隔离门禁 |
| `test_proxy_pool_lifecycle.py` | 通过 | 保持端口迁移、删池解绑、重新绑定和 sing-box 检测门禁 |
| `test_27_presets_mock_boundary.py` | 27 个 SellerSprite 官方预设通过 | mock 开关只注入测试子进程，生产默认仍为 0 |
| `test_chat_tool_normalization.py` | 当时仍是 V1/V2 混合套件，包含已退役 provider 断言 | Phase 0.5 已按活动 provider 收口并删除旧断言；未新增兼容套件，当前完整门禁通过 |

已知的运行时与 UI 红灯已经关闭。统一 HTTP smoke、活动 provider 契约、混合聊天套件拆分和浏览器回归已于 2026-08-28 完成并在服务器 4004 通过全量门禁；退役 provider 测试直接删除，不转成兼容测试。后续若测试矩阵新增红灯，不得笼统标记为“历史问题”后继续重构：每项必须落为活动 V2 套件中的 `pass`，或带有明确外部条件且不掩盖回归的 `skip`。

### 4.4 建立测试矩阵

按风险域维护最小矩阵：

- 聊天：provider 路由、会话隔离、官方 preset 白名单、工具执行前复核。
- 日报：暂停/恢复、单视频重试、缓存占位失效、LLM fallback、数据库生命周期。
- 代理：节点生命周期、删池解绑、重新绑定、发布/采集任务暂停与恢复。
- 视频：超时诊断、孤儿进程清理、direct-video prompt/压缩回退。
- UI：共享导航、三 provider 共享壳、代理绑定抽屉、日报禁用态。

2026-08-28 交叉审计确认，原矩阵对上传、下载、分析、翻译、后处理、店铺抽取和指标任务仍以页面 smoke/辅助函数测试为主，缺少无外网的 HTTP job 生命周期契约。Phase 0.5B 已新增 `scripts/test_web_workflow_lifecycle.py` 合成 fixture：覆盖请求校验、任务创建、状态查询、SSE 完成/失败结构、结果读取和临时目录清理；该测试禁止使用真实 API key、账号、额度、媒体发布、生产数据删除或外网抓取。页面返回 200 不能替代这些功能契约。

响应基线存放在 `scripts/contracts/`。快照生成时只允许规范化随机 ID、时间戳、临时绝对路径和日志时间前缀；字段缺失、状态码、重定向目标、SSE event/data 结构不得被归一掉。快照中只能使用合成数据，不得写入凭据、Cookie、真实账号或请求头。

### 4.5 退役 FastMoss 全量清理

FastMoss 已不是 V2 活动 provider。本计划按“零兼容、零运行时残留”处理：不保留 URL 重定向、provider/API 别名、工具域特判、会话迁移器、MCP Bridge、Skill、预设、配置项、前端分支或仓库内历史文档。旧 URL 删除专用路由后自然落入通用 404；旧 provider/tool 值按通用未知值处理，不写任何 FastMoss 专用兼容代码。

2026-08-28 盘点表明，遗留不只在测试中：`scripts/web_app.py` 约 615 处、`scripts/test_chat_tool_normalization.py` 约 379 处，另有 Compose/env、MCP Bridge、三个 FastMoss 专用 Python 模块、一个本地 Skill、三个专项测试和多份历史文档。行数只用于说明影响面，实施时以 CodeGraph 调用关系和 `git grep -i -E 'fastmoss|fast_moss'` 的当时结果为准。

清理范围：

| 类别 | 必须删除/迁移的内容 | 必须保护的行为 |
| --- | --- | --- |
| URL 与 API | 删除 `web_app.py` 中所有旧 URL 重定向和专用分支；从 provider/chat type 注册表、请求处理和 PDF/export 映射中移除旧 provider | 未注册路径统一返回 404，且不带 `Location`；外部 provider 入参使用通用严格校验，未知值不得静默降级到 Home |
| 工具安全边界 | 从 catalog、schema 暴露、参数归一、执行网关、LLM 编排和缓存域中删除专用 tool namespace | 任意未注册工具前缀都走通用 fail-closed 路径，不得到达 Bridge/API；SellerSprite、出海匠和 SociaVault 工具集不变 |
| 运行时与编排 | 删除 presets、playbooks、evidence/finalizer、业务默认值、报告模型分支、provider 会话、标题和旧 session 读取逻辑 | Home、SellerSprite、出海匠的 prompt、工具白名单、会话隔离和报告输出快照无差异 |
| 共享语义渲染 | `sellersprite_evidence_renderer.py` 仍从 FastMoss 命名模块导入通用 profile、renderer 和本地化函数；必须先迁入中性命名模块，再删旧文件 | 先对 SellerSprite semantic Markdown 做快照；迁移提交只改归属/命名，不改渲染结果 |
| 通用辅助函数 | 对被 SellerSprite、出海匠或 SociaVault 调用的旧 provider 命名 helper 先改为中性命名；其余直接删除 | 逐个函数用 CodeGraph 确认调用方；不允许因名字属于旧 provider 就误删共享能力 |
| Bridge 与配置 | 从 `sellersprite_mcp_chat/server.js`、`.env*.example`、`docker-compose.yml` 移除 endpoint/key/port/cache/Skill/report-model 变量和双 provider 分支 | SellerSprite Bridge 的工具列表、调用、缓存键与 TTL 测试通过 |
| 专用源码与资源 | 删除 `fastmoss_official_skill.py`、`fastmoss_lightweight_skill.py`、旧 provider 专用 renderer 业务部分、`skills/fastmoss-product-scout/`、专用测试、前端 CSS/label 和文件名含 FastMoss 的所有资产 | 只能在共享能力迁移且活动 provider 回归通过后删文件 |
| 历史会话与缓存 | 部署前将服务器 `data/fastmoss_mcp/` 和 `data/fastmoss_official_skill/` 备份到项目目录之外，验证备份后从 V2 应用数据目录删除；`retitle_sessions.py` 移除旧 store | 备份不挂载、不被运行时读取，仅用于部署回滚；回滚期后删除站外备份属破坏性操作，实施时再明确确认 |
| 文档 | README、AGENTS 和设计交付改为当前三 provider 契约；直接删除仓库中旧 FastMoss 设计、Skill 修复和实施日志 | 不在仓库新建历史兼容文档；审计记录由 Git 历史保留 |

实施顺序与提交边界：

1. **加活动域门禁：** 新增通用未注册路由/provider/tool 的 404/fail-closed 契约、SellerSprite 语义渲染快照和三 provider catalog 快照；此提交不删代码，也不新增旧 provider 专用契约。
2. **中性化共享能力：** 迁移 SellerSprite 仍使用的 semantic renderer/profile/localization 以及被其他域调用的通用 MCP 内容检查 helper；此提交不改外部行为。
3. **删除 provider 与专用路由：** 清理 `web_app.py`、`commerce_research_planner.py` 中的 provider、preset、orchestration、tool domain、session 路径和所有 307 分支；旧路径直接走通用 404。
4. **删运行时资产：** 清理 Bridge、Compose/env、前端分支、专用模块、Skill、专用测试和旧文档；此提交不删服务器真实数据。
5. **数据移出与零遗留检查：** 先生成项目外回滚备份并校验，再删除 V2 应用目录中的旧数据，最后运行全仓搜索和 4004 验收。

清理后，除本小节的计划记录外，以下命令必须无输出：

```bash
git grep -i -E 'fastmoss|fast_moss' -- . \
  ':(exclude)docs/refactor-plan-decouple-normalize-2026-08-01.md'
git ls-files | rg -i 'fastmoss|fast_moss'
find data -maxdepth 1 -iname '*fastmoss*' -print
```

三条命令都必须无输出。这意味着运行时 Python/JS/CSS、Compose/env、测试、README/AGENTS、普通文档、文件名和 V2 应用数据目录均不得再出现 FastMoss。不以“已无 UI 入口”、“断言暂时 skip”、“已转历史文档”或“运行时未触发”作为验收理由。

**FastMoss 清理验收：** 旧 URL 返回通用 404 且无 `Location`；活动 provider 只有 Home、SellerSprite、出海匠；三者的 catalog、会话隔离和专项测试通过；SellerSprite renderer/Bridge 快照无差异；Compose 生效配置和 4004 容器环境无 `FASTMOSS_`；项目内无旧 session/cache/Skill 目录；上述全仓搜索无输出；4004 日志无 FastMoss 进程、请求或启动失败。

**Phase 0 验收：** 服务器 4004 容器中 smoke 通过；混合聊天套件完成拆分；现有测试没有“来源不明”的红灯；测试矩阵写入仓库。

**Phase 0.5 验收：** FastMoss 全量清理达到上述验收标准，且清理后的完整活动功能回归通过；不得把 Phase 0 的旧基线结果当作 Phase 0.5 的验收结果。

### 4.6 当前执行状态、并行边界与阶段级回归门禁

截至 2026-08-29 的执行状态：

| 阶段 | 状态 | 阶段出口 |
| --- | --- | --- |
| Phase 0 测试基线 | 已完成 | 服务器 4004 确定性套件、HTTP smoke、桌面/移动浏览器回归全绿 |
| Phase 0.5A 共享能力中性化 | 已完成 | SellerSprite semantic renderer 输出保持一致；服务器 4004 全量回归全绿 |
| Phase 0.5B 运行时和仓库资产清理 | 已完成 | 运行时、Bridge/配置、UI/文档三条工作线合并后零残留扫描通过；补齐无外网的核心 workflow HTTP 生命周期 fixture；服务器 4004 全量回归全绿 |
| Phase 0.5C V2 应用数据清理 | 已完成 | `data/`、`data-dev/`、历史 session/cache/Skill 和旧 `.pyc` 已移出 V2 项目树；项目外备份逐文件校验通过；镜像、运行容器和服务器 4004 再次全量回归全绿 |
| Phase 0.5D 中性语义边界补漏 | 已完成 | 删除 7 个退役工具的不可达指标规则和 3 个隐藏枚举值；SellerSprite boundary、工具专属审计字段和查询标题规则全部回归 provider adapter；服务器 4004 完整门禁全绿 |
| Phase 1.1 `core/http.py` | 已完成 | 五个响应/SSE helper 已迁入纯 stdlib 模块；字节级与黑盒 HTTP 契约已补齐；两次独立服务器全量门禁均全绿 |
| Phase 1.1R HTTP 断连可靠性补漏 | 已完成 | JSON/text 非流式响应仅在 body 写入阶段忽略客户端断连；序列化/header/SSE 异常语义保持不变；部署后 Playwright 日志零 traceback |
| Phase 1.2A `core/config.py` 模型与测试 | 已完成 | `6c9731d`；纯 stdlib 不可变配置模型及隔离构造测试通过，服务器完整门禁全绿 |
| Phase 1.2B 路径配置切换 | 已完成 | `64e63af`；单一 `APP_CONFIG` 接管根路径与测试根路径，服务器完整门禁全绿 |
| Phase 1.2C 其余导入期配置切换 | 已完成 | `2115c5f`；模块级 `os.getenv` 为 0，函数期动态读取为 56 且基线多重集合不变，服务器完整门禁全绿 |
| Phase 1.2R 工具目录、外部 provider 与部署边界漏洞收口 | 已完成 | `a30b494`、`afacd7c`；出海匠工具目录五域完整，外部未知/退役 provider 与 4004 部署入口 fail-closed，服务器完整门禁全绿 |
| Phase 1.3A `core/json_store.py` 原子原语与故障注入 | 已完成 | `1f231c4`；纯 stdlib JSON 原语、12 项专项测试和同进程规范化路径写锁通过，服务器完整门禁全绿 |
| Phase 1.3B `web_app` helper 切换 | 已完成 | `1c630be`；47 个读、10 个原子写、旧 helper 定义 0 的 AST 契约通过，服务器完整门禁全绿 |
| Phase 1.3C `ChatStore` 只读复用审计 | 已完成（默认不迁移） | session 无末尾换行、debounce、双锁、迁移和异常日志的严格等价契约尚不足；保留领域实现 |

Phase 0.5B 可以多智能体并行，但文件所有权必须互斥：一条线负责 Python 运行时与专用模块，一条线负责 MCP Bridge/Compose/env，一条线负责静态 UI 与受控文档；README、计划文档、资产版本、跨线冲突和最终集成由主任务统一处理。子智能体只运行专项测试，不得独立提交；主任务合并审计后统一提交和部署。

“每个阶段全功能回归”定义为以下集合，适用于 Phase 0.5B、0.5C、0.5D，以及 Phase 1～7 中每个可执行代码子阶段；专项测试通过不能代替该集合。即使子阶段只新增尚未接线的 core/router 模块，也要执行完整门禁，防止导入、镜像内容或测试发现规则发生漂移：

1. Windows 工作树完成 `git diff --check`、Python/Node 语法检查、依赖边界检查和仓库零遗留扫描；本地不构建 Docker。
2. 源码提交并通过 GitHub 同步到 `/home/openclaw/Video_analyzer-ui-4004`，服务器使用 `docker-compose -p short-video-analyzer-ui-4004 build web` 重新构建镜像。
3. 在新镜像中运行登记的全部确定性 Python 套件、带显式功能开关的专项套件和 Node Bridge 套件；不只运行本阶段改动对应的测试。
4. 部署后检查 `/healthz`、全部活动页面和 API smoke；未注册路径以及已退役路径必须是无 `Location` 的通用 404。
5. 桌面与移动视口分别运行聊天滚动和邻聊上传队列 Playwright 回归，检查浏览器控制台和页面错误。
6. 检查 4004 容器环境、生效 Compose 配置、进程和近期日志；不得出现退役 provider、启动失败或触碰 4002/4003 的证据。
7. 记录镜像 ID、提交 SHA、测试总数和失败数。任一项失败即留在当前阶段修复并重跑完整集合，不得带红灯进入下一阶段。

固定计数口径：Phase 0.5 先在 4004 服务器新镜像中运行 **36 项构建前确定性回归**，即 32 个常规 Python、`test_hot_report_resume.py`、启用 `SELLERSPRITE_TOOL_MOCK_MODE=1` 的 `test_27_presets_mock_boundary.py`，以及 `scripts/test_mcp_bridge_cache.js`、`sellersprite_mcp_chat/test_stdio_mcp_client.js` 两个 Node 门禁；部署后再运行 **2 个 Playwright 脚本**，合计 38 个自动化脚本。Phase 1.1 新增 `test_core_http.py` 和 `test_http_response_contract.py`、Phase 1.2 新增 `test_core_config.py` 后，历史登记门禁为 **39 项构建前确定性回归 + 2 个部署后 Playwright = 41 个自动化脚本**。Phase 1.3 新增 `test_core_json_store.py` 后，当前登记门禁为 **40 项构建前确定性回归 + 2 个部署后 Playwright = 42 个自动化脚本**。Node stdio 门禁的实际路径为 `sellersprite_mcp_chat/test_stdio_mcp_client.js`。两个 Playwright 都覆盖桌面和移动 viewport。`test_api.py` 是吞异常的固定历史数据探针，`test_low_reasoning_video_insight.py` 会调用付费外部模型且失败仍返回成功，二者只作为人工实验，不计入阶段门禁。

Phase 0.5 最终证据（2026-08-28）：源码清理提交为 `0618559`，回归夹具修复为 `bd864cd`、`e362e7b`，递归排除历史 Python 字节码的构建修复为 `d1d4d2c`；服务器部署镜像为 `a70c61cd2e9f`。最终 36 项构建前回归失败数为 0，两个部署后 Playwright 均通过；`/healthz` 和 12 个活动页面为 200，旧路径 GET/POST/DELETE 均为无 `Location` 的 404。V2 项目树、镜像、容器环境、生效 Compose 和进程扫描均无旧 provider 残留。项目外回滚备份覆盖 11 组、128 个文件、3,788,317 字节，manifest SHA-256 为 `7f1c552462b943e733424e1f32c1a952a162256d3d247696b23dfdc70c61a91f`；备份保留原权限，未来恢复其中 root 文件时需要 `sudo`。

## 五、Phase 1：抽取低风险基础设施

每个子步骤独立提交，保持行为不变。

### 5.1 `core/http.py`

Phase 1.1 只迁移 `json_response`、`text_response`、`binary_response`、`file_response`、`write_sse_event` 五个稳定响应/SSE helper。路由和请求体读取仍留在 `web_app.py`；不得在本阶段移动 `_lan_chat_request_json`、multipart/JSON body 解析、静态资源/视频/附件服务、SSE 生命周期或任何业务异常映射。

按以下互斥边界实施，每个运行时提交后都必须执行当时登记的完整门禁；Phase 1.1 当时登记门禁为 40 个自动化脚本，后续阶段当前登记门禁为 42 个：

1. **1.1A 新模块与字节级单测：** 只拥有 `scripts/core/http.py`、`scripts/core/__init__.py` 和新建的纯 helper 单测。使用 `FakeHandler`/`BytesIO` 锁定 JSON UTF-8 与缩进、no-cache、HEAD、Range 206/416、RFC 5987 文件名、1 MiB 流式读取、BrokenPipe 行为以及 SSE `data:` 帧和 flush；不导入 `web_app.py`，不产生循环依赖。
2. **1.1B 调用方替换：** 单独拥有 `scripts/web_app.py`，显式导入上述五个 helper 并逐个替换原定义。AST 终审确认真实直接调用量为 278 个 JSON、13 个 text、12 个 binary、6 个 file 和 4 个 SSE，共 313 个；状态码、headers、异常捕获和 Handler 签名必须保持不变，不改路由条件和业务分支。
3. **1.1C HTTP 黑盒契约：** 单独拥有 HTTP contract 测试文件，补齐当前 smoke 未锁定的 Range、Content-Disposition、HEAD、SSE 帧和断连行为。它可以与 1.1A 并行准备，但必须在 1.1A 合入后执行，在 1.1B 合入后作为端到端回归再次执行。
4. **1.1D 只读交叉审计：** 用 CodeGraph 核对调用数、导入方向和遗漏定义；不得修改 A/B/C 拥有的文件。主任务统一提交、GitHub 同步、服务器构建和部署。

验收：新增字节级单测、HTTP smoke、workflow lifecycle、SSE 专项、未注册/禁用功能 404 分支通过，且 Phase 1.1 当时登记的 40 脚本门禁全绿。

Phase 1.1 最终证据（2026-08-28）：`6d7a52b` 新增纯 stdlib `scripts/core/http.py`、12 项字节级单测和 HTTP 黑盒契约，`03827ac` 只在 `web_app.py` 增加一次显式导入并删除五个原定义。AST 验收为旧 FunctionDef 0、导入 1、313 个直接调用数保持不变；CodeGraph 已在最终代码上重新同步。A/C 集成提交和 B 调用方切换提交分别在服务器新镜像执行 38 项构建前回归，均为 0 失败；最终部署镜像 `fc57b7755c31` 的 `/healthz`、12 个活动页面、旧路径 404、两个桌面/移动 Playwright 全部通过，4002/4003 健康检查仍为 200。部署日志无 import、语法或启动失败；Playwright 导航取消产生的两条 `BrokenPipeError` 为抽取前既有的客户端断连行为，本阶段按行为等价要求保留，后续若要静默应作为独立可靠性改动并单独回归。

2026-08-29 补漏证据：`b957587` 将 SellerSprite 的指标边界、工具专属审计字段、查询标题规则和匿名关键词动态提示全部移回 provider adapter，并从中性 renderer 删除 7 个退役工具规则和 3 个隐藏枚举值；服务器镜像 `4e6701d89667` 通过 38 项确定性回归、2 个 Playwright、12 个活动页面、旧路径 404 和 4002/4003 隔离检查。`8572f31` 只在 JSON/text 非流式响应的 body 写入阶段捕获 `BrokenPipeError`/`ConnectionResetError`，新增断连与序列化异常单测后服务器镜像 `bb4a08d9e487` 再次通过同一完整门禁；Playwright 后容器日志中的 traceback、BrokenPipe、ConnectionReset、语法和 import 错误匹配数为 0。SSE 断连仍向上传播以终止事件循环，JSON 序列化和 header 异常仍显式失败。

### 5.2 `core/config.py`

本阶段只收口**模块导入期配置**，不把所有 `os.getenv` 机械搬入一个全局对象。2026-08-29 审计基线为 `web_app.py` 共 69 处环境读取，其中 13 处发生在模块导入期，包含路径、布尔、整数、浮点和字符串；请求期 API Key、Mock 开关、tool router mode、动态端口和任务参数继续在所属 service/request 边界读取，避免把安全开关和测试覆盖冻结为进程启动快照。

配置模块使用纯 stdlib 和不可变 `AppConfig`，提供显式 `from_env(env: Mapping[str, str], root: Path | None = None)` 构造入口。默认 `root` 继续使用 `Path.cwd()`，不得借重构改为 `__file__` 推导；布尔、整数和浮点解析必须保持当前默认值、空值和非法值失败语义。不得把真实密钥、Cookie、token 或 provider 会话放入配置对象，也不得导入 `web_app`、store、route 或 service。

Phase 1.2 已按以下子阶段完成，每个阶段均独立提交并执行当时登记的 41 脚本门禁：

1. **1.2A 纯配置模型与解析测试：** 新增 `core/config.py` 和纯单测，覆盖两份 env mapping 在同一进程独立构造、路径派生、测试根目录只在 `UI_TEST_MODE` 开启时生效、布尔同义值、整数/浮点边界和非法值显式报错；不切换 `web_app.py`。
2. **1.2B 路径配置切换：** `web_app.py` 从单个 `APP_CONFIG` 派生 `ROOT`、`RUNTIME_ROOT`、`SCRIPTS_DIR`、`DATA_DIR`、`VIDEOS_DIR`、`OUTPUT_DIR` 及其路径常量；过渡期只允许显式同名赋值，不允许 `from core.config import *`。保持 4004 Compose 工作目录和 `APP_TEST_ROOT` 行为不变。
3. **1.2C 其余导入期配置切换：** 收口当前剩余导入期的 TTL、数量限制、超时、OCR 路径、Feishu cache、proxy enabled 和 UI 测试来源配置；AST 门禁要求 `web_app.py` 模块顶层不再直接调用 `os.getenv`。函数体内的动态环境读取不在本阶段迁移。

验收边界必须写清：Phase 1.2 保证“同一进程可构造多个互不污染的 `AppConfig`”，不承诺“同一进程同时运行多套完整 web 应用”。`ChatStore`、`LanChatStore` 和 provider stores 当前仍由 composition root 在导入期实例化，完整多实例应用要等路由/服务组装边界建立后再评估；测试 web 运行时继续使用独立子进程与 `APP_TEST_ROOT`。4004 默认值不得回落到 4002/4003 的项目名、数据目录或端口。

Phase 1.2 最终证据（2026-08-29）：`6c9731d`（1.2A）服务器镜像 `5a40219821e279f3c3e7d1bc27a9a2e8053c3efadfe4725ad130874311238577`、`64e63af`（1.2B）服务器镜像 `3923dfaff675cb86f6f98f6c2c4095134ad1a7e7cd11507a96ed61c1edde7c18`、`2115c5f`（1.2C）服务器镜像 `00a5deeca714557a23ebf76df888ee2a1105a0a421bccf9d1f4f8bb091bd37cc` 均通过 **39 项确定性回归 + 2 个 Playwright = 41 项**，失败数为 0。1.2C 的 AST 终审确认模块级 `os.getenv` 为 0、函数期动态读取为 56，读取键的基线多重集合不变。

Phase 1.2R 漏洞收口证据（2026-08-29）：`a30b494` 的服务器镜像为 `sha256:42fd8d8cff7d6d6e3de4c5b17e99dd0cc8fe357d61049e5fa68d762de16041b0`，**39 项确定性回归 + 2 个 Playwright = 41 项**全绿。`/api/chat/tool-catalog` 恢复 200 并包含 system、function、SociaVault、SellerSprite、出海匠五域；全部 10 个外部 chat provider 入口均 fail-closed，unknown/retired provider 在 sessions、messages、catalog、events、ask、export、rename 和删除等读写路径返回 400，内部 `None` 仍归 Home。FastMoss URL 的 GET/POST/DELETE 均为无 `Location` 的 404；4004 近期日志异常匹配为 0，4002/4003 健康检查均为 200。

部署边界终审发现旧 `UI4004_BRANCH` / `ALLOW_NON_UI4004_BRANCH` 可绕过分支检查后，`afacd7c` 将部署分支硬锁为 `v2` 并增加防回归断言，所有 legacy、非 `v2`、非 4004 项目名和非 4004 端口均在首次 Docker 探测前 fail-closed。最终服务器镜像为 `sha256:2fa76aa8a45faade8cd34af16fc257e9962ba1d24c8669eee29d553cbfb7342e`，再次通过 **39 项确定性回归 + 2 个 Playwright = 41 项**；12 个活动页面全部 200，五域 catalog 与 unknown provider 黑盒检查通过，FastMoss 三方法仍为无 `Location` 的 404，4004 日志异常匹配为 0，Compose/容器环境退役配置匹配为 0，4002/4003/4004 健康检查均为 200。

### 5.3 `core/json_store.py`

只提供纯函数式 JSON 文件原语，不创建通用 repository、ORM、数据库基类或有业务状态的 `JSONStore`。SQLite 事务继续留在各领域 repository 中；`ChatStore` 的 debounce、领域锁、异常日志和 session 迁移仍归 `chat_session.py` 所有。

原子写入是明确的可靠性变化，不再标记为“纯行为等价搬运”。新实现必须保持 UTF-8、`ensure_ascii=False`、两空格缩进、末尾换行、父目录创建、文件不存在时的既有返回契约和损坏 JSON 显式异常；临时文件必须与目标同目录，完成 flush/fsync 后用 `os.replace`，替换失败时清理临时文件且保留旧目标。锁的承诺限定为**同一进程内按规范化路径串行写入**；跨进程防丢更新不在本阶段伪装保证，跨进程一致性继续由 SQLite 或领域协议解决。

Phase 1.3 已按以下子阶段完成；每个运行时子阶段均独立提交并执行当前登记的 42 脚本门禁：

1. **1.3A 原子原语与故障注入测试：** 新增 `read_json`、`atomic_write_json` 及临时文件/锁管理测试，不切换运行时调用方。覆盖并发读写始终得到完整 JSON、替换失败保留旧文件、序列化失败不创建目标、损坏 JSON 抛错和异常后锁释放。
2. **1.3B `web_app` helper 切换：** 只替换 `web_app.py` 当前的 `read_json`/`write_json` 定义与调用；迁移前后对缺失文件、损坏文件、缩进和末尾换行做字节契约。`result_path.write_text(json.dumps(...))` 等非 helper 写入必须逐项审计，不允许顺手全仓替换。
3. **1.3C 只读复用审计（默认不迁移）：** `chat_session.py` 当前已有自己的 `tempfile + fsync + os.replace`、双锁和 debounce，而且 session 文件当前没有末尾换行，与 `web_app.write_json` 的字节契约不同。只有 contract test 证明 debounce、锁顺序、迁移、错误日志和文件字节完全不变时才允许复用更底层的临时文件原语；否则保留领域实现，不作为 Phase 1 阻塞项。

验收：故障注入和并发测试全绿；确认没有 SQLite、业务 dataclass、session schema 或 job 状态迁入 `core/json_store.py`；可靠性变化单独记录，不能夹带业务修复。

Phase 1.3 最终证据（2026-08-29）：`1f231c4`（1.3A）服务器镜像 `sha256:b8ac407aab6efca544ffd0538be01fc5bcef80126b6dcfc0fd63305e63f1b510` 新增纯 stdlib `read_json` 与 `atomic_write_json`。原子写入是明确的可靠性变化：按规范化路径提供同进程写锁、以等待者计数回收锁条目，在目标同目录创建临时文件，完成 flush/fsync 后以 `os.replace` 替换；序列化、文件描述符、fsync 或替换失败时均清理临时文件并保留旧目标。12 项专项测试覆盖缺失/损坏 JSON、输出字节契约、序列化/替换/fsync/描述符故障、异常后锁释放、锁规范化与回收；Windows 因文件共享语义跳过 POSIX 并发读取项，服务器 Linux 全部执行。`1c630be`（1.3B）服务器镜像 `sha256:55ca43a5465e62080ceae41e7861ba7015a7b2125cb4da7d4a03d4a1e7926365` 只让 `web_app.py` 显式导入原语，AST 终审确认 `read_json` 调用 47、`atomic_write_json` 调用 10、旧 `read_json`/`write_json` 定义和 `write_json` 调用均为 0、`json.dump` 为 0；唯一 Amazon `result_path.write_text(json.dumps(...))` 非 helper 写入仍为 1 并保持不动。两个阶段均通过 **40 项确定性回归 + 2 个 Playwright = 42 项**，失败数为 0；12 个活动页面均为 200，4002/4003/4004 健康检查均通过，容器日志异常匹配为 0，provider 与 FastMoss 边界回归全绿。1.3C 完成只读审计后默认不迁移 `ChatStore`：其无末尾换行、debounce、双锁、迁移与异常日志契约尚无严格等价证明，继续保留领域实现。

**Phase 1 整体验收：已完成。** HTTP smoke 与 Phase 0 测试矩阵全绿；最终部署后 `test_web_workflow_lifecycle.py` 连续独立执行两次均通过，确认临时根目录可重复运行；`web_app.py` 仅通过显式 import 使用 `core/http.py`、`core/config.py` 和 `core/json_store.py` 三个 core 模块；对外响应快照无非规范化差异。后续工作从 Phase 2.1 开始，不提前创建业务 routes/services。

## 六、Phase 2：建立无业务状态的路由骨架

先建立一个很薄的 stdlib 路由注册表，但本阶段只迁移健康检查、纯页面和静态资源，不提前搬 Shop/Metrics/Amazon 等依赖任务全局状态的业务路由：

```python
router.get("/healthz", healthz)
router.get("/report", report_page)
```

Router 只负责 method/path 匹配、path 参数和 404/405；不得拥有业务 store、job 字典、线程、数据库连接或 provider 配置。注册函数接收 handler 或显式依赖，不得导入 `web_app`。`web_app.py` 作为 composition root 创建 router 并保留 fallback，过渡期只允许 `web_app → routes`。

按以下子阶段实施，每个子阶段独立提交并执行当前登记的 42 脚本门禁：

1. **2.1 Router 匹配与冲突测试：** 新增 GET/POST/DELETE、精确路径、参数路径、404、405、重复注册和注册顺序测试，不接入运行时。
2. **2.2 health 与纯页面：** 先迁移 `/healthz`，再迁移只读取静态模板的页面入口；每迁移一组都用路由表快照确认 URL、method 和 handler 唯一。
3. **2.3 静态资源：** 只迁移资源定位和 content type 映射，Range/附件/视频仍使用 `core/http.py` 已冻结的 helper；不得把文件系统根目录重新推导一遍。

**Phase 2 验收：** 新增一个纯页面或静态资源路由不需要编辑 `Handler.do_GET`/`do_POST`；CodeGraph 不存在 `routes → web_app`；原 URL、状态码、Content-Type、缓存 header 和 404/405 行为不变。业务 API 仍留在原位置，等待 Phase 3 的任务边界稳定后按垂直切片迁移。

## 七、Phase 3：任务模型与注册表归一

本阶段提前到业务路由迁移之前，避免新 route/service 反向访问 `web_app.py` 中的 `download_jobs`、`shop_jobs`、`metrics_jobs`、`amazon_jobs` 和四把锁。不要先用继承强行统一四类任务；先定义稳定的只读快照协议：

```python
class JobSnapshot(TypedDict):
    id: str
    status: str
    created_at: float
    updated_at: float
    log: list[str]
    error: str
```

按以下子阶段实施，每个子阶段独立提交并执行当前登记的 42 脚本门禁：

1. **3.1 快照协议：** 为四类任务分别增加纯 `snapshot()` adapter，逐字段锁定现有 API/SSE 输出；业务专属字段继续由各 adapter 显式补充，不要求共享 dataclass 基类。
2. **3.2 `JobRegistry`：** 统一锁内注册、查找、日志追加、状态读取和不可变快照；测试并发 append/snapshot、任务不存在和异常后锁释放。Registry 不启动线程、不执行业务命令、不持久化数据库。
3. **3.3 调用方切换：** 四类任务逐域切换 registry，每切换一类都验证 API 查询与 SSE 字节契约。SSE 端点只消费快照，不直接访问可变任务对象。
4. **3.4 基类复核：** 只有发现除快照字段外还有稳定共享行为才评估基类；若只是少量字段复用，明确记录“不引入继承”。

**Phase 3 验收：** `web_app.py` 不再直接维护四套 job 字典/锁；四类任务的 API/SSE 输出与基线逐字段一致；并发测试无死锁、丢日志或可变对象泄漏；`jobs` 不导入 route/service/web_app。

## 八、Phase 4：按业务垂直切片迁移 service 与 route

本阶段补齐旧计划缺失的 services 实施步骤。每个垂直切片必须同时交付 `services/<domain>.py`、`routes/<domain>.py`、领域 contract test 和完整门禁；禁止只搬 route 后继续调用 `web_app` 全局函数。

路由 handler 只允许做四件事：读取并校验 HTTP 参数、调用 service、把已登记的领域错误映射为 HTTP、写响应。Service 拥有业务编排但不认识 `BaseHTTPRequestHandler`，不写 HTTP header，不导入 route 或 web_app。跨域共享只能依赖 `jobs/core` 或已有稳定领域接口。

迁移顺序：

1. **4.1 Shop：** 任务创建、命令编排、状态/结果读取和 SSE adapter。
2. **4.2 Metrics：** 保持 endpoint/target 校验和结果注册语义。
3. **4.3 Amazon：** 保持 scraper 容器生命周期、输出目录和错误映射。
4. **4.4 下载与分析：** 下载、上传、分析、翻译、postprocess 分开迁移，不能合成新的万能 VideoService。此阶段只把 `tools.py` 中与视频子进程执行直接相关的能力迁入 analyzer service，并保留活动调用方所需的窄 facade；聊天工具归一与权限逻辑留到 Phase 6。
5. **4.5 日报 web adapter：** 只迁移 HTTP/调度适配，核心继续留在 `hot_video_report.py`；显式 `max_tokens`、resume、候选备份和单视频 cache 规则不变。
6. **4.6 邻聊与淘宝：** 先冻结全局用户隔离、文件传输和写操作授权，再迁移；不得顺手改变账号模型。

代理和聊天因各自具有外部进程/安全边界，分别留到 Phase 5、Phase 6，不在 Phase 4 建立临时兼容 route。

**Phase 4 验收：** 上述业务 API 不再编辑巨型 `do_GET`/`do_POST`；CodeGraph 满足 `web_app → routes → services → jobs/core`，不存在 `routes/services → web_app` 或 `services → routes`；每个域的 URL、状态码、JSON 字段、SSE 帧、数据目录和任务生命周期与 Phase 0 基线一致。

## 九、Phase 5：拆分代理子系统

`proxy_pool.py` 不应与普通 web service 一起大搬。按事务边界拆，并在每个子阶段执行当前登记的 42 脚本门禁及代理专项故障注入：

1. `proxy/repository.py`：schema、migration、查询、事务函数。
2. `proxy/nodes.py`：VLESS/VMess/static/direct 解析、端口作用域与序列化。
3. `proxy/runtime.py`：mihomo/sing-box 配置生成、启动、检查和清理。
4. `proxy/accounts.py`：账号、代理绑定、会话和预检。
5. `proxy/publishing.py`、`proxy/collection.py`：任务状态机。
6. `routes/proxy.py`：最后接入已稳定的 proxy facade，不直接操作 SQLite 或外部进程。

关键不变量：

- 删除代理池必须在一个受控流程内完成：检查活动会话 → 解绑账号 → 暂停等待任务 → 清理运行时。
- “等待代理”的精确定义为 `status='delayed'` 且 `stage='waiting_proxy'`；重新绑定只恢复该账号的这类任务为 `status='queued'`、`stage='proxy_rebound'`，并返回发布/采集恢复数量。
- 数据库提交和外部进程操作失败时必须有明确补偿/重试状态，不能静默半成功。
- TikTok 与 Instagram 登录/采集状态不能混用；`port_scope` 不能丢失。

迁移数据库前必须复制隔离 fixture，验证旧 schema 升级、重复执行 migration、失败回滚和升级后读取。故障注入至少覆盖：运行时配置清理失败、数据库提交失败、运行时重启失败；每种情况都要断言账号绑定、任务状态和代理配置三者最终一致或进入可重试的明确状态。

**Phase 5 验收：** `test_proxy_pool_lifecycle.py` 全绿；用合成账号/代理 seed 在 `/proxy` 完成删池、未绑定、重新绑定、任务恢复。浏览器固定验证 1440×900 和 390×844 两个 viewport，保存 DOM 契约、控制台错误和截图，测试后删除 seed 数据；`proxy` 包和 route 均不导入 `web_app`。

## 十、Phase 6：聊天、LLM 与聊天路由归一

### 10.1 聊天边界

保持 `chat.html` 为 Home、SellerSprite、出海匠的唯一共享壳。拆分后仍必须满足：

- provider 规范化和 public/internal session ID 转换只有一个实现。
- tool schema 暴露和执行前都复核官方 Skill 白名单。
- `sellersprite__*`、`chuhaijiang__*`、`sociavault__*` 等工具域不能交叉泄漏。
- `officialPresetId` 仅属于当前请求，不能持久化或污染下一次自由聊天。
- `routes/chat.py` 最后接入稳定的 provider/tool/service 接口，不直接读取 MCP 进程字典或修改 session 内部对象。
- `tools.py` 中剩余的聊天工具归一、schema 和执行入口按契约迁入 `chat/tool_gateway.py`；只有活动调用方全部切换后才能删除 facade，不以“文件变小”为目的重写工具行为。

### 10.2 LLM transport

不采用旧计划中单一 `call_llm(prompt, ...)` 覆盖所有调用的方案。统一的应是传输层能力：认证、URL、超时、重试、错误标准化、usage 提取；各调用方继续保留消息结构、system prompt、tools、response format、视觉输入和业务解析。

先为现有 DeepSeek/Qwen 调用补 contract test，再引入 transport adapter。`hot_video_report.py` 保持显式 `max_tokens`，不能依赖隐式默认值。每个 transport/provider/chat 子阶段仍执行当前登记的 42 脚本门禁，专项测试不能替代完整门禁。

Transport 规则必须显式化：只对连接失败、429 和可重试 5xx 在首个响应字节前重试；次数和退避由调用方配置；流式输出开始后不得自动重放；带副作用的工具调用不得由 transport 重试；错误类型和 usage 合并规则对调用方保持兼容。请求快照必须删除认证头、Cookie、真实媒体 URL 和用户内容，只保留合成 fixture。

**Phase 6 验收：** 三 provider 工具边界测试、日报 LLM fallback、翻译、postprocess、direct-video 分别通过；请求 payload 与基线快照一致；聊天 route 不导入 `web_app`，provider 工具域、会话作用域和官方 Skill 白名单继续 fail closed。

## 十一、Phase 7：前端资源拆分

后端 API 稳定后再处理 `proxy.html`：

- HTML 保留语义结构。
- CSS 迁入独立、版本化资源。
- 数据请求、状态 store、drawer workflow 分成小型原生 JS 模块。
- 共享导航继续由 `ui-system.css/js` 提供。

静态资源变化必须更新 `UI_ASSET_VERSION`。不得借重构恢复任何已退役 provider 页面或拆出三套聊天壳。每个可独立部署的资源拆分子阶段执行当前登记的 42 脚本门禁，并检查两个 Playwright 的桌面/移动 viewport、控制台和页面错误。

**Phase 7 验收：** 代理页桌面/窄屏浏览器回归；无控制台错误；所有写操作仍有确认、禁用和错误反馈。

## 十二、提交和验证规范

每个重构提交都必须满足：

1. `git status --short --branch` 明确且不包含他人改动。
2. 仓库存在 `.codegraph/` 时，先运行 `codegraph sync .`，再用 `codegraph explore "<待迁移符号、调用方和相关测试>"` 检查影响面；不存在时用 `rg` 完成等价清单。
3. 先复制测试保护，再移动代码；不在同一提交顺手修业务。
4. `git diff --check` 通过。
5. Windows 工作树只做静态检查和提交，不在本地构建 Docker。源码必须先通过 GitHub 同步，再在服务器 4004 checkout 中构建和验证：

```powershell
Set-Location 'C:\Users\admin\Documents\Video_analyzer-ui-4004'
if ((git branch --show-current) -ne 'v2') { throw '必须在 v2 分支执行' }
git merge-base --is-ancestor b364276 HEAD
if ($LASTEXITCODE -ne 0) { throw '当前 HEAD 尚未包含 V2 对齐提交 b364276' }
git -c http.proxy=http://127.0.0.1:7892 -c https.proxy=http://127.0.0.1:7892 push origin v2
```

4004 部署唯一入口是 `scripts/deploy_ui_4004.sh`：它默认且只允许分支 `v2`、项目名 `short-video-analyzer-ui-4004`、端口 `4004` 和基础 `docker-compose.yml`。当前 V2 禁止使用 overlay；任何 legacy preview 开关只要非 `0` 必须直接拒绝部署，不得回退到 preview/overlay Compose 配置。当前服务器安装的是 legacy `docker-compose`，不是 Compose v2 插件；下列为脚本采用的基础构建与服务器验证命令，不得替代该部署入口：

```bash
cd /home/openclaw/Video_analyzer-ui-4004
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 pull --ff-only origin v2
docker-compose -p short-video-analyzer-ui-4004 build web
docker-compose -p short-video-analyzer-ui-4004 run --rm --no-deps \
  -e UI_TEST_MODE=1 -e APP_TEST_ROOT=/tmp/v2-smoke \
  -e APP_TEST_PORT_FILE=/tmp/v2-smoke/web.port \
  -e HOT_VIDEO_REPORT_ENABLED=0 -e PROXY_POOL_ENABLED=0 analyzer \
  python scripts/test_web_smoke.py
```

6. 页面变更在 4004 隔离端口验证，不构建、停止或部署 4002/4003。
7. 一个提交只迁移一个稳定边界，可独立回退。

## 十三、阶段门禁与完成定义

```text
Phase 0 测试基线
  → Phase 0.5 退役 provider 零兼容清理
    → Phase 1 HTTP/config/store
      → Phase 2 无业务状态路由骨架
        → Phase 3 任务快照与注册表
          → Phase 4 业务 service/route 垂直切片
            → Phase 5 代理子系统
              → Phase 6 聊天、LLM 与聊天路由
                → Phase 7 前端资源
```

最终完成条件：

- `web_app.py` 只保留 composition root、Handler/Router 组装、明确的过渡期兼容导出和启动序列；不以机械行数作为完成门禁。小于 800 行只作为拆分结果的观察指标，不能为了达标制造 facade 转发层或无业务边界的小文件。
- `proxy_pool.py` 被兼容 facade 替代，核心逻辑进入 `proxy/` 且事务边界清晰。
- `tools.py` 只在视频执行器和聊天 tool gateway 分别完成迁移后删除或缩成有明确调用方的窄 facade。
- 新增业务域不再修改巨型 `do_GET`/`do_POST`。
- 三 provider、日报、代理、视频和邻聊各有独立测试门禁。
- 4004 活动功能的 URL、API schema、SSE 格式、数据目录和 Compose 隔离保持兼容；已退役 provider 不属于兼容范围。
- `core` 不包含 provider/tool/业务实体专属规则；provider adapter 注入的规则必须全部属于活动注册表。
- CodeGraph 中不存在 `routes/services/jobs/core → web_app`、`services → routes` 或 `core → 领域模块` 的反向依赖。

## 十四、下一批实施任务

Phase 0、0.5、1.1、2026-08-29 两个补漏阶段、Phase 1.2 与 Phase 1.3 已完成。下一步只实施 **Phase 2.1 Router 匹配与冲突测试**，不提前创建业务 routes/services。

完成 Phase 1 整体验收后，下一批只能先做 Phase 2.1，再按顺序推进 Phase 2 的 health/page/static 骨架与 Phase 3 JobRegistry；不得跳过 Phase 3 直接迁移 Shop/Metrics/Amazon 业务路由。每个子阶段仍遵循 GitHub 同步、服务器 4004 构建、40 项确定性回归、部署、2 项 Playwright、日志扫描和 4002/4003 只读健康检查。
