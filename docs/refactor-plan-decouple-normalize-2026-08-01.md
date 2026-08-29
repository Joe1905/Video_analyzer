# V2 解耦化与归一化重构执行计划

初版日期：2026-08-01
复核日期：2026-08-29
适用环境：`v2` / 4004 / `short-video-analyzer-ui-4004`
技术路线：保留 stdlib `http.server`，先建立行为基线，再按稳定边界拆分
执行原则：同一提交只做“行为修复”或“结构搬迁”中的一种
执行要求：每个阶段开始前必须完整重读 docs/refactor-execution-requirements.md；该文件优先于本计划

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
9. 活动聊天 provider 收紧为 Home、SellerSprite、出海匠；代理页已补齐共享 `.ui-header` 契约。

4004 隔离要求保持不变：Compose 项目、sing-box 项目、测试端口和服务操作都不得落到 4002/4003。`0232f9f` 已通过 GitHub 同步并部署到 `/home/openclaw/Video_analyzer-ui-4004`；部署后 4002、4003、4004 的 `/healthz` 均返回 200。

## 二、当前代码总览

CodeGraph 已在当前运行时代码 `46dab34` 上重新同步。主要热点如下：

| 文件 | 规模 | 当前职责 | 判断 |
| --- | ---: | --- | --- |
| `scripts/web_app.py` | 13,206 行 / 608,465 字节（约 594.20 KiB） | 配置、页面装配、聊天 provider、四类临时任务、下载/店铺/指标/Amazon、遗留 GET/POST 路由、后台线程启动 | 第一重构对象，但必须分批拆；HTTP response/SSE helper、导入期配置、JSON 文件原语、health 与八个纯页面路由已完成抽取 |
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

`web_app.py` 可以在过渡期为活动功能显式 re-export 兼容符号，但禁止 `from ... import *`。每个兼容导出都必须有调用方和删除条件，且只能服务当前活动功能。

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
| `test_chat_tool_normalization.py` | 活动 provider、工具域与执行边界 | 仅保留当前活动域和通用 unknown fail-closed 契约，完整门禁通过 |

已知的运行时与 UI 红灯已经关闭。统一 HTTP smoke、活动 provider 契约、混合聊天套件拆分和浏览器回归已于 2026-08-28 完成并在服务器 4004 通过全量门禁。后续若测试矩阵新增红灯，不得笼统标记为“历史问题”后继续重构：每项必须落为活动 V2 套件中的 `pass`，或带有明确外部条件且不掩盖回归的 `skip`。

### 4.4 建立测试矩阵

按风险域维护最小矩阵：

- 聊天：provider 路由、会话隔离、官方 preset 白名单、工具执行前复核。
- 日报：暂停/恢复、单视频重试、缓存占位失效、LLM fallback、数据库生命周期。
- 代理：节点生命周期、删池解绑、重新绑定、发布/采集任务暂停与恢复。
- 视频：超时诊断、孤儿进程清理、direct-video prompt/压缩回退。
- UI：共享导航、三 provider 共享壳、代理绑定抽屉、日报禁用态。

2026-08-28 交叉审计确认，原矩阵对上传、下载、分析、翻译、后处理、店铺抽取和指标任务仍以页面 smoke/辅助函数测试为主，缺少无外网的 HTTP job 生命周期契约。Phase 0.5B 已新增 `scripts/test_web_workflow_lifecycle.py` 合成 fixture：覆盖请求校验、任务创建、状态查询、SSE 完成/失败结构、结果读取和临时目录清理；该测试禁止使用真实 API key、账号、额度、媒体发布、生产数据删除或外网抓取。页面返回 200 不能替代这些功能契约。

响应基线存放在 `scripts/contracts/`。快照生成时只允许规范化随机 ID、时间戳、临时绝对路径和日志时间前缀；字段缺失、状态码、重定向目标、SSE event/data 结构不得被归一掉。快照中只能使用合成数据，不得写入凭据、Cookie、真实账号或请求头。

### 4.6 当前执行状态、并行边界与阶段级回归门禁

截至 2026-08-29 的执行状态：

| 阶段 | 状态 | 阶段出口 |
| --- | --- | --- |
| Phase 0 测试基线 | 已完成 | 服务器 4004 确定性套件、HTTP smoke、桌面/移动浏览器回归全绿 |
| Phase 0.5A 共享能力中性化 | 已完成 | SellerSprite semantic renderer 输出保持一致；服务器 4004 全量回归全绿 |
| Phase 0.5B 运行时和仓库资产清理 | 已完成 | 运行时、Bridge/配置、UI/文档三条工作线合并后零残留扫描通过；补齐无外网的核心 workflow HTTP 生命周期 fixture；服务器 4004 全量回归全绿 |
| Phase 0.5C V2 应用数据清理 | 已完成 | `data/`、`data-dev/`、历史 session/cache/Skill 和旧 `.pyc` 已移出 V2 项目树；项目外备份逐文件校验通过；镜像、运行容器和服务器 4004 再次全量回归全绿 |
| Phase 0.5D 中性语义边界补漏 | 已完成 | 删除中性 renderer 中 7 项不可达指标规则和 3 个隐藏枚举值；SellerSprite boundary、工具专属审计字段和查询标题规则全部回归 provider adapter；服务器 4004 完整门禁全绿 |
| Phase 1.1 `core/http.py` | 已完成 | 五个响应/SSE helper 已迁入纯 stdlib 模块；字节级与黑盒 HTTP 契约已补齐；两次独立服务器全量门禁均全绿 |
| Phase 1.1R HTTP 断连可靠性补漏 | 已完成 | JSON/text 非流式响应仅在 body 写入阶段忽略客户端断连；序列化/header/SSE 异常语义保持不变；部署后 Playwright 日志零 traceback |
| Phase 1.2A `core/config.py` 模型与测试 | 已完成 | `6c9731d`；纯 stdlib 不可变配置模型及隔离构造测试通过，服务器完整门禁全绿 |
| Phase 1.2B 路径配置切换 | 已完成 | `64e63af`；单一 `APP_CONFIG` 接管根路径与测试根路径，服务器完整门禁全绿 |
| Phase 1.2C 其余导入期配置切换 | 已完成 | `2115c5f`；模块级 `os.getenv` 为 0，函数期动态读取为 56 且基线多重集合不变，服务器完整门禁全绿 |
| Phase 1.2R 工具目录、外部 provider 与部署边界漏洞收口 | 已完成 | `a30b494`、`afacd7c`；出海匠工具目录五域完整，未知外部 provider 与 4004 部署入口 fail-closed，服务器完整门禁全绿 |
| Phase 1.3A `core/json_store.py` 原子原语与故障注入 | 已完成 | `1f231c4`；纯 stdlib JSON 原语、12 项专项测试和同进程规范化路径写锁通过，服务器完整门禁全绿 |
| Phase 1.3B `web_app` helper 切换 | 已完成 | `1c630be`；47 个读、10 个原子写、旧 helper 定义 0 的 AST 契约通过，服务器完整门禁全绿 |
| Phase 1.3C `ChatStore` 只读复用审计 | 已完成（默认不迁移） | session 无末尾换行、debounce、双锁、迁移和异常日志的严格等价契约尚不足；保留领域实现 |
| Phase 1.R1 4004 镜像隔离漏洞收口 | 已完成 | `40d2b2e`、`4f978ea`；4004 强制专属镜像标签，通用 Compose 默认不漂移，新增宿主部署门禁并更新旧代理生命周期契约 |
| Phase 1.R2 Amazon 结果原子写收口 | 已完成 | `2f9502f`；Amazon 非原子 JSON 写入降为 0，原子写调用增至 11，运行中轮询不再暴露半写文件 |
| Phase 2.1 纯 Router 匹配与冲突契约 | 已完成 | `635c360`；纯 stdlib Router、11 项专项测试、零运行时接线，服务器 44 项完整门禁全绿 |
| Phase 2.2A `/healthz` 单路由接线 | 已完成 | `be39cbb`；独立 health route、旧分派 fallback、字节级响应契约，服务器 45 项完整门禁全绿 |
| Phase 2.2B-0 纯页面只读盘点 | 已完成 | 两个 Terra 只读审计交叉确认读取时机、导航注入、feature flag、重定向、query/尾斜杠与业务状态边界；首批只选择同构的 `/report`、`/report/player` |
| Phase 2.2B-1 日报纯页面路由 | 已完成 | `a59baa8`；逐请求模板读取与导航注入语义不变，旧内联分支删除，服务器 46 项完整门禁全绿 |
| Phase 2.2B-2 邻聊与工具纯页面路由 | 已完成 | `70f8982`；按领域建立两个窄 route，只迁移 GET HTML，邻聊与工具 API 边界保持不变，服务器 47 项完整门禁全绿 |
| Phase 2.2B-3 Harness 纯页面路由 | 已完成 | `e1b60af`；只迁移 GET `/harness`，保持无导航注入、逐请求模板读取及独立证书下载边界，服务器 48 项完整门禁全绿 |
| Phase 2.2B-4 缓存页面路由 | 已完成 | `46dab34`；按领域迁移 `/shop`、`/metrics`、`/taobao` exact GET，保留三种导入期快照/缺失模板语义及全部业务 API，服务器 49 项完整门禁全绿 |
| Phase 2.2B-5 Extract 纯页面路由 | 已完成 | `75d96ef`；只迁移 `/extract` exact GET，保持逐请求模板读取、逐请求 analysis mode、全量占位符替换与分析业务 API 边界，服务器 50 项完整门禁全绿 |
| Phase 2.3A 静态/文件边界审计与契约 | 已完成 | `8e17845`；三个 Terra 交叉审计 `/assets/`、固定证书、授权附件、Range/HEAD 与动态导出，新增两份隔离 HTTP 契约，服务器 52 项完整门禁全绿 |
| Phase 2.3B 固定证书 exact GET 路由 | 已完成 | `d720c25`；新增窄领域 route 并删除旧内联分支，保持固定路径、headers、正文、缺失、query 与非 GET 语义，清理历史会话数据中的 2 处退役条目后服务器 52 项完整门禁全绿 |
| Phase 2.3C Router 最小前缀匹配 | 已完成 | `2b6ad36`；只扩展纯 Router 与既有专项测试，冻结 exact/template 总体优先、最长前缀、重复冲突及未解码不可变 suffix，未接线 `/assets/`，服务器 52 项完整门禁全绿 |
| Phase 2.3D `/assets/` 前缀路由接线 | 已完成 | `656282d`、`2cd64a5`；新增窄静态资源 route 并删除旧 Handler 分支/方法，修正随迁移失效的 UI 结构断言，保持路径安全与全部 HTTP 语义，服务器 52 项完整门禁全绿 |
| Phase 3.0 任务模型与消费边界只读盘点 | 已完成 | 三路 Terra 与主审交叉列清四类 dataclass、字典/锁、全部创建/更新/查询/日志/API/SSE 消费点和逐字段差异；零运行时代码改动，发现下载校验失败分支存在独立未定义调用风险 |
| Phase 3.R1 下载校验失败分支修复 | 已完成 | `6111887`；只删除未定义调用并在既有 lifecycle 冻结精确 JSON 400 与后续健康响应，服务器 52 项完整门禁全绿 |
| Phase 3.1A 四类任务快照契约基线 | 已完成 | `a728a5c`；只新增一份隔离契约，冻结四类 public/GET/SSE、缺失差异、日志窗口/复制、artifact 重读、下载结果 alias 与 SSE marker，服务器 53 项完整门禁全绿 |
| Phase 3.1B 四类纯快照 adapter | 已完成 | `c60119d`；新增四个领域显式纯函数与纯 stdlib 专项，深复制结果、显式注入 artifact、零运行时接线，服务器 54 项完整门禁全绿 |
| 执行要求归一与门禁审计 | 已完成 | `df1c7a1`、`3937623`、`1b9f4aa`、`d715d93`；建立唯一要求入口，清除非活动域专项断言并保留通用 fail-closed 契约，服务器 52 项完整门禁全绿 |

