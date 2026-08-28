# V2 解耦化与归一化重构执行计划

初版日期：2026-08-01
复核日期：2026-08-28
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
9. 活动聊天 provider 收紧为 Home、SellerSprite、出海匠；当前部署仍有的 `/fastmoss*` 307 重定向只是待清理遗留，Phase 0 不保留该路由。代理页已补齐共享 `.ui-header` 契约。

4004 隔离要求保持不变：Compose 项目、sing-box 项目、测试端口和服务操作都不得落到 4002/4003。`0232f9f` 已通过 GitHub 同步并部署到 `/home/openclaw/Video_analyzer-ui-4004`；部署后 4002、4003、4004 的 `/healthz` 均返回 200。

## 二、当前代码总览

CodeGraph 已在 `0232f9f` 上重新同步。主要热点如下：

| 文件 | 规模 | 当前职责 | 判断 |
| --- | ---: | --- | --- |
| `scripts/web_app.py` | 15,535 行 / 735 KB | 配置、页面装配、聊天 provider、HTTP 工具、四类临时任务、下载/店铺/指标/Amazon、全部 GET/POST 路由、后台线程启动 | 第一重构对象，但必须分批拆 |
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
  core/
    config.py                # 路径与环境配置的唯一来源
    http.py                  # response、请求体、SSE、静态文件
    json_store.py            # 原子 JSON 读写等通用持久化
  jobs/
    model.py                 # 稳定的任务快照协议
    registry.py              # 锁、注册、日志、状态读取
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

强制 import 方向：`web_app` 可导入 `routes`；`routes` 可导入 `services/chat/proxy/jobs`；领域模块可导入 `core`。反向 import 一律禁止。

```text
web_app → routes → services/chat/proxy/jobs → core
```

`web_app.py` 可以在过渡期显式 re-export 兼容符号，但禁止 `from ... import *`。每个兼容导出都必须有调用方和删除条件。

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

2026-08-28 功能对齐完成后的门禁状态如下：

| 检查 | 当前结果 | 重构前处理 |
| --- | --- | --- |
| `test_ui_contract.py` | 15 项通过 | 保持为共享壳和代理页 DOM 门禁 |
| `test_chuhaijiang_ui_contract.py` | 7 项通过 | 保持 Home 0 个旧 scene、出海匠 8 个官方 scene 的契约 |
| `test_chuhaijiang_boundary.py` | 5 项通过 | 保持活动 provider 与工具域隔离门禁 |
| `test_proxy_pool_lifecycle.py` | 通过 | 保持端口迁移、删池解绑、重新绑定和 sing-box 检测门禁 |
| `test_27_presets_mock_boundary.py` | 27 个 SellerSprite 官方预设通过 | mock 开关只注入测试子进程，生产默认仍为 0 |
| `test_chat_tool_normalization.py` | 仍是 V1/V2 混合套件；包含已退役 FastMoss provider 断言 | 按活动 provider 拆出 V2 套件；删除全部旧 FastMoss 断言，不新增兼容套件 |

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

2026-08-28 执行状态：

| 阶段 | 状态 | 阶段出口 |
| --- | --- | --- |
| Phase 0 测试基线 | 已完成 | 服务器 4004 确定性套件、HTTP smoke、桌面/移动浏览器回归全绿 |
| Phase 0.5A 共享能力中性化 | 已完成 | SellerSprite semantic renderer 输出保持一致；服务器 4004 全量回归全绿 |
| Phase 0.5B 运行时和仓库资产清理 | 已完成 | 运行时、Bridge/配置、UI/文档三条工作线合并后零残留扫描通过；补齐无外网的核心 workflow HTTP 生命周期 fixture；服务器 4004 全量回归全绿 |
| Phase 0.5C V2 应用数据清理 | 已完成 | `data/`、`data-dev/`、历史 session/cache/Skill 和旧 `.pyc` 已移出 V2 项目树；项目外备份逐文件校验通过；镜像、运行容器和服务器 4004 再次全量回归全绿 |
| Phase 1.1 `core/http.py` | 已完成 | 五个响应/SSE helper 已迁入纯 stdlib 模块；字节级与黑盒 HTTP 契约已补齐；两次独立服务器全量门禁均全绿 |

