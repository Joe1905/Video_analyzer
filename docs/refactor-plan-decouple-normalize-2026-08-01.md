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
9. 活动聊天 provider 收紧为 Home、SellerSprite、出海匠；`/fastmoss*` 只保留到 `/chuhaijiang` 的 307 兼容重定向。代理页已补齐共享 `.ui-header` 契约。

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
- 路由兼容：`/amazon/`、`/chuhaijiang/`、`/fastmoss*` 的规范化或重定向。
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
| `GET/POST /fastmoss`、`/fastmoss/`、`/fastmoss/<tail>` | 307，`Location: /chuhaijiang` |
| `GET /amazon/<tail>`、`/chuhaijiang/<tail>` | 404 JSON `{"error":"Not found"}` |
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
| `test_chat_tool_normalization.py` | 仍是 V1/V2 混合套件；包含已退役 FastMoss provider 断言 | 按活动 provider 拆出 V2 套件；旧 FastMoss 仅保留重定向/历史兼容测试，不得恢复 provider |

已知的运行时与 UI 红灯已关闭。Phase 0 尚未完成的是新增统一 HTTP smoke、拆分混合聊天套件、落库测试矩阵与首批响应契约。这些不能笼统标记为“历史问题”后继续重构：每项必须最终落为活动 V2 套件中的 `pass`、带明确条件的 `skip`，或独立的旧 FastMoss 兼容测试。

### 4.4 建立测试矩阵

按风险域维护最小矩阵：

- 聊天：provider 路由、会话隔离、官方 preset 白名单、工具执行前复核。
- 日报：暂停/恢复、单视频重试、缓存占位失效、LLM fallback、数据库生命周期。
- 代理：节点生命周期、删池解绑、重新绑定、发布/采集任务暂停与恢复。
- 视频：超时诊断、孤儿进程清理、direct-video prompt/压缩回退。
- UI：共享导航、三 provider 共享壳、代理绑定抽屉、日报禁用态。

响应基线存放在 `scripts/contracts/`。快照生成时只允许规范化随机 ID、时间戳、临时绝对路径和日志时间前缀；字段缺失、状态码、重定向目标、SSE event/data 结构不得被归一掉。快照中只能使用合成数据，不得写入凭据、Cookie、真实账号或请求头。

**Phase 0 验收：** 服务器 4004 容器中 smoke 通过；混合聊天套件完成拆分；现有测试没有“来源不明”的红灯；测试矩阵写入仓库。

## 五、Phase 1：抽取低风险基础设施

每个子步骤独立提交，保持行为不变。

### 5.1 `core/http.py`

先迁移 `json_response`、`text_response`、`binary_response`、`file_response`、请求体读取、SSE 写入。路由仍留在 `Handler` 中，只把稳定纯工具替换为显式导入。

验收：HTTP smoke、SSE 相关专项测试、禁用功能 404 分支通过。

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

静态资源变化必须更新 `UI_ASSET_VERSION`。不得借重构恢复旧 FastMoss 页面或拆出三套聊天壳。

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
- 4004 的 URL、API schema、SSE 格式、数据目录和 Compose 隔离保持兼容。
- CodeGraph 中不存在 `routes → web_app`、`service → routes` 的反向依赖。

## 十三、建议的第一批实施任务

第一批只做 Phase 0 和 Phase 1.1，控制在 3～5 个小提交：

1. 新增可重复运行的 `test_web_smoke.py`。
2. 拆分 `test_chat_tool_normalization.py`：活动 V2 provider 测试、SellerSprite mock 边界、旧 FastMoss 重定向兼容各自独立；禁止通过恢复 FastMoss provider 让旧断言通过。
3. 为禁用日报、禁用代理的 404/提示分支加契约测试。
4. 抽 `core/http.py`，保留 `web_app.py` 显式兼容导出。
5. 在 Docker 4004 环境跑 smoke 与专项测试后再进入配置拆分。

这一批不触碰聊天权限逻辑、代理事务、Job 继承或前端资源拆分。