Phase 0.5B 可以多智能体并行，但文件所有权必须互斥：一条线负责 Python 运行时与专用模块，一条线负责 MCP Bridge/Compose/env，一条线负责静态 UI 与受控文档；README、计划文档、资产版本、跨线冲突和最终集成由主任务统一处理。子智能体只运行专项测试，不得独立提交；主任务合并审计后统一提交和部署。

“每个阶段全功能回归”定义为以下集合，适用于 Phase 0.5B、0.5C、0.5D，以及 Phase 1～7 中每个可执行代码子阶段；专项测试通过不能代替该集合。即使子阶段只新增尚未接线的 core/router 模块，也要执行完整门禁，防止导入、镜像内容或测试发现规则发生漂移：

1. Windows 工作树完成 `git diff --check`、Python/Node 语法检查、依赖边界检查和仓库零遗留扫描；本地不构建 Docker。
2. 源码提交并通过 GitHub 同步到 `/home/openclaw/Video_analyzer-ui-4004`，服务器只使用 `bash scripts/deploy_ui_4004.sh` 构建并部署 4004 专属镜像。
3. 在新镜像中运行登记的全部确定性 Python 套件、带显式功能开关的专项套件和 Node Bridge 套件；不只运行本阶段改动对应的测试。
4. 部署后检查 `/healthz`、全部活动页面和 API smoke；未注册路径必须是无 `Location` 的通用 404。
5. 桌面与移动视口分别运行聊天滚动和邻聊上传队列 Playwright 回归，检查浏览器控制台和页面错误。
6. 检查 4004 容器环境、生效 Compose 配置、进程和近期日志；不得出现未注册 provider、启动失败或触碰 4002/4003 的证据。
7. 记录镜像 ID、提交 SHA、测试总数和失败数。任一项失败即留在当前阶段修复并重跑完整集合，不得带红灯进入下一阶段。

固定计数口径：Phase 0.5 先在 4004 服务器新镜像中运行 **36 项构建前确定性回归**，即 32 个常规 Python、`test_hot_report_resume.py`、启用 `SELLERSPRITE_TOOL_MOCK_MODE=1` 的 `test_27_presets_mock_boundary.py`，以及 `scripts/test_mcp_bridge_cache.js`、`sellersprite_mcp_chat/test_stdio_mcp_client.js` 两个 Node 门禁；部署后再运行 **2 个 Playwright 脚本**，合计 38 个自动化脚本。Phase 1.1 新增 `test_core_http.py` 和 `test_http_response_contract.py`、Phase 1.2 新增 `test_core_config.py` 后，历史登记门禁为 **39 项构建前确定性回归 + 2 个部署后 Playwright = 41 个自动化脚本**。Phase 1.3 新增 `test_core_json_store.py` 后，当时登记门禁为 **40 项构建前确定性回归 + 2 个部署后 Playwright = 42 个自动化脚本**。Phase 1.R1 新增 `test_deploy_ui_4004_boundary.py` 后，当时登记门禁为 **41 项确定性回归 + 2 个部署后 Playwright = 43 个自动化脚本**。Phase 2.1 新增 `test_router.py` 后，当时登记门禁为 **42 项确定性回归 + 2 个部署后 Playwright = 44 个自动化脚本**。Phase 2.2A 新增 `test_health_route_contract.py` 后，当时登记门禁为 **43 项确定性回归 + 2 个部署后 Playwright = 45 个自动化脚本**。Phase 2.2B-1 新增 `test_report_page_routes_contract.py` 后，当时登记门禁为 **44 项确定性回归 + 2 个部署后 Playwright = 46 个自动化脚本**。Phase 2.2B-2 新增 `test_lan_tool_page_routes_contract.py` 后，当时登记门禁为 **45 项确定性回归 + 2 个部署后 Playwright = 47 个自动化脚本**。Phase 2.2B-3 新增 `test_harness_page_route_contract.py` 后，当时登记门禁为 **46 项确定性回归 + 2 个部署后 Playwright = 48 个自动化脚本**。Phase 2.2B-4 新增 `test_cached_page_routes_contract.py` 后，当时登记门禁为 **47 项确定性回归 + 2 个部署后 Playwright = 49 个自动化脚本**。Phase 2.2B-5 新增 `test_extract_page_route_contract.py` 后，当时登记门禁为 **48 项确定性回归 + 2 个部署后 Playwright = 50 个自动化脚本**。Phase 2.3A 新增 `test_static_asset_contract.py` 与 `test_harness_certificate_contract.py` 后，当前登记门禁为 **50 项确定性回归 + 2 个部署后 Playwright = 52 个自动化脚本**；其中部署边界脚本在服务器源码 checkout 执行，容器内执行 46 个常规确定性 Python、单独以 `HOT_VIDEO_REPORT_ENABLED=1` 和隔离临时根目录执行 `test_hot_report_resume.py`，再执行两个 Node 门禁。其他 smoke 容器显式使用 `HOT_VIDEO_REPORT_ENABLED=0` 与隔离 `APP_TEST_ROOT`。Node stdio 门禁的实际路径为 `sellersprite_mcp_chat/test_stdio_mcp_client.js`。两个 Playwright 都覆盖桌面和移动 viewport。`test_api.py` 是吞异常的固定历史数据探针，`test_low_reasoning_video_insight.py` 会调用付费外部模型且失败仍返回成功，二者只作为人工实验，不计入阶段门禁。

Phase 0.5 最终证据（2026-08-28）：源码清理提交为 `0618559`，回归夹具修复为 `bd864cd`、`e362e7b`，递归排除历史 Python 字节码的构建修复为 `d1d4d2c`；服务器部署镜像为 `a70c61cd2e9f`。最终 36 项构建前回归失败数为 0，两个部署后 Playwright 均通过；`/healthz` 和 12 个活动页面为 200。V2 项目树、镜像、容器环境、生效 Compose 和进程扫描均通过。项目外回滚备份覆盖 11 组、128 个文件、3,788,317 字节，manifest SHA-256 为 `7f1c552462b943e733424e1f32c1a952a162256d3d247696b23dfdc70c61a91f`；备份保留原权限，未来恢复其中 root 文件时需要 `sudo`。

## 五、Phase 1：抽取低风险基础设施

每个子步骤独立提交，保持行为不变。

### 5.1 `core/http.py`

Phase 1.1 只迁移 `json_response`、`text_response`、`binary_response`、`file_response`、`write_sse_event` 五个稳定响应/SSE helper。路由和请求体读取仍留在 `web_app.py`；不得在本阶段移动 `_lan_chat_request_json`、multipart/JSON body 解析、静态资源/视频/附件服务、SSE 生命周期或任何业务异常映射。

按以下互斥边界实施，每个运行时提交后都必须执行当时登记的完整门禁；Phase 1.1 当时登记门禁为 40 个自动化脚本，后续阶段当前登记门禁为 52 个：

1. **1.1A 新模块与字节级单测：** 只拥有 `scripts/core/http.py`、`scripts/core/__init__.py` 和新建的纯 helper 单测。使用 `FakeHandler`/`BytesIO` 锁定 JSON UTF-8 与缩进、no-cache、HEAD、Range 206/416、RFC 5987 文件名、1 MiB 流式读取、BrokenPipe 行为以及 SSE `data:` 帧和 flush；不导入 `web_app.py`，不产生循环依赖。
2. **1.1B 调用方替换：** 单独拥有 `scripts/web_app.py`，显式导入上述五个 helper 并逐个替换原定义。AST 终审确认真实直接调用量为 278 个 JSON、13 个 text、12 个 binary、6 个 file 和 4 个 SSE，共 313 个；状态码、headers、异常捕获和 Handler 签名必须保持不变，不改路由条件和业务分支。
3. **1.1C HTTP 黑盒契约：** 单独拥有 HTTP contract 测试文件，补齐当前 smoke 未锁定的 Range、Content-Disposition、HEAD、SSE 帧和断连行为。它可以与 1.1A 并行准备，但必须在 1.1A 合入后执行，在 1.1B 合入后作为端到端回归再次执行。
4. **1.1D 只读交叉审计：** 用 CodeGraph 核对调用数、导入方向和遗漏定义；不得修改 A/B/C 拥有的文件。主任务统一提交、GitHub 同步、服务器构建和部署。