Phase 0.5B 可以多智能体并行，但文件所有权必须互斥：一条线负责 Python 运行时与专用模块，一条线负责 MCP Bridge/Compose/env，一条线负责静态 UI 与受控文档；README、计划文档、资产版本、跨线冲突和最终集成由主任务统一处理。子智能体只运行专项测试，不得独立提交；主任务合并审计后统一提交和部署。

“每个阶段全功能回归”定义为以下集合，适用于 Phase 0.5B、0.5C，以及 Phase 1～6 中每个会改变运行时的子阶段；专项测试通过不能代替该集合：

1. Windows 工作树完成 `git diff --check`、Python/Node 语法检查、依赖边界检查和仓库零遗留扫描；本地不构建 Docker。
2. 源码提交并通过 GitHub 同步到 `/home/openclaw/Video_analyzer-ui-4004`，服务器使用 `docker-compose -p short-video-analyzer-ui-4004 build web` 重新构建镜像。
3. 在新镜像中运行登记的全部确定性 Python 套件、带显式功能开关的专项套件和 Node Bridge 套件；不只运行本阶段改动对应的测试。
4. 部署后检查 `/healthz`、全部活动页面和 API smoke；未注册路径以及已退役路径必须是无 `Location` 的通用 404。
5. 桌面与移动视口分别运行聊天滚动和邻聊上传队列 Playwright 回归，检查浏览器控制台和页面错误。
6. 检查 4004 容器环境、生效 Compose 配置、进程和近期日志；不得出现退役 provider、启动失败或触碰 4002/4003 的证据。
7. 记录镜像 ID、提交 SHA、测试总数和失败数。任一项失败即留在当前阶段修复并重跑完整集合，不得带红灯进入下一阶段。

固定计数口径：Phase 0.5 先在 4004 服务器新镜像中运行 **36 项构建前确定性回归**，即 32 个常规 Python、`test_hot_report_resume.py`、启用 `SELLERSPRITE_TOOL_MOCK_MODE=1` 的 `test_27_presets_mock_boundary.py`，以及 `scripts/test_mcp_bridge_cache.js`、`sellersprite_mcp_chat/test_stdio_mcp_client.js` 两个 Node 门禁；部署后再运行 **2 个 Playwright 脚本**，合计 38 个自动化脚本。Phase 1.1 新增 `test_core_http.py` 和 `test_http_response_contract.py` 后，后续阶段的固定门禁上调为 **38 项构建前确定性回归 + 2 个部署后 Playwright = 40 个自动化脚本**。两个 Playwright 都覆盖桌面和移动 viewport。`test_api.py` 是吞异常的固定历史数据探针，`test_low_reasoning_video_insight.py` 会调用付费外部模型且失败仍返回成功，二者只作为人工实验，不计入阶段门禁。

Phase 0.5 最终证据（2026-08-28）：源码清理提交为 `0618559`，回归夹具修复为 `bd864cd`、`e362e7b`，递归排除历史 Python 字节码的构建修复为 `d1d4d2c`；服务器部署镜像为 `a70c61cd2e9f`。最终 36 项构建前回归失败数为 0，两个部署后 Playwright 均通过；`/healthz` 和 12 个活动页面为 200，旧路径 GET/POST/DELETE 均为无 `Location` 的 404。V2 项目树、镜像、容器环境、生效 Compose 和进程扫描均无旧 provider 残留。项目外回滚备份覆盖 11 组、128 个文件、3,788,317 字节，manifest SHA-256 为 `7f1c552462b943e733424e1f32c1a952a162256d3d247696b23dfdc70c61a91f`；备份保留原权限，未来恢复其中 root 文件时需要 `sudo`。

## 五、Phase 1：抽取低风险基础设施

每个子步骤独立提交，保持行为不变。

### 5.1 `core/http.py`

Phase 1.1 只迁移 `json_response`、`text_response`、`binary_response`、`file_response`、`write_sse_event` 五个稳定响应/SSE helper。路由和请求体读取仍留在 `web_app.py`；不得在本阶段移动 `_lan_chat_request_json`、multipart/JSON body 解析、静态资源/视频/附件服务、SSE 生命周期或任何业务异常映射。