验收：新增字节级单测、HTTP smoke、workflow lifecycle、SSE 专项、未注册/禁用功能 404 分支通过，且 Phase 1.1 当时登记的 40 脚本门禁全绿。

Phase 1.1 最终证据（2026-08-28）：`6d7a52b` 新增纯 stdlib `scripts/core/http.py`、12 项字节级单测和 HTTP 黑盒契约，`03827ac` 只在 `web_app.py` 增加一次显式导入并删除五个原定义。AST 验收为旧 FunctionDef 0、导入 1、313 个直接调用数保持不变；CodeGraph 已在最终代码上重新同步。A/C 集成提交和 B 调用方切换提交分别在服务器新镜像执行 38 项构建前回归，均为 0 失败；最终部署镜像 `fc57b7755c31` 的 `/healthz`、12 个活动页面、HTTP smoke 和两个桌面/移动 Playwright 全部通过，4002/4003 健康检查仍为 200。部署日志无 import、语法或启动失败；Playwright 导航取消产生的两条 `BrokenPipeError` 为抽取前既有的客户端断连行为，本阶段按行为等价要求保留，后续若要静默应作为独立可靠性改动并单独回归。

2026-08-29 补漏证据：`b957587` 将 SellerSprite 的指标边界、工具专属审计字段、查询标题规则和匿名关键词动态提示全部移回 provider adapter，并从中性 renderer 删除 7 项不可达工具规则和 3 个隐藏枚举值；服务器镜像 `4e6701d89667` 通过 38 项确定性回归、2 个 Playwright、12 个活动页面和 4002/4003 隔离检查。`8572f31` 只在 JSON/text 非流式响应的 body 写入阶段捕获 `BrokenPipeError`/`ConnectionResetError`，新增断连与序列化异常单测后服务器镜像 `bb4a08d9e487` 再次通过同一完整门禁；Playwright 后容器日志中的 traceback、BrokenPipe、ConnectionReset、语法和 import 错误匹配数为 0。SSE 断连仍向上传播以终止事件循环，JSON 序列化和 header 异常仍显式失败。

### 5.2 `core/config.py`

本阶段只收口**模块导入期配置**，不把所有 `os.getenv` 机械搬入一个全局对象。2026-08-29 审计基线为 `web_app.py` 共 69 处环境读取，其中 13 处发生在模块导入期，包含路径、布尔、整数、浮点和字符串；请求期 API Key、Mock 开关、tool router mode、动态端口和任务参数继续在所属 service/request 边界读取，避免把安全开关和测试覆盖冻结为进程启动快照。

配置模块使用纯 stdlib 和不可变 `AppConfig`，提供显式 `from_env(env: Mapping[str, str], root: Path | None = None)` 构造入口。默认 `root` 继续使用 `Path.cwd()`，不得借重构改为 `__file__` 推导；布尔、整数和浮点解析必须保持当前默认值、空值和非法值失败语义。不得把真实密钥、Cookie、token 或 provider 会话放入配置对象，也不得导入 `web_app`、store、route 或 service。

Phase 1.2 已按以下子阶段完成，每个阶段均独立提交并执行当时登记的 41 脚本门禁：

1. **1.2A 纯配置模型与解析测试：** 新增 `core/config.py` 和纯单测，覆盖两份 env mapping 在同一进程独立构造、路径派生、测试根目录只在 `UI_TEST_MODE` 开启时生效、布尔同义值、整数/浮点边界和非法值显式报错；不切换 `web_app.py`。
2. **1.2B 路径配置切换：** `web_app.py` 从单个 `APP_CONFIG` 派生 `ROOT`、`RUNTIME_ROOT`、`SCRIPTS_DIR`、`DATA_DIR`、`VIDEOS_DIR`、`OUTPUT_DIR` 及其路径常量；过渡期只允许显式同名赋值，不允许 `from core.config import *`。保持 4004 Compose 工作目录和 `APP_TEST_ROOT` 行为不变。
3. **1.2C 其余导入期配置切换：** 收口当前剩余导入期的 TTL、数量限制、超时、OCR 路径、Feishu cache、proxy enabled 和 UI 测试来源配置；AST 门禁要求 `web_app.py` 模块顶层不再直接调用 `os.getenv`。函数体内的动态环境读取不在本阶段迁移。

验收边界必须写清：Phase 1.2 保证“同一进程可构造多个互不污染的 `AppConfig`”，不承诺“同一进程同时运行多套完整 web 应用”。`ChatStore`、`LanChatStore` 和 provider stores 当前仍由 composition root 在导入期实例化，完整多实例应用要等路由/服务组装边界建立后再评估；测试 web 运行时继续使用独立子进程与 `APP_TEST_ROOT`。4004 默认值不得回落到 4002/4003 的项目名、数据目录或端口。

Phase 1.2 最终证据（2026-08-29）：`6c9731d`（1.2A）服务器镜像 `5a40219821e279f3c3e7d1bc27a9a2e8053c3efadfe4725ad130874311238577`、`64e63af`（1.2B）服务器镜像 `3923dfaff675cb86f6f98f6c2c4095134ad1a7e7cd11507a96ed61c1edde7c18`、`2115c5f`（1.2C）服务器镜像 `00a5deeca714557a23ebf76df888ee2a1105a0a421bccf9d1f4f8bb091bd37cc` 均通过 **39 项确定性回归 + 2 个 Playwright = 41 项**，失败数为 0。1.2C 的 AST 终审确认模块级 `os.getenv` 为 0、函数期动态读取为 56，读取键的基线多重集合不变。

Phase 1.2R 漏洞收口证据（2026-08-29）：`a30b494` 的服务器镜像为 `sha256:42fd8d8cff7d6d6e3de4c5b17e99dd0cc8fe357d61049e5fa68d762de16041b0`，**39 项确定性回归 + 2 个 Playwright = 41 项**全绿。`/api/chat/tool-catalog` 恢复 200 并包含 system、function、SociaVault、SellerSprite、出海匠五域；全部 10 个外部 chat provider 入口均 fail-closed，未知外部 provider 在 sessions、messages、catalog、events、ask、export、rename 和删除等读写路径返回 400，内部 `None` 仍归 Home；4004 近期日志异常匹配为 0，4002/4003 健康检查均为 200。

部署边界终审发现旧 `UI4004_BRANCH` / `ALLOW_NON_UI4004_BRANCH` 可绕过分支检查后，`afacd7c` 将部署分支硬锁为 `v2` 并增加防回归断言，所有 legacy、非 `v2`、非 4004 项目名和非 4004 端口均在首次 Docker 探测前 fail-closed。最终服务器镜像为 `sha256:2fa76aa8a45faade8cd34af16fc257e9962ba1d24c8669eee29d553cbfb7342e`，再次通过 **39 项确定性回归 + 2 个 Playwright = 41 项**；12 个活动页面全部 200，五域 catalog 与 unknown provider 黑盒检查通过，4004 日志异常匹配为 0，4002/4003/4004 健康检查均为 200。

### 5.3 `core/json_store.py`

只提供纯函数式 JSON 文件原语，不创建通用 repository、ORM、数据库基类或有业务状态的 `JSONStore`。SQLite 事务继续留在各领域 repository 中；`ChatStore` 的 debounce、领域锁、异常日志和 session 迁移仍归 `chat_session.py` 所有。

原子写入是明确的可靠性变化，不再标记为“纯行为等价搬运”。新实现必须保持 UTF-8、`ensure_ascii=False`、两空格缩进、末尾换行、父目录创建、文件不存在时的既有返回契约和损坏 JSON 显式异常；临时文件必须与目标同目录，完成 flush/fsync 后用 `os.replace`，替换失败时清理临时文件且保留旧目标。锁的承诺限定为**同一进程内按规范化路径串行写入**；跨进程防丢更新不在本阶段伪装保证，跨进程一致性继续由 SQLite 或领域协议解决。

Phase 1.3 已按以下子阶段完成；每个运行时子阶段均独立提交并执行当时登记的 42 脚本门禁：

1. **1.3A 原子原语与故障注入测试：** 新增 `read_json`、`atomic_write_json` 及临时文件/锁管理测试，不切换运行时调用方。覆盖并发读写始终得到完整 JSON、替换失败保留旧文件、序列化失败不创建目标、损坏 JSON 抛错和异常后锁释放。
2. **1.3B `web_app` helper 切换：** 只替换 `web_app.py` 当前的 `read_json`/`write_json` 定义与调用；迁移前后对缺失文件、损坏文件、缩进和末尾换行做字节契约。`result_path.write_text(json.dumps(...))` 等非 helper 写入必须逐项审计，不允许顺手全仓替换。
3. **1.3C 只读复用审计（默认不迁移）：** `chat_session.py` 当前已有自己的 `tempfile + fsync + os.replace`、双锁和 debounce，而且 session 文件当前没有末尾换行，与 `web_app.write_json` 的字节契约不同。只有 contract test 证明 debounce、锁顺序、迁移、错误日志和文件字节完全不变时才允许复用更底层的临时文件原语；否则保留领域实现，不作为 Phase 1 阻塞项。

验收：故障注入和并发测试全绿；确认没有 SQLite、业务 dataclass、session schema 或 job 状态迁入 `core/json_store.py`；可靠性变化单独记录，不能夹带业务修复。

Phase 1.3 最终证据（2026-08-29）：`1f231c4`（1.3A）服务器镜像 `sha256:b8ac407aab6efca544ffd0538be01fc5bcef80126b6dcfc0fd63305e63f1b510` 新增纯 stdlib `read_json` 与 `atomic_write_json`。原子写入是明确的可靠性变化：按规范化路径提供同进程写锁、以等待者计数回收锁条目，在目标同目录创建临时文件，完成 flush/fsync 后以 `os.replace` 替换；序列化、文件描述符、fsync 或替换失败时均清理临时文件并保留旧目标。12 项专项测试覆盖缺失/损坏 JSON、输出字节契约、序列化/替换/fsync/描述符故障、异常后锁释放、锁规范化与回收；Windows 因文件共享语义跳过 POSIX 并发读取项，服务器 Linux 全部执行。`1c630be`（1.3B）服务器镜像 `sha256:55ca43a5465e62080ceae41e7861ba7015a7b2125cb4da7d4a03d4a1e7926365` 只让 `web_app.py` 显式导入原语，AST 终审确认 `read_json` 调用 47、`atomic_write_json` 调用 10、旧 `read_json`/`write_json` 定义和 `write_json` 调用均为 0、`json.dump` 为 0；唯一 Amazon `result_path.write_text(json.dumps(...))` 非 helper 写入仍为 1 并保持不动。两个阶段均通过 **40 项确定性回归 + 2 个 Playwright = 42 项**，失败数为 0；12 个活动页面均为 200，4002/4003/4004 健康检查均通过，容器日志异常匹配为 0，活动 provider 边界回归全绿。1.3C 完成只读审计后默认不迁移 `ChatStore`：其无末尾换行、debounce、双锁、迁移与异常日志契约尚无严格等价证明，继续保留领域实现。

Phase 1 漏洞复审与收口证据（2026-08-29）：`40d2b2e` 让 `deploy_ui_4004.sh` 默认且只允许 `short-video-analyzer-ui-4004:latest`，并在 Docker 探测前拒绝共享/错误标签；通用 Compose 的 analyzer/web 默认保持 `short-video-analyzer:latest`，sellersprite-redirect 只改为可被 `ANALYZER_IMAGE` 覆盖，避免反向污染 4002/4003。首次完整门禁准确捕获了部署测试在容器中不可见 Compose 源码及 `test_proxy_pool_lifecycle.py` 的陈旧共享标签断言；`4f978ea` 将 Compose 静态检查固定为服务器源码 checkout 门禁并更新旧契约，随后完整重跑全绿。历史污染的共享标签已恢复到 4002 正式镜像 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`，P1 最终 4004 镜像为 `sha256:c3a11ff94fe2127c9148df7f7e0b85c2a9b669988d739ea52a374ceb376e2378`。`2f9502f` 将 `run_amazon_job` 唯一非 helper 写入切换为 `atomic_write_json(result_path, result)`，AST 契约更新为 `read_json` 47、`atomic_write_json` 11、非原子 Amazon JSON 写入 0；P2 最终 4004 镜像为 `sha256:a1d1830cb093faf3af2aaa4a0990d496961805c65264693f5d10cb252cd3681a`。两个收口阶段均分别通过 **41 项确定性回归 + 2 个 Playwright = 43 项**，失败数为 0；最终 12 个活动页面均为 200，4002/4003/4004 健康检查均为 200，未知外部 provider 返回 400，容器日志异常匹配为 0。

**Phase 1 整体验收：已完成并通过漏洞复审。** HTTP smoke 与 Phase 0 测试矩阵全绿；最终部署后 `test_web_workflow_lifecycle.py` 连续独立执行两次均通过，确认临时根目录可重复运行；`web_app.py` 仅通过显式 import 使用 `core/http.py`、`core/config.py` 和 `core/json_store.py` 三个 core 模块；对外响应快照无非规范化差异。4004 构建标签与 4002 正式标签已经隔离，Amazon 结果文件不存在半写读取窗口。Phase 2.1 纯 Router、Phase 2.2A `/healthz` 首条运行时接线、Phase 2.2B-1～B-5 页面小批迁移及 Phase 2.3A～2.3D 均已完成；当前下一步为 Phase 3.0 任务模型与消费边界只读盘点。

## 六、Phase 2：建立无业务状态的路由骨架

先建立一个很薄的 stdlib 路由注册表，但本阶段只迁移健康检查、纯页面和静态资源，不提前搬 Shop/Metrics/Amazon 等依赖任务全局状态的业务路由：

```python
router.get("/healthz", healthz)
router.get("/report", report_page)
```

Router 只负责 method/path 匹配、path 参数和 404/405；不得拥有业务 store、job 字典、线程、数据库连接或 provider 配置。注册函数接收 handler 或显式依赖，不得导入 `web_app`。`web_app.py` 作为 composition root 创建 router 并保留 fallback，过渡期只允许 `web_app → routes`。

Phase 2.1 已冻结 Router 契约：调用方先 `urlparse` 并只传入未整体解码的 `parsed.path`；query、fragment、尾斜杠、连续斜杠和 percent encoding 不由 Router 规范化。模板只允许完整 `{name}` segment，literal specificity 高者优先且与注册顺序无关；同方法、同 specificity 且可匹配同一路径的模板注册时报冲突，不允许隐式阴影。`resolve` 只返回不可变的 handler/params 匹配结果，不执行 handler；HEAD 不自动回退 GET。`RouteNotFound` 与 `MethodNotAllowed` 只是结构化匹配结果，Phase 2.2 接线不得借此把现有全站 404 擅自改成 405。

按以下子阶段实施，每个子阶段独立提交并执行当前登记的至少 52 脚本门禁；新增专项脚本时同步提高总数：

1. **2.1 Router 匹配与冲突测试（已完成）：** `635c360` 新增 GET/POST/DELETE、根/精确/参数路径、原始编码、404/405、不可变匹配、冲突和注册顺序无关测试；AST 锁定 `routes → web_app` 为 0，且 `web_app` 尚未接入 Router。
2. **2.2A health 单路由接线（已完成）：** `be39cbb` 只迁移 GET `/healthz`。状态码、JSON 字节、Content-Type、Content-Length 与不存在的 cache header 已冻结；composition root 创建 Router，命中后调用显式 handler，未命中或方法不符继续进入旧分派。POST/DELETE `/healthz` 保持 JSON 404，HEAD 保持空 body 404，未启用全局 405/Allow 或 HEAD fallback。
3. **2.2B 纯页面分批迁移（已完成）：** 2.2B-0 只读盘点、2.2B-1 `/report`/`/report/player`、2.2B-2 `/lan-chat`/`/tool`、2.2B-3 `/harness`、2.2B-4 `/shop`/`/metrics`/`/taobao` 与 2.2B-5 `/extract` 均已完成。`/harness-ca.crt` 继续留给 2.3 文件下载边界；`/`、`/chat`、`/amazon`、`/chuhaijiang` 因动态 provider 装配延期，`/proxy` 因状态注入和 feature flag 延期。`/amazon/`、`/chuhaijiang/` 两条显式 307 与其他严格尾斜杠行为必须留待对应 provider 阶段分别锁定，不做全局 slash 归一化。
4. **2.3 静态资源与固定文件响应（2.3A～2.3D 已完成）：** `/assets/` 与固定证书端点的黑盒契约已经冻结并分别迁入窄领域 route，Router 前缀能力与静态资源接线保持独立提交。授权附件、报表封面、视频 Range、邻聊/淘宝文件和动态导出保留各自实现，没有建立通用文件服务抽象。Phase 2 无业务状态路由骨架至此关闭。

Phase 2.1 最终证据（2026-08-29）：`635c360` 仅新增 `scripts/routes/__init__.py`、`scripts/routes/router.py` 和 `scripts/test_router.py`，`web_app.py` 零改动、零运行时接线。Router 只依赖 stdlib，不含 store、job、线程、锁、数据库或 provider 配置；11 项专项测试覆盖根路径、GET/POST/DELETE、单/多参数、空段与跨段拒绝、原始 `%2F`、尾斜杠/连续斜杠、不可变结果、非 callable、literal 优先、等 specificity 冲突、404/405 和固定 Allow 顺序。服务器镜像为 `sha256:8ee38e8054c846a052aa9feb7dd96a125c7dc6c1983c44fce0f5abd4116b820e`，完整门禁为 **42 项确定性回归 + 2 个 Playwright = 44 项**，失败数为 0；12 个活动页面均为 200，4002/4003/4004 健康检查均为 200，未知 provider 边界全绿，容器日志异常匹配为 0。

Phase 2.2A 最终证据（2026-08-29）：`be39cbb` 新增 `scripts/routes/health.py` 和 `scripts/test_health_route_contract.py`，并只修改 `web_app.py` 的 Router 导入、模块级注册与 `do_GET` 首个分派；`do_POST`、`do_DELETE`、`do_HEAD` 及其余 GET 分支未改。专项测试锁定 UI test true/false 的 44/45 字节 JSON、无尾随换行、Content-Type、Content-Length、无 Cache-Control/Allow、query 等价、严格尾斜杠及非 GET 旧行为。服务器镜像为 `sha256:238f86e11cb404849e77a16c9bed44dabc36d68517ab0ed7b80e973dd04979bc`；完整门禁为 **43 项确定性回归 + 2 个 Playwright = 45 项**，失败数为 0。12 个活动页面均为 200，4002/4003/4004 健康检查均为 200，未知 provider 返回 400，容器最近日志异常匹配为 0；4004 专属镜像与 4002 正式共享标签继续隔离。

Phase 2.2B-1 最终证据（2026-08-29）：`a59baa8` 新增 `scripts/routes/report_pages.py` 与 `scripts/test_report_page_routes_contract.py`，只迁移 GET `/report`、`/report/player` 并删除两个旧内联分支。注册函数只接收 `scripts_dir` 与 `inject_nav` 显式依赖，不导入 `web_app`、store、job、provider 或数据库；每次请求重新读取 UTF-8 模板的时机保持不变。专项测试冻结模板热更新、精确导航路径、响应 header、缺失模板异常传播，smoke 冻结 query 等价与严格尾斜杠 404。服务器镜像为 `sha256:643d1b3e97fafd489b9fad97d74d074a932b5af7cda89939de384bd1a573cd0d`；完整门禁为 **44 项确定性回归 + 2 个 Playwright = 46 项**，失败数为 0。12 个活动页面均为 200，4002/4003/4004 健康检查均为 200，共享正式镜像仍为 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`，未知 provider 有效 JSON 请求返回 400，最近日志异常匹配为 0。