按以下互斥边界实施，每个运行时提交后都必须执行本节上方完整的 38 脚本门禁：

1. **1.1A 新模块与字节级单测：** 只拥有 `scripts/core/http.py`、`scripts/core/__init__.py` 和新建的纯 helper 单测。使用 `FakeHandler`/`BytesIO` 锁定 JSON UTF-8 与缩进、no-cache、HEAD、Range 206/416、RFC 5987 文件名、1 MiB 流式读取、BrokenPipe 行为以及 SSE `data:` 帧和 flush；不导入 `web_app.py`，不产生循环依赖。
2. **1.1B 调用方替换：** 单独拥有 `scripts/web_app.py`，显式导入上述五个 helper 并逐个替换原定义。AST 终审确认真实直接调用量为 278 个 JSON、13 个 text、12 个 binary、6 个 file 和 4 个 SSE，共 313 个；状态码、headers、异常捕获和 Handler 签名必须保持不变，不改路由条件和业务分支。
3. **1.1C HTTP 黑盒契约：** 单独拥有 HTTP contract 测试文件，补齐当前 smoke 未锁定的 Range、Content-Disposition、HEAD、SSE 帧和断连行为。它可以与 1.1A 并行准备，但必须在 1.1A 合入后执行，在 1.1B 合入后作为端到端回归再次执行。
4. **1.1D 只读交叉审计：** 用 CodeGraph 核对调用数、导入方向和遗漏定义；不得修改 A/B/C 拥有的文件。主任务统一提交、GitHub 同步、服务器构建和部署。

验收：新增字节级单测、HTTP smoke、workflow lifecycle、SSE 专项、未注册/禁用功能 404 分支通过，且当前完整 40 脚本门禁全绿。

Phase 1.1 最终证据（2026-08-28）：`6d7a52b` 新增纯 stdlib `scripts/core/http.py`、12 项字节级单测和 HTTP 黑盒契约，`03827ac` 只在 `web_app.py` 增加一次显式导入并删除五个原定义。AST 验收为旧 FunctionDef 0、导入 1、313 个直接调用数保持不变；CodeGraph 已在最终代码上重新同步。A/C 集成提交和 B 调用方切换提交分别在服务器新镜像执行 38 项构建前回归，均为 0 失败；最终部署镜像 `fc57b7755c31` 的 `/healthz`、12 个活动页面、旧路径 404、两个桌面/移动 Playwright 全部通过，4002/4003 健康检查仍为 200。部署日志无 import、语法或启动失败；Playwright 导航取消产生的两条 `BrokenPipeError` 为抽取前既有的客户端断连行为，本阶段按行为等价要求保留，后续若要静默应作为独立可靠性改动并单独回归。

### 5.2 `core/config.py`

归一 `ROOT`、`SCRIPTS_DIR`、`DATA_DIR`、`VIDEOS_DIR`、`OUTPUT_DIR` 和布尔/整数环境变量解析。配置对象必须可在测试中显式构造，避免测试依赖 import 时的宿主环境。

验收：不同临时数据目录可在同一测试进程中运行；4004 Compose 默认值不回落到正式项目。

### 5.3 `core/json_store.py`

只归一原子 JSON 读写、文件锁和安全替换；SQLite 事务仍留在各领域 repository 中，不做“通用数据库层”。

验收：覆盖临时文件原子替换失败、并发读写、损坏 JSON 的显式报错、异常后的锁释放；确认没有 SQLite 调用迁入该模块。

**Phase 1 整体验收：** HTTP smoke 与 Phase 0 测试矩阵全绿；临时根目录可重复运行；`web_app.py` 只通过显式 import 使用三个 core 模块；对外响应快照无非规范化差异。

## 六、Phase 2：稳定路由边界

先建立一个很薄的 stdlib 路由注册表，再逐域迁移：

```python
router.get('/healthz', healthz)
router.post('/api/report/run', run_report)
```

路由 handler 只允许做四件事：读取参数、调用 service、映射错误、写响应。业务状态修改不得继续写在 `do_GET`/`do_POST` 分支中。

迁移顺序：