Phase 2.2B-2 最终证据（2026-08-29）：`70f8982` 新增按领域命名的 `scripts/routes/lan_chat.py`、`scripts/routes/tool.py` 与共享专项脚本 `scripts/test_lan_tool_page_routes_contract.py`，只迁移 GET `/lan-chat`、`/tool`。两个 route 均每请求读取自己的 UTF-8 模板并注入精确路径，不建立通用模板 registry，不导入 `web_app`、store、API、provider 或任务状态；`/api/lan-chat/*`、`/api/tool/convert`、UI_TEST_MODE 写入策略及 HEAD/POST/DELETE 分派保持原位。服务器镜像为 `sha256:205b43a84a366efe69490b5a2314aeac142f7e415c17966387be0f8f97066a46`；完整门禁为 **45 项确定性回归 + 2 个 Playwright = 47 项**，失败数为 0。12 个活动页面均为 200，两页 query 响应体等价且尾斜杠无重定向 404，邻聊 bootstrap 为 200、工具转换 GET 为 404；4002/4003/4004 健康均为 200，共享正式镜像未变化、未知 provider 400、最近日志异常匹配为 0。

Phase 2.2B-3 最终证据（2026-08-29）：`e1b60af` 新增 `scripts/routes/harness.py` 与 `scripts/test_harness_page_route_contract.py`，只迁移精确 GET `/harness` 并删除旧内联页面分支。route 每次请求重新读取 UTF-8 模板，不注入统一导航，不导入 `web_app`、store、job、provider 或证书状态；`/harness-ca.crt` 仍由旧文件下载分支处理。专项测试冻结模板热更新、无导航注入、响应 header、缺失模板异常传播、query 等价及尾斜杠/HEAD/POST/DELETE 404。服务器镜像为 `sha256:818c94163fd1d964a94ecaec484a17b13fd1765482514a507f1b4f07242164e5`；完整门禁为 **46 项确定性回归 + 2 个 Playwright = 48 项**，失败数为 0。12 个活动页面均为 200，证书端点保持 200、`application/x-x509-ca-cert`、attachment 与 `no-store`；4002/4003/4004 `/healthz` 均为 200，共享正式镜像仍为 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`、未知 provider 400、最近日志异常匹配为 0。

Phase 2.2B-4 最终证据（2026-08-29）：`46dab34` 新增三个领域 route `scripts/routes/shop.py`、`metrics.py`、`taobao.py` 与 `scripts/test_cached_page_routes_contract.py`，只迁移 `/shop`、`/metrics`、`/taobao` 的 exact GET。三个 route 只接收导入期 HTML 快照与导航函数，不读文件、不导入 `web_app` 或业务状态；`METRICS_HTML` 无条件读取、`SHOP_HTML`/`TAOBAO_HTML` 的 `is_file() ? read_text : ""` 及注册晚于快照初始化的差异均由 AST 契约冻结。Shop/Metrics job、SSE、Taobao 用户/浏览器/归档 API 和 UI_TEST_MODE 拦截仍留在旧分派。服务器镜像为 `sha256:677e2d9185fd08593c6bdafdbe0daa27e318507722df59d1968fc565ee3b2f82`；完整门禁为 **47 项确定性回归 + 2 个 Playwright = 49 项**，失败数为 0。首次主门禁因手写清单含不存在的历史文件名而停止，随后改为从镜像实际 `test_*.py` 清单排除两个手工探针、两个 Playwright、宿主边界与独立续跑后从头完整重跑，43 个常规 Python 与两个 Node 全绿。12 个活动页面均为 200，三页 query 响应体等价、尾斜杠无 `Location` 404、相邻 API 仍返回旧链 404；4002/4003/4004 `/healthz` 均为 200，共享正式镜像未变化、未知 provider 400、最近日志异常匹配为 0。

Phase 2.2B-5 最终证据（2026-08-29）：`75d96ef` 新增 `scripts/routes/extract.py` 与 `scripts/test_extract_page_route_contract.py`，只迁移 GET `/extract` 并删除旧 exact 分支。route 显式接收模板路径、analysis-mode callable 与导航函数，每请求重新读取 UTF-8 模板、重新取 mode、替换全部 `__DEFAULT_ANALYSIS_MODE__` 并注入 `/extract` 导航；不缓存模板、不导入 `web_app`/`AppConfig`/业务状态，上传、分析、翻译、后处理、结果与文件 API 全部保持旧分派。服务器镜像为 `sha256:d4baa16115cb6816afe95757f02e49c886b68c851b3b7c7e770de9e2b2c5407b`；完整门禁为 **48 项确定性回归 + 2 个 Playwright = 50 项**，失败数为 0，其中主容器自动发现并执行 44 个 Python 与两个 Node 门禁，续跑测试独立执行。13 个现存页面均为 200，`/extract` 与 query 响应体 SHA-256 相同、占位符残留为 0、尾斜杠四方法均为无 `Location` 的 404；延期的 `/amazon/`、`/chuhaijiang/` 仍精确 307 到无 query 的规范路径。4002/4003/4004 `/healthz` 均为 200，共享正式镜像仍为 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`，未知 provider 返回 400，最近日志异常匹配为 0。

**Phase 2.2B 阶段总结与漂移审计：已完成。** 五批代码均只搬 exact GET 页面呈现，不迁移相邻 API、store、job、SSE、provider renderer、feature flag 或代理状态；所有 route 依赖均由 composition root 显式注入，CodeGraph 未出现 `routes → web_app` 反向依赖。审计确认 `/`、`/chat`、`/amazon`、`/chuhaijiang` 和 `/proxy` 不满足无业务状态条件，继续留在各自垂直切片；不以减少 `Handler` 行数为理由扩大范围。Phase 2.3A 已冻结 `/assets/` 与固定证书端点的现有文件响应契约；下一步只接线固定证书 exact GET，Range、业务附件和视频流不随静态资源迁移。

Phase 2.3A 最终证据（2026-08-29）：`8e17845` 只新增 `scripts/test_static_asset_contract.py` 与 `scripts/test_harness_certificate_contract.py`，运行时代码零改动。两套测试均复制当前 `scripts/` 到临时工作树，使用隔离 `APP_TEST_ROOT`、假静态文件/假证书与真实 HTTP 子进程，不读取服务器真实证书或静态内容，不在主测试进程导入/patch 巨型 `web_app`。assets 7 项冻结 nested/unknown MIME、长度/cache/body、query、单次 decode、根内归一化、文件尾斜杠、穿越/符号链接、缺失、Range 忽略及 GET-only 方法边界；证书 2 项冻结存在/缺失、精确下载 headers、query、尾斜杠与 HEAD/POST/DELETE。服务器镜像为 `sha256:4a1db35f4a1a716f8da8d29d24c335b3488aef7d7a8a2d0074c17de886e8cdd3`；完整门禁为 **50 项确定性回归 + 2 个 Playwright = 52 项**，失败数为 0，Linux 符号链接逃逸项实际执行通过。13 个现存页面均为 200；部署态 asset query 与原 body SHA-256 相同，Range 仍返回完整 200、HEAD 404、编码穿越 400；证书仍为 x509 attachment/no-store 且非 GET 为 404。4002/4003/4004 健康均为 200，共享正式镜像仍为 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`，未知 provider 400，最近日志异常匹配为 0。

Phase 2.3B 最终证据（2026-08-29）：`d720c25` 新增 `scripts/routes/harness_certificate.py`，由 composition root 只注入固定证书路径并注册 exact GET，删除 `Handler.do_GET` 中唯一对应内联分支；没有新增通用文件 helper、前缀路由、Range、HEAD fallback、授权附件或业务 API 迁移。专项 HTTP 与 AST 契约共 3 项，冻结存在/缺失、x509 attachment、Content-Length、`no-store`、query 等价、严格尾斜杠及 HEAD/POST/DELETE 旧 404，并证明 `routes → web_app` 为 0、旧内联分支为 0。Terra 实现、独立语义审查和架构审查均通过，CodeGraph 确认 route 只有 composition root 一个调用方且依赖方向为 `web_app → routes → core`。服务器提交 `d720c25a5e200c71a5242b2b63926715224a8077`、镜像 `sha256:e2beed79bf7ee0ad8757c2cc0e6632debee23d51b66853009f06499c0e190d6f` 通过清理后的 **50 项确定性回归 + 2 个 Playwright = 52 项**，失败数为 0；独立日报续跑通过。部署黑盒确认 13 个页面为 200，证书正文 1,874 字节且 query 等价，尾斜杠与非 GET 为 404，通用未知路径无重定向 404，未知 provider 为 400 JSON；4002/4003/4004 首页健康均为 200，正式镜像仍为 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`。零残留复核发现并原子清理 `sessions.json` 同一条历史助手回复中的 2 处退役条目，源码、镜像、环境、文件名和持久化数据最终命中均为 0；4004 只使用原镜像重启，未构建、停止或改写 4002/4003。容器启动时仍有 1 条既有静态代理节点同步 404 告警，页面与路由回归不受影响；该运行配置问题单独跟踪，不并入本次结构迁移。

Phase 2.3C 最终证据（2026-08-29）：`2b6ad36` 只修改 `scripts/routes/router.py` 与既有 `scripts/test_router.py`，新增冻结的 literal prefix route、GET-only 注册、最长前缀、重复冲突及 raw suffix 不可变映射；exact/template 路径候选在方法边界上也总体优先，避免宽前缀穿透既有 POST/DELETE 业务路径。没有修改 `web_app.py`、没有注册 `/assets/`、没有 URL decode、glob、正则、middleware 或 HEAD fallback。Terra 实现与两路交叉审查曾对跨方法优先级给出不同建议，主审按现有 Router 安全边界选择 exact/template 总体优先，并以 GET/HEAD 405、POST/DELETE 允许列表和纯 prefix 方法边界专项锁定；Router 专项由 11 项增至 14 项，未新增脚本，因此完整门禁仍为 **50 项确定性回归 + 2 个 Playwright = 52 项**，失败数为 0。服务器提交 `2b6ad36cde56443f130f512e45d78f42b4c129c0`、镜像 `sha256:578f54c72f10b2d50067b4eb2803f237413ce253d436fe515d9ec09d518928e1` 的 46 个常规 Python、独立日报续跑、两个 Node 和两个 Playwright 全绿。部署黑盒确认 13 个页面、4002/4003/4004 健康、现有静态文件 query/尾斜杠/Range/穿越/非 GET、证书、通用未知路径和未知 provider 行为均未变化；4002/4003 镜像与启动时间未变化，源码、镜像、环境和持久化数据零残留，4004 新容器日志异常与静态代理同步告警匹配均为 0。

Phase 2.3D 最终证据（2026-08-29）：`656282d` 新增 `scripts/routes/static_assets.py`，由 composition root 只注入固定静态根与 MIME guesser，通过 `get_prefix("/assets/", ...)` 接收未解码 suffix；route 每请求保持单次 `unquote → root/path resolve → containment`，继续以原 JSON 400/404 与完整二进制 200 响应，并删除 `Handler.do_GET` 旧前缀分支和 `Handler.serve_static_asset`。既有静态资源契约增至 8 项并加入防假绿 AST：唯一注册必须精确为 `router.get_prefix("/assets/", serve_static_asset)`，旧分支/方法为 0，依赖方向严格为 `web_app → routes → core`。Terra 实现、架构安全审查与测试有效性审查均通过；审查发现并修复了注册接收者未锁定以及静态根解析时机两处测试/等价性缺口。首次服务器完整门禁在第 42 项发现 `test_ui_contract.py` 仍把缓存策略归属锁在 `web_app.py`；`2cd64a5` 将其最小修正为静态 route 中唯一 `CACHE_CONTROL` 和唯一 `binary_response(..., cache_control=CACHE_CONTROL)` 的 AST 契约，重新构建后从第 1 项完整重跑。最终服务器提交 `2cd64a545f409073b21a3b1b713b288a715175e6`、镜像 `sha256:db5c7ca536a9cb6dacd4fd4d6605fd0a7d1cc1f1439c211fa06580287b639c13` 通过 **50 项确定性回归 + 2 个 Playwright = 52 项**，失败数为 0；46 个主 Python、宿主部署边界、独立日报续跑、两个 Node 与两个 Playwright 均为同一修正版镜像结果。部署黑盒确认 13 页面和三端健康均为 200；`ui-system.css` 为 134,494 字节、SHA-256 `52409786c3b6884e588e31f3fed9210e96de8907f04fe36121b65a35a97a3f2a`，query、文件尾斜杠与 Range 正文不变且 Range 仍为完整 200；asset root/非 GET 为 404、编码穿越为 400、未知 provider 为 400。4002/4003 镜像与启动时间未变化，服务器 checkout clean，源码、镜像、环境和持久化数据零残留，代码日志异常为 0；启动时出现 1 条已单独跟踪的静态代理节点同步 404 配置告警，未擅自改写真实代理数据。

执行要求归一与门禁审计证据（2026-08-29）：`df1c7a1` 建立 `docs/refactor-execution-requirements.md` 唯一入口，`3937623` 与 `1b9f4aa` 只收紧测试为活动域和通用未知输入契约，`d715d93` 只整理主计划；运行时代码、路由、provider 注册、API schema、SSE、数据目录和 UI 行为均未修改。CodeGraph 依赖审计无新增边，tracked/worktree/镜像/运行容器/data 文件名与容器环境扫描命中 0。服务器镜像 `sha256:12982ce1eff91175b1d18c2057fa641587b94a4bfce9d8461dd9257b8ec116c9` 通过 **50 项确定性回归 + 2 个 Playwright = 52 项**，失败数为 0；13 个现存页面均为 200，通用未知路径 GET/POST/DELETE 均为无 `Location` 的 404，未知 provider 为 400 JSON，4002/4003/4004 健康均为 200，共享正式镜像保持 `sha256:a5f9a71d4637c408f4fb0f66e940dfa6b3547d85f76fb801b3c5e57fa1c1d39c`，4004 最近日志异常匹配为 0。

**执行要求归一总结与漂移审计：已完成。** 本批没有实施结构迁移，也没有建立 facade、兼容层或新抽象；通用未知输入门禁取代所有非活动域专项断言，测试覆盖更直接且不保留专用分支。模块依赖、复用准入和安全边界均未变化；Phase 2 已完成，当前下一步为 Phase 3.0 任务模型与消费边界只读盘点。

**Phase 2.3A 总结与漂移审计：已完成。** 本批只建立可观察 HTTP 基线，没有把当前 Handler 结构或未来 route 模块名写成永久契约；交叉审查已移除直接导入副作用、动态 Date header 比较和“尚未迁移”断言。`/harness-ca.crt` 可进入独立 exact GET 迁移；`/assets/` 仍需先扩展最小前缀 Router 能力。授权附件、报表封面、视频 Range、邻聊/淘宝文件和动态导出继续排除在 Phase 2.3 通用化范围外。

**Phase 2.3B 总结与漂移审计：已完成。** 本批只迁移一个固定 GET 边界，URL、响应字节、headers、query、缺失和方法分派均保持不变；没有引入兼容层、通用下载抽象或跨领域复用，也没有触及 `/assets/`、Range、附件鉴权、业务状态或外部 API。固定路径由 composition root 显式注入，route 只依赖 Router 与 `core.http`，复用边界合理且无反向 import。下一步保持账本约束，只在 Router 内增加未解码 suffix 的最小前缀匹配能力，不在同一提交接线 assets；现存静态代理同步告警作为独立运行配置问题，不改变 Phase 2.3C 的代码范围。

**Phase 2.3C 总结与漂移审计：已完成。** 本批只增加一个已由相邻静态资源迁移明确需要的 Router 原语，未接入任何运行时路由，因此对外行为、数据、鉴权和业务状态均为零变更。模块仍为纯 stdlib，handler 只接收不可变的未解码 suffix，没有路径解析或文件职责，也不存在 `routes → web_app` 反向依赖；该能力的抽取时机与复用边界合理。终审没有发现需求漂移、兼容层、通用文件服务或为减少行数而扩大的抽象。下一步 Phase 2.3D 只接线 `/assets/`，删除对应旧内联分支并保持已冻结的全部 HTTP 与路径安全语义；Range、HEAD、授权附件及其他文件响应继续排除。

**Phase 2.3D 总结与漂移审计：已完成。** 本批只迁移普通静态资源 GET 前缀，没有触及授权附件、报表封面、视频 Range、邻聊/淘宝文件、动态导出、业务 API 或数据。路径安全与 MIME 属于静态资源 route，Router 仍只匹配并传递原始 suffix，职责边界清晰；固定根和 guesser 由 composition root 显式注入，无反向依赖、兼容层或跨领域抽象。测试同时覆盖真实隔离 HTTP 与结构接线，首次完整门禁捕获陈旧 UI 断言并在独立测试提交后从头重跑，证明门禁有效。需求、模块复用和安全语义均未漂移，Phase 2 可以关闭；下一步先做 Phase 3.0 四类任务模型、字典/锁、API/SSE 消费点的只读清单与逐字段基线，不直接引入共享基类或切换运行时。

**Phase 2 验收：已完成。** 新增一个纯页面或静态资源路由不需要编辑 `Handler.do_GET`/`do_POST`；CodeGraph 不存在 `routes → web_app`；原 URL、状态码、Content-Type、缓存 header 和 404/405 行为不变。业务 API 仍留在原位置，等待 Phase 3 的任务边界稳定后按垂直切片迁移。

**Phase 3.0 总结与漂移审计：已完成。** 四类任务只有 `id/status/created_at/updated_at/log/error` 六个共同内部字段，领域输入、结果来源、日志窗口和公开字段均不同，因此当前没有引入共享 dataclass、继承或 registry 的充分依据。Download 公共 payload 为 `id/url/status/created_at/updated_at/filename/error/log/result`，日志窗口 80 且 `result` 直接引用 job 上的可变字典；Shop 隐藏 `prompt`，公开输入字段、120 条日志以及从磁盘读取的 `extract/analysis`；Metrics 和 Amazon 各公开领域输入、120 条日志及从磁盘读取的 `result`。四类 GET 在各自锁内查找并序列化，SSE 也在锁内调用相同 serializer，缺失 GET 是领域错误文案的 JSON 404，缺失 SSE 则保持 HTTP 200 并发送 `status=missing` 后关闭；SSE 变化标记严格为 `status/updated_at/log长度/error`。POST 启动线程后才序列化 202，初始 `queued/running` 存在现有竞态，测试不得错误收紧为固定 queued。现有生命周期测试只覆盖 Download complete、Shop complete、Metrics failed，Amazon、逐字段集合、四类缺失、日志截断/复制和 artifact 结果均未完整冻结。

审计未发现需求漂移或新的反向依赖，但发现 `handle_download` 的输入校验失败分支在登记 failed job 后调用全仓无定义的 `write_download_job_log`，可能在预期 JSON 400 前抛出 `NameError`。该问题必须以独立行为修复提交先收口，不得与快照结构搬迁混合；其后 3.1A 新增唯一任务快照契约脚本，完整门禁基线相应从 **50+2=52** 提升为 **51+2=53**。3.1B 只新增零接线的领域纯 adapter，并显式验证深复制后的不可变快照与现有外部 JSON 等价；不得把持锁磁盘读取优化、任务清理策略或 POST 初态竞态改造夹带其中。

Phase 3.R1 最终证据（2026-08-29）：`6111887` 只从 `handle_download` 校验失败分支删除全仓无定义的调用，保留 failed job 同锁登记、原错误字段/日志、成功请求、线程启动和全部任务结构；既有 `test_web_workflow_lifecycle.py` 通过真实 HTTP 新增精确 400、JSON Content-Type、错误 payload，以及紧随其后的 `/healthz` 200/完整 payload，原 `NameError` 会在响应前断连，测试不存在 fallback 或异常吞噬。Terra 实现、语义/架构审查和测试有效性审查均为 0 blocker；主审进一步把宽松的非空 error 断言收紧为完整 payload。服务器只通过 4004 部署脚本构建，提交 `61118872222116697a7cdd91808a483d5efdd1b7`、镜像 `sha256:cbf06833c8ece18e7fb35d6ed30f81a10830beea38442950b97c075bd255c367` 最终在一次从第 1 项开始的连续运行中通过 **50 项确定性回归 + 2 个 Playwright = 52 项**；13 页面为 200、无效下载为 400、未知 provider 为 400、三端健康为 200，4002/4003 镜像与启动时间均未变化，服务器 checkout clean，代码异常日志为 0，仍只有 1 条已跟踪的静态代理配置告警。