1. 健康检查、静态资源和纯页面路由。
2. Shop、Metrics、Amazon 等边界较清晰的域。
3. 视频下载与分析。
4. 日报 web 适配层。
5. 邻聊和淘宝。
6. 代理路由。
7. 聊天路由最后迁移。

聊天最后迁移的原因是它同时承载 provider 作用域、官方 Skill、工具白名单、流式响应和 session 兼容，影响面最大。

**Phase 2 验收：** 新增一个路由不需要编辑 `Handler.do_GET`/`do_POST`；原 URL、状态码、JSON 字段和 SSE 格式不变。

## 七、Phase 3：任务模型归一

不要先用继承强行统一 `DownloadJob`、`ShopJob`、`MetricsJob`、`AmazonJob`。先定义稳定的只读快照协议：

```python
class JobSnapshot(TypedDict):
    id: str
    status: str
    created_at: float
    updated_at: float
    log: list[str]
    error: str
```

步骤：

1. 为四类任务补齐一致的 `snapshot()` 行为和并发测试。
2. 抽 `JobRegistry` 统一锁、查找、日志追加和状态读取。
3. 各业务任务保留自己的字段和状态机。
4. 最后评估是否需要 dataclass 基类；若只是少量字段复用，可不引入继承。

SSE 端点只消费快照，不直接访问可变任务对象。

**Phase 3 验收：** 四类任务的 API/SSE 输出与基线逐字段一致；并发读写测试无死锁和丢日志。

## 八、Phase 4：拆分代理子系统

`proxy_pool.py` 不应与普通 web service 一起大搬。按事务边界拆：

1. `proxy/repository.py`：schema、migration、查询、事务函数。
2. `proxy/nodes.py`：VLESS/VMess/static/direct 解析、端口作用域与序列化。
3. `proxy/runtime.py`：mihomo/sing-box 配置生成、启动、检查和清理。
4. `proxy/accounts.py`：账号、代理绑定、会话和预检。
5. `proxy/publishing.py`、`proxy/collection.py`：任务状态机。

关键不变量：

- 删除代理池必须在一个受控流程内完成：检查活动会话 → 解绑账号 → 暂停等待任务 → 清理运行时。
- “等待代理”的精确定义为 `status='delayed'` 且 `stage='waiting_proxy'`；重新绑定只恢复该账号的这类任务为 `status='queued'`、`stage='proxy_rebound'`，并返回发布/采集恢复数量。
- 数据库提交和外部进程操作失败时必须有明确补偿/重试状态，不能静默半成功。
- TikTok 与 Instagram 登录/采集状态不能混用；`port_scope` 不能丢失。

迁移数据库前必须复制隔离 fixture，验证旧 schema 升级、重复执行 migration、失败回滚和升级后读取。故障注入至少覆盖：运行时配置清理失败、数据库提交失败、运行时重启失败；每种情况都要断言账号绑定、任务状态和代理配置三者最终一致或进入可重试的明确状态。

**Phase 4 验收：** `test_proxy_pool_lifecycle.py` 全绿；用合成账号/代理 seed 在 `/proxy` 完成删池、未绑定、重新绑定、任务恢复。浏览器固定验证 1440×900 和 390×844 两个 viewport，保存 DOM 契约、控制台错误和截图，测试后删除 seed 数据。

## 九、Phase 5：聊天与 LLM 归一

### 9.1 聊天边界

保持 `chat.html` 为 Home、SellerSprite、出海匠的唯一共享壳。拆分后仍必须满足：

- provider 规范化和 public/internal session ID 转换只有一个实现。
- tool schema 暴露和执行前都复核官方 Skill 白名单。
- `sellersprite__*`、`chuhaijiang__*`、`sociavault__*` 等工具域不能交叉泄漏。
- `officialPresetId` 仅属于当前请求，不能持久化或污染下一次自由聊天。

### 9.2 LLM transport

不采用旧计划中单一 `call_llm(prompt, ...)` 覆盖所有调用的方案。统一的应是传输层能力：认证、URL、超时、重试、错误标准化、usage 提取；各调用方继续保留消息结构、system prompt、tools、response format、视觉输入和业务解析。