**Phase 3.R1 总结与漂移审计：已完成。** 本批是独立行为漏洞修复，不是结构迁移；没有新模块、兼容层、持久化、锁范围、API 字段、SSE、成功下载语义或反向依赖。测试用真实 Handler 和第二个健康请求证明旧异常不可被 helper 掩盖，复用边界未变化。审计同时记录两项流程偏差并已纠正：Terra 在本地依赖缺失后越界尝试一次本地 Docker run，但 daemon 未运行且没有构建或状态变更，该结果未作为证据；服务器全量门禁前两次分别漏传测试 mock 开关、错误沿用日报关闭开关，均为编排 fail-closed，修正后已从第 1 项完整重跑而非拼接结果。部署后零残留复扫发现服务器忽略文件中仍有 3 行历史环境配置和 `data-dev` 备份中的 1 条历史消息，已只删除对应行/消息；tracked 源码、工作树内容/文件名、镜像、容器环境及 `data`/`data-dev` 最终均为 0，未重启或触碰 4002/4003。下一步进入 3.1A，只新增快照契约测试，不改运行时代码。

Phase 3.1A 最终证据（2026-08-29）：`a728a5c` 只新增 `scripts/test_job_snapshot_contract.py`，运行时代码、URL、API/SSE 实现、数据目录和 UI 零改动。专项逐字段冻结四类 public payload 与排除字段、GET 200/缺失 JSON 404、终态及缺失 SSE 200、Download 80 条与其余三类 120 条日志窗口/切片副本、Download `result` 当前 Python alias、Shop/Metrics/Amazon artifact 覆写后的磁盘重读与对象独立，以及 SSE marker 对 `status/updated_at/log长度/error` 的逐项触发；所有 fixture、环境和四个全局 store 均在失败路径清理。Terra 实现、两路交叉复验和三路阶段后审计均为 0 blocker；CodeGraph 仅新增测试消费现有 Handler/serializer 的边，不存在生产反向依赖、adapter、registry 或共享基类。服务器只通过 4004 部署脚本构建，提交 `a728a5c13dac5f23c944991f30d34612deadc4ee`、镜像 `sha256:def0f3dfd2ed829497c29fc56a23cddbbb518a3f73f75c1332bb8e2e068d30f7` 在一次从第 1 项开始的 fail-fast 运行中通过 **51 项确定性回归 + 2 个 Playwright = 53 项**；13 页面、四类缺失 GET/SSE、未知 provider、三端健康、clean checkout 与代码日志均通过，4002/4003 镜像和启动时间未变化。首次宿主零残留扫描因浏览器 profile 权限不足而不作为证据，随后以 4004 镜像 root 身份只读挂载全 checkout 复扫，内容/文件名/扫描错误均为 0，镜像与环境也为 0。

**Phase 3.1A 总结与漂移审计：已完成。** 本批只建立迁移前可观察基线，没有修改生产行为、兼容层、锁范围、持久化或任务生命周期；测试依赖方向是 `test → web_app`，不构成生产反向依赖。四域的公开字段、日志窗口与结果来源仍不同，因此现在引入共享 DTO、基类或 registry 都是不合理的过度复用。下一步 Phase 3.1B 只新增四个按领域显式补字段的纯 snapshot adapter 及其单测，零运行时接线；不得提前迁移四套字典/锁、优化锁内磁盘读取、固定 POST 初态竞态，或改变现有 GET/SSE、artifact 时序和 Download alias 的外部可观察语义。

Phase 3.1B 最终证据（2026-08-29）：`c60119d` 只新增 `scripts/jobs/__init__.py`、`scripts/jobs/snapshots.py` 与 `scripts/test_job_snapshot_adapters.py`。四个函数逐域显式输出 3.1A 已冻结字段，Download 日志窗口为 80、其余为 120；Download `result` 与 Shop/Metrics/Amazon 显式注入的 artifact/result 均深复制，`None` 原样保留，adapter 不读磁盘、不持锁、不导入 `web_app`/route/service，也没有 registry、共享基类或运行时调用者。纯 stdlib 专项逐域验证精确字段、排除字段、窗口、新列表、输入不变、嵌套对象双向隔离与 `None`；交叉审查发现并补齐 Download `result=None` 后三路阶段审计均为 0 blocker。服务器只通过 4004 部署脚本构建，提交 `c60119dd3d51f38ca77dbabb2a796de2539a1831`、镜像 `sha256:d1d8d08af071288a7b0c9608bb5afc1b37e00f3b69a33b5ad760cdf86b6868e4` 在一次从第 1 项开始的 fail-fast 运行中通过 **52 项确定性回归 + 2 个 Playwright = 54 项**；13 页面、四类旧任务缺失 GET/SSE、未知 provider、三端健康、clean checkout、代码日志和 root 只读零残留均通过，4002/4003 镜像与启动时间未变化。

**Phase 3.1B 总结与漂移审计：已完成。** 本批建立的是零接线内存快照能力，外部 API/SSE、磁盘读取时序、四套字典/锁、任务线程和数据均未改变。四个函数同置一个窄模块但不共享字段表、dispatcher、base helper 或模型，少量重复保留了领域差异；`Any` 仅用于避免对仍位于 `web_app.py` 的四个 dataclass 建立反向依赖，待模型真正迁入 `jobs` 后再评估收窄。下一步 Phase 3.2 只实现纯 `JobRegistry` 与并发专项：锁内注册、查找、日志追加、状态/快照读取、missing 和异常释放锁；不得读取 artifact、执行线程/命令、处理 HTTP/SSE、持久化数据、归一领域字段或接线/替换 `web_app` 的字典与锁。

Phase 3.2 最终证据（2026-08-29）：`fed5df9` 只新增 `scripts/jobs/registry.py` 与 `scripts/test_job_registry.py`。普通 `JobRegistry` 仅使用 stdlib、私有字典和一把 `Lock`，显式 key 注册时深复制接管对象，重复注册拒绝覆盖；`snapshot/status` 保持只读 missing 为 `None`，日志追加保持原 `rstrip()` 和同锁时钟更新，写入 missing 抛 `KeyError`。没有 Protocol、共享基类、领域字段表、callback、artifact/I/O、线程启动、持久化、HTTP/SSE 或生产调用者。并发专项覆盖 8×40 日志无丢失/重复、输入与快照双向隔离、真实 append/snapshot 锁重叠、deepcopy 异常后释放锁和缺失/重复语义。交叉审查曾发现并收口“共享 Protocol 过度固化”和“append/snapshot 未真实重叠”两个 blocker。服务器只通过 4004 部署脚本构建，提交 `fed5df9548d905ed4bd72e6637d31d615c52268c`、镜像 `sha256:4e9e1c247d1296faa7d172abd5fefb0a38cf06c3e395fd921f4d63f2855405a7` 通过 **53 项确定性回归 + 2 个 Playwright = 55 项**；13 页面、四类缺失 GET/SSE、未知 provider、三端健康、clean checkout、近期日志、镜像/环境与 root 只读零残留均通过，4002/4003 镜像和启动时间未变化。

**Phase 3.2 总结与漂移审计：已完成。** 本批是零接线纯能力，四套字典/锁、worker、serializer、artifact 时序、URL、API schema、SSE、任务状态和数据均未改变；`jobs` 只依赖 stdlib，`Any` 避免对仍在 `web_app.py` 的模型形成反向依赖，没有 facade、兼容层或过度共享模型。阶段后审计确认 3.2 为 0 blocker，但禁止直接开始运行时切换：注册时深复制后旧 worker 持有的原对象不再是 registry 内对象，当前 API 又没有安全的受锁字段更新入口；下载结果的现有 Python 对象身份和后三域锁内 artifact 读取范围也不能在结构迁移中静默改变。下一步先做 3.3.0 只读更新点/锁时序清单与契约决策，再决定最小受锁更新原语或独立可靠性修正；不得双写旧/新 store、访问 `_jobs/_lock`、引入通用业务状态机/callback 框架，或把磁盘 I/O 塞进 registry。

**Phase 3.3.0 更新与锁时序审计：已完成。** 四域均在锁内登记后只把 `job_id` 传给 worker；worker 锁内取得 live 对象并更新内存字段，命令、网络、缓存、文件写入和 Metrics 结果登记均在锁外。共同更新为 `status/updated_at/log`，领域更新分别为 Download 的 `filename/result/error` 与 Shop/Metrics/Amazon 的 `output_dir/error`。整体对象 `replace` 会覆盖并发日志，继续修改注册前对象则因 Registry 接管深复制而写不到唯一真相，双写和私有成员访问均被否决。下一步 3.3.P1 只给 Registry 增加受锁、非 callback 的原子 `update_fields`：调用方传字段 mapping，Registry 只校验对象已有非私有属性、拒绝 `log/updated_at`，先深复制全部值再一次提交并由注入时钟更新时间；允许在同一原子更新中附加一条不归一化日志，以保持失败终态和末条日志不产生额外 SSE 半状态。不得引入领域字段表、状态机、整对象 replace、live get、版本/CAS、I/O 或线程职责。

审计同时确认 Download `result` identity 只在进程内 helper 可见、HTTP/SSE 只观察 JSON 值，但既有测试已显式冻结；其 defensive copy 必须作为 Download 接线前的独立可靠性提交，不混入结构迁移。Shop/Metrics/Amazon 的锁只保护内存字段，不保护锁外子进程的 artifact 写入；迁移时由 Registry 先取不可变内存快照、再由领域 adapter 读 artifact，不等同于把 I/O 交给 Registry。每个域仍须用确定性时序测试证明 artifact 读取次数、终态先后、POST queued/running 竞态、GET/SSE marker 与 missing 行为保持基线。迁移顺序定为 Download → Metrics → Shop → Amazon，每域独立提交和完整门禁。

## 七、Phase 3：任务模型与注册表归一

本阶段提前到业务路由迁移之前，避免新 route/service 反向访问 `web_app.py` 中的 `download_jobs`、`shop_jobs`、`metrics_jobs`、`amazon_jobs` 和四把锁。不要先用继承强行统一四类任务；先定义稳定的只读快照协议：

```python
class JobSnapshot(TypedDict):
    id: str
    status: str
    created_at: float
    updated_at: float
    log: list[str]
    error: str | None
```

按以下子阶段实施，每个子阶段独立提交并执行当前登记的 55 项完整门禁；后续新增专项时继续同步提高总数：

1. **3.R1 下载校验失败分支修复：** 以独立行为修复提交删除或替换未定义调用，并在既有隔离 lifecycle 中冻结 JSON 400；不改任务结构、成功请求或持久化策略。
2. **3.1A 快照基线：** 用唯一专项逐字段锁定四类任务当前的 API/SSE 输出、缺失差异、日志窗口/复制、artifact 读取和初态竞态；本批不改运行时代码。
3. **3.1B 纯快照 adapter：** 为四类任务分别增加纯 `snapshot()` adapter，业务专属字段继续由各 adapter 显式补充，零运行时切换且不要求共享 dataclass 基类。
4. **3.2 `JobRegistry`：** 统一锁内注册、查找、日志追加、状态读取和不可变快照；测试并发 append/snapshot、任务不存在和异常后锁释放。Registry 不启动线程、不执行业务命令、不持久化数据库。
5. **3.3.0 更新与锁时序契约：** 先只读列出四域每个字段赋值、日志、serializer、artifact 读取、线程启动与 GET/SSE 锁范围；明确下载结果对象身份和后三域锁内 I/O 的处理方式。若需要可靠性变化，必须独立提交、独立测试；本批不接线、不双写、不访问 Registry 私有成员，也不预设通用 callback。
6. **3.3.P1 原子字段更新纯能力：** 扩展现有 Registry 与同一专项，不新增运行时调用者；冻结原子多字段、字段校验、值深复制、同锁可选末条日志、时钟、missing、异常零部分写和 append 并发不丢日志。
7. **3.3 调用方切换：** 在 3.3.P1 通过后，按 Download → Metrics → Shop → Amazon 单域独立提交切换 registry；Download 接线前先以独立可靠性提交处理已冻结的 result identity。每切换一类都验证 API 查询与 SSE 字节契约。SSE 端点只消费快照，不直接访问可变任务对象。
8. **3.4 基类复核：** 只有发现除快照字段外还有稳定共享行为才评估基类；若只是少量字段复用，明确记录“不引入继承”。

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

`proxy_pool.py` 不应与普通 web service 一起大搬。按事务边界拆，并在每个子阶段执行当前登记的至少 52 脚本门禁及代理专项故障注入：

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

先为现有 DeepSeek/Qwen 调用补 contract test，再引入 transport adapter。`hot_video_report.py` 保持显式 `max_tokens`，不能依赖隐式默认值。每个 transport/provider/chat 子阶段仍执行当时登记的完整门禁，专项测试不能替代完整门禁。

Transport 规则必须显式化：只对连接失败、429 和可重试 5xx 在首个响应字节前重试；次数和退避由调用方配置；流式输出开始后不得自动重放；带副作用的工具调用不得由 transport 重试；错误类型和 usage 合并规则对调用方保持兼容。请求快照必须删除认证头、Cookie、真实媒体 URL 和用户内容，只保留合成 fixture。

**Phase 6 验收：** 三 provider 工具边界测试、日报 LLM fallback、翻译、postprocess、direct-video 分别通过；请求 payload 与基线快照一致；聊天 route 不导入 `web_app`，provider 工具域、会话作用域和官方 Skill 白名单继续 fail closed。

## 十一、Phase 7：前端资源拆分

后端 API 稳定后再处理 `proxy.html`：

- HTML 保留语义结构。
- CSS 迁入独立、版本化资源。
- 数据请求、状态 store、drawer workflow 分成小型原生 JS 模块。
- 共享导航继续由 `ui-system.css/js` 提供。

静态资源变化必须更新 `UI_ASSET_VERSION`。聊天壳只服务 Home、SellerSprite、出海匠，不拆出三套页面。每个可独立部署的资源拆分子阶段执行当时登记的完整门禁，并检查两个 Playwright 的桌面/移动 viewport、控制台和页面错误。

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
  → Phase 0.5 共享能力中性化与资产清理
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
- 4004 活动功能的 URL、API schema、SSE 格式、数据目录和 Compose 隔离保持兼容。
- `core` 不包含 provider/tool/业务实体专属规则；provider adapter 注入的规则必须全部属于活动注册表。
- CodeGraph 中不存在 `routes/services/jobs/core → web_app`、`services → routes` 或 `core → 领域模块` 的反向依赖。

## 十四、下一批实施任务

Phase 0、0.5、1.1、2026-08-29 两个补漏阶段、Phase 1.2、Phase 1.3、**Phase 2.1～2.3D**、**Phase 3.0**、**Phase 3.R1**、**Phase 3.1A**、**Phase 3.1B**、**Phase 3.2** 与 **Phase 3.3.0** 已完成。Phase 2 无业务状态路由骨架关闭；Phase 3 已建立四类任务公开契约、领域纯快照 adapter、零接线纯 Registry，并完成字段更新与锁/artifact 时序审计。Amazon 与出海匠聊天壳延期到 Chat/Provider 阶段，Proxy 页面延期到代理垂直切片。下一步实施 **Phase 3.3.P1 原子字段更新纯能力**；通过前不得切换任一运行时调用方。

Phase 2.3 继续复用现有 Terra 子智能体，避免为同一长期任务无限新增执行记录，并按以下门槛推进：

1. 2.3A 只补契约、不接线（已完成，`8e17845`）：为 `/assets/` 冻结合法/嵌套资源、MIME、Content-Length、cache、query、原始 URL suffix、解码后路径穿越、缺失 JSON 404 与现有 HEAD 404；为 `/harness-ca.crt` 冻结存在/缺失、证书 headers、正文与现有 HEAD 404。测试只使用临时工作树与假夹具。
2. 2.3B 固定证书 exact GET 小步迁移（已完成，`d720c25`）：只接收固定证书路径，未抽象成通用下载框架，未与 `/assets/` 同提交；HTTP/AST 专项、CodeGraph、52 项完整门禁和部署黑盒均通过。
3. 2.3C Router 最小前缀能力（已完成，`2b6ad36`）：实现 exact/template 总体优先、最长前缀、重复冲突及未解码不可变 suffix；没有 glob、正则、全局 URL decode、自动 HEAD→GET、middleware 或 `/assets/` 运行时接线，专项、CodeGraph、52 项完整门禁和部署黑盒均通过。
4. 2.3D `/assets/` 独立接线（已完成，`656282d`、`2cd64a5`）：保持 `unquote → resolve → containment`、400/404 JSON、MIME、Content-Length、query/尾斜杠/Range 与 `no-cache, no-store, must-revalidate`；旧内联分支和 Handler 方法已删除，专项、CodeGraph、52 项完整门禁和部署黑盒均通过。
5. 主代理逐步复核 CodeGraph 导入方向、测试有效性与用户脏文件边界，每个新增专项脚本都同步提高当前 **53 项确定性回归 + 2 项 Playwright = 55 项** 的完整门禁总数。

停止结论必须保留：`/assets/` 是动态多段前缀，现有 Router 的 `{param}` 不跨 `/`，不能伪装成 `/assets/{path}`；路径安全、URL 解码和 MIME 归属 route，不归 Router。`/harness-ca.crt`、普通 assets、授权附件、报表封面、视频 Range、邻聊/淘宝文件、frames/PDF 动态生成是不同响应边界，禁止为了复用而合并。尤其不得用 `file_response` 未审计替换 `serve_video`、为 `binary_response` 自动增加 Range、扩大 HEAD 支持、绕过 owner/token 检查或统一 Content-Disposition。任何一项需要改变既有 HTTP 语义时，立即停止该代码批并先补现状基线与显式变更决策；不得越过 Phase 3 直接迁移业务 API。

Phase 3 下一批按以下门槛推进：

1. **3.0 只读盘点（已完成）：** CodeGraph 与三路交叉审计已列出 `DownloadJob`、`ShopJob`、`MetricsJob`、`AmazonJob` 的字段、四套字典/锁、所有创建/更新/查询/日志/SSE 消费点，以及 API 返回的领域专属字段；运行时代码零改动。
2. **3.R1 行为漏洞收口（已完成，`6111887`）：** 只处理下载输入校验失败路径的未定义调用，既有 lifecycle 已冻结预期 400 JSON 与服务可继续响应；服务器 52 项完整门禁通过。
3. **3.1A 快照基线（已完成，`a728a5c`）：** `scripts/test_job_snapshot_contract.py` 已逐字段冻结四类任务当前的 API/SSE 可观察输出、四类缺失任务、日志 80/120 窗口与复制、下载结果 alias 现状、后三类 artifact 重读和 SSE marker；运行时代码零改，服务器 53 项完整门禁通过。
4. **3.1B 纯 adapter（已完成，`c60119d`）：** 四个领域显式 snapshot adapter 已深复制 result/artifact、保留 80/120 日志窗口并以纯 stdlib 专项冻结；生产调用者为 0，服务器 54 项完整门禁通过。
5. **3.2 纯 JobRegistry（已完成，`fed5df9`）：** 只新增 registry 与并发专项，负责锁内注册、查找、日志追加、状态/快照读取、missing 与异常释放锁；生产调用者为 0，服务器 55 项完整门禁通过。
6. **3.3.0 更新与锁时序契约（已完成）：** 四域更新点和时序已逐项审计；否决双写、整对象 replace、live get、私有成员访问和通用 callback，确认 artifact 写入不受旧内存锁保护，并确定 Download → Metrics → Shop → Amazon 的单域顺序。
7. **3.3.P1 原子字段更新纯能力（当前）：** 只扩展 `JobRegistry` 与既有专项，增加字段 mapping 的原子深复制更新和同锁可选末条日志；零运行时接线、零领域字段表、零 I/O/线程/状态机，完整门禁总数仍为 55。
8. Phase 3 不把下载、Shop、Metrics、Amazon 的业务专属字段塞入共享基类；`jobs` 不得导入 route、service 或 `web_app`，SSE 归一只消费不可变快照。