先为现有 DeepSeek/Qwen 调用补 contract test，再引入 transport adapter。`hot_video_report.py` 保持显式 `max_tokens`，不能依赖隐式默认值。

Transport 规则必须显式化：只对连接失败、429 和可重试 5xx 在首个响应字节前重试；次数和退避由调用方配置；流式输出开始后不得自动重放；带副作用的工具调用不得由 transport 重试；错误类型和 usage 合并规则对调用方保持兼容。请求快照必须删除认证头、Cookie、真实媒体 URL 和用户内容，只保留合成 fixture。

**Phase 5 验收：** 三 provider 工具边界测试、日报 LLM fallback、翻译、postprocess、direct-video 分别通过；请求 payload 与基线快照一致。

## 十、Phase 6：前端资源拆分

后端 API 稳定后再处理 `proxy.html`：

- HTML 保留语义结构。
- CSS 迁入独立、版本化资源。
- 数据请求、状态 store、drawer workflow 分成小型原生 JS 模块。
- 共享导航继续由 `ui-system.css/js` 提供。

静态资源变化必须更新 `UI_ASSET_VERSION`。不得借重构恢复任何已退役 provider 页面或拆出三套聊天壳。

**Phase 6 验收：** 代理页桌面/窄屏浏览器回归；无控制台错误；所有写操作仍有确认、禁用和错误反馈。

## 十一、提交和验证规范

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

服务器使用 `/home/openclaw/Video_analyzer-ui-4004`、分支 `v2` 和项目名 `short-video-analyzer-ui-4004`。当前服务器安装的是 legacy `docker-compose`，不是 Compose v2 插件：

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

## 十二、阶段门禁与完成定义

```text
Phase 0 测试基线
  → Phase 0.5 退役 provider 零兼容清理
    → Phase 1 HTTP/config/store
      → Phase 2 路由边界
        → Phase 3 任务注册表
          → Phase 4 代理子系统
            → Phase 5 聊天与 LLM
              → Phase 6 前端资源
```

最终完成条件：

- `web_app.py` 只保留兼容入口、Handler 组装和启动序列，目标小于 800 行；小于 500 行不是硬指标。
- `proxy_pool.py` 被兼容 facade 替代，核心逻辑进入 `proxy/` 且事务边界清晰。
- 新增业务域不再修改巨型 `do_GET`/`do_POST`。
- 三 provider、日报、代理、视频和邻聊各有独立测试门禁。
- 4004 活动功能的 URL、API schema、SSE 格式、数据目录和 Compose 隔离保持兼容；已退役 provider 不属于兼容范围。
- CodeGraph 中不存在 `routes → web_app`、`service → routes` 的反向依赖。

## 十三、建议的第一批实施任务

第一批只完成 Phase 0/0.5，不进入 `core/http.py`。建议拆成 5～7 个可独立回退的小提交：

1. 新增可重复运行的 `test_web_smoke.py`，冻结活动页面、通用未注册路由 404 和功能开关契约。
2. 拆分 `test_chat_tool_normalization.py`：只保留 Home、SellerSprite、出海匠套件，删除全部旧 FastMoss 断言和专用测试。
3. 为活动 provider catalog、SellerSprite semantic renderer/Bridge、通用未知 provider/tool fail-closed 补快照与门禁。
4. 把 SellerSprite 和其他域仍调用的共享 renderer/helper 迁到中性命名模块，保持输出不变。
5. 删除 `web_app.py`、planner、Bridge、Compose/env、前端、Skill、专用模块与文档中的全部 FastMoss 逻辑，不保留路由或 API 兼容。
6. 通过 GitHub 同步到服务器，在项目外备份后删除 V2 应用目录中的旧会话/cache/Skill 数据，然后重建且验证 4004。
7. 运行全仓零遗留搜索、三 provider 专项测试、HTTP smoke 和服务日志检查；全绿后才开始 Phase 1.1。

第二批才抽 `core/http.py` 并为禁用日报、禁用代理的 404/提示分支加契约测试。第一批不触碰代理事务、Job 继承或前端资源拆分；聊天代码只处理退役 provider 删除和为保护活动 provider 所必需的中性化迁移。
