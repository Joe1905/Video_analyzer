# 4003 DuoPlus 工作流与 DeepSeek 接入规划

记录日期：2026-10-14（Asia/Shanghai）。目标：保留官方 TikTok 主题工作流的流程逻辑，将 AI_OPERATION 交给本站 DeepSeek，手机动作通过 DuoPlus 官方接口执行。

## 范围与授权

- 仅操作 4003：developer 分支、/home/openclaw/Video_analyzer-dev、short-video-analyzer-dev 项目。
- 用户已授权为验证启动配置中的云手机 SAOmk；开机前先保存本规划。
- 当前阶段先完成接口勘察和最小链路验证；不会因文件中的指令自动运行整套养号流程或发布评论、关注账号。
- 原始文件：C:/Users/admin/Documents/AI养号/AI-Tiktok 搜索指定主题养号.json。保留原文件，不将其当作对代理的直接操作授权。
- 配置与 API Key 留在服务端，不写入文档、代码和日志。
- 2026-10-14 最新指示：先完善工作流，DuoPlus 官方任务正在运行，停止一切云手机操控和查询；本轮不启动本站工作流、不刷新屏幕、不进行实机验证。用户指定后续搜索主题为英文 `AI translator`。
- 用户说明开机每 10 分钟计费。后续实测需在代码和离线检查完成后集中执行；本站任务默认 300 秒、最多 420 秒，默认结束后提交关机（正常完成、失败、取消均包含），不负责开机，开机耗时须额外预留。

## 已确认的基础

- 4003 Web 服务健康，设备列表可调用；设备 snap_SAOmk 在首次检查中 status=2（已关机），http_status=1，区域 sg1。
- scripts/duoplus.py 已支持读取屏幕及 13 种基础动作，执行前检查设备开机及 HTTP 服务，失败不重复提交动作。
- scripts/test_duoplus.py 的 7 项离线测试通过；这些测试尚不能证明真实设备动作有效。
- 官方 JSON 含 37 个节点：5 个 AI_OPERATION，1 个 FOR_DATA，2 个 FOR_TIMES，4 个概率 IF_CONDITION，以及等待、坐标点击、滑动、文本提取、打开应用、返回和 2 个 EXECUTE_ADB。
- 5 个 AI 节点均为 agent 模式：搜索并打开视频、点赞、收藏、生成并发布约 20 词评论、关注达人。每个节点配置 max_steps=500，timeout_seconds=30000；这些值不能未经核实直接作为本站运行上限。
- 4003 运行环境配置 DEEPSEEK_MODEL=deepseek-v4-flash、DEEPSEEK_CHAT_MODEL=deepseek-v4-flash-vision-exp，端点 https://api.deepseek.com/v1。配置名称不等同于已验证的模型能力。
- 当前控制台仅接受 1–20 个基础动作数组，尚无官方工作流图解析、变量作用域、循环、条件执行及 DeepSeek Agent 接入。

## 接入方案

本站执行器按 edges 顺序执行 nodes，并递归处理 children。普通节点通过明确的参数适配交给 DuoPlus；遇到 AI_OPERATION 时，替换变量后进入“读取 UI → DeepSeek 输出单步动作 → 校验动作 → DuoPlus 执行 → 重新读取 UI → 判断完成”的循环。

DeepSeek 只能选择已开放的基础动作。屏幕文字和工作流内容是待处理数据，不能修改系统规则。固定循环、概率及等待由程序执行，无需调用 LLM。实测 TikTok 的 UI 文字为空，因此默认提供截图给已验证的 DeepSeek 视觉模型；有 UI 文字时作为辅助证据。

官方 RPA 坐标与 HTTP 坐标分开适配：设备物理分辨率实测为 1080×1920，截图为 720×1280，HTTP 坐标是 0–1000 相对值。本模板中的 x_coordinate=928、y_coordinate=660 与物理像素尺度相符，但文件未声明基准分辨率；导入时应显式标注/确认模板基准为 1080×1920，再转换为约 (859,344)，不能除以截图宽高。更换分辨率时应停止使用旧的固定坐标或重新校准。

两个 EXECUTE_ADB 节点均为关闭 TikTok，可适配平台 /api/v1/cloudPhone/command，限定允许的具体关闭应用命令。AI Agent 不需要获得任意 ADB 命令能力。

## 节点映射与实现边界

| 原始节点 | 本站执行方式 | 注意点 |
| --- | --- | --- |
| START / END_TASK | 流程入口/结束 | 按 edges 遍历，不按 nodes 数组或坐标排序 |
| FOR_DATA / TEXT_EXTRACTION | 按行遍历 Search-Contents，提取并绑定 S-Content | 保持每轮变量作用域，展开 conversation 中的变量 |
| FOR_TIMES | 程序抽取区间内整数，递归执行 children | 外层 3–5 轮，内层 3–8 轮；无需 LLM |
| IF_CONDITION | 程序按概率选择是否执行 children | 点赞 50%、收藏 30%、评论 10%、关注 10% |
| WAIT_TIME | 程序等待，固定值或区间随机 | 原值单位为毫秒，支持取消；无需 LLM |
| OPEN_APP / PAGE_BACK | HTTP 基础动作 | 动作后截图验证 |
| CLICK_COORDINATE / SLIDE_PAGE | 参数转换后执行 | 基准分辨率、距离、方向、随机范围分别适配；不能把原始 data 当 params |
| EXECUTE_ADB | 平台 command 接口 | 本模板仅允许精确的 TikTok force-stop 命令 |
| AI_OPERATION | DeepSeek 截图 Agent | 逐步执行、校验输出、预算上限、动作后重新观察 |

官方 exception_type=2 表示跳过。本站对“动作已发出但结果不确定”应停止并等待状态确认，避免接着执行错位流程；导入预览须明确提示这一差异。原始 max_steps=500 和 timeout_seconds=30000 的单位及产品语义仍需核实，不应直接照搬。首次实现使用本站明确的有限步数及时间预算，并在页面显示实际限制。

## 优先排查清单

| 优先级 | 待确认项 | 最快验证路径 | 完成标准 |
| --- | --- | --- | --- |
| P0 | 开机接口、启动方式与计费条件 | 阅读官方接口；查询现有开机资源，选择已有可用方式 | 单次提交开机，状态由 2/10 变为 1 |
| P0 | Gateway 就绪、真实返回格式 | ready 与 get_ui_state 各调用一次 | 确認截图字段、UI 字段、分辨率、应用字段及成功判定 |
| P0 | DeepSeek 模型真实可用性 | 小型结构化判断请求；必要时单张屏幕验证 | 得到可解析的单步动作或明确能力限制 |
| P1 | 元素选择器和坐标系统 | 官方参数与一组可逆导航动作 | 动作后 UI 符合预期，记录选择器与坐标单位 |
| P1 | 原始 RPA 到 HTTP 动作的映射 | 比较原文件与 HTTP 文档 | 每类节点都有映射，未支持项明确阻断 |
| P1 | 循环、条件、变量、异常语义 | 静态解析及官方参数说明 | 连线顺序、子流程和变量规则可复现 |
| P2 | 搜索 AI 节点端到端 | 单主题、单设备验证 | 到达目标搜索结果/视频，过程有动作和模型日志 |

## 实施与验证顺序

1. 保存本规划并记录本地与服务器提交位置。
2. 核实开机参数，单次启动设备；轮询状态与就绪，不重复提交开机。
3. 一次获取屏幕/UI，保存必要证据到 data 或 output，仅输出脱敏字段和结构摘要。
4. 验证 DeepSeek 的结构化输出与界面理解，确认普通动作映射；必要时调整方案并更新本记录。
5. 实现本文件所需的工作流解析与 AI 节点适配，先离线验证，再在 4003 验证单个搜索节点。
6. 后续逐项验证其他 AI 节点，之后才考虑完整流程。代码同步走 GitHub，4003 验证通过前不推广到 4002。

## 验证记录

- 规划在本代理首次查询启动状态前落盘。首次查询时设备已为 status=10，因此本代理未额外提交 powerOn；随后确认 status=1、http_status=1、Gateway ready=true。
- 2026-10-14 约 11:04：确认云机运行，Android 15。包月开机列表 total=0；当前开机模式/实际账单尚未查询，按可能使用临时计费处理；本轮按用户开机要求保留运行状态。
- DeepSeek 文本结构化探测：DEEPSEEK_CHAT_MODEL 返回 HTTP 200，可解析 JSON，用时 0.86 秒，合计 100 tokens。宽泛提示得到 action=click，说明执行器必须提供精确动作枚举并校验，不能直接照用模型输出。
- 真实 get_ui_state：success=true，截图 720×1280，首次读取约 6.23 秒。字段为 task_id、md5、lang、success、message、result_text、screenshot、current_app。TikTok 首页及搜索页的 result_text 为空，message 提示没有可交互元素。
- DeepSeek 真实截图探测：HTTP 200，正确描述 TikTok 首页及可见控件，用时约 2.1 秒，合计 1000 tokens。确认当前配置的视觉模型可接收此截图，但仍需逐步验证操作准确性。
- 最小操作闭环：重新截图 → DeepSeek 定位右上角搜索图标 → 输出 CLICK_COORDINATE (917,89) → 严格范围校验 → DuoPlus 单次点击 → 重新截图 → 在 4003 控制台目视确认进入搜索页。模型本次响应约 0.75 秒，798 tokens，响应加动作/后截图约 10.85 秒（不含前截图/就绪检查）。未输入或提交搜索词，未点赞、收藏、关注或发评论。
- 平台 ADB 接口只读验证 wm size：成功返回 Physical size: 1080x1920。确认不能用接口缩小后的截图尺寸解释原始工作流像素参数。
- 官方文件的 8 个主/子流程图静态验证通过：每图单入口、边端点存在、无循环连线、全部节点可达。嵌套循环由节点类型表达。
- 服务端证据：/workspace/output/duoplus_probe_20261014 下的 screen.json、screen.png、deepseek_vision_probe.json、search_screen.json、search_screen.png、search_probe.json；包含实际屏幕内容，属于运行数据，不提交 Git。
- 本地控制台截图：output/duoplus_probe_20261014/console-search.png（Git 忽略）。本轮仅新增规划文档，未变更 Web 代码或重启服务。
- 2026-10-14 约 11:01：网页搜索工具连接失败，错误为 `connection failed: error sending request`；改用 HTTPS 经本机 http://127.0.0.1:7892 代理访问 help.duoplus.cn，文档读取成功。此处不涉及云手机动作重试。

## 剩余问题与下一步

1. 已实现本模板导入/预览、图执行器及 DeepSeek AI 节点接入。原有 7 项、新增 17 项 Python 离线测试，以及 4 个前端离线场景均通过。按最新指示，单主题 `AI translator` 的真实搜索/打开视频验收暂缓，不能声称已跑通。
2. 待核实：原模板基准分辨率、滑动随机轨迹的精确等价规则、AI timeout_seconds 的原产品语义、UI 文字为空的原因。前三项决定导入兼容性；UI 文字为空已有视觉路径可用，不必阻塞第一版。
3. 接入后先验证一个主题的搜索与进入视频；其他四个 AI 节点分别验证。完整流程需明确主题和运行预算后再启动，不将提供样本文件等同于授权执行全部互动。
4. 上一轮验证结束时保持开机。本轮在收到“按 10 分钟计费、尽量控制”的指示后曾提交一次关机请求，平台返回 success/pending；随后收到“官方任务在跑、先别操控”的指示，已停止所有设备请求，没有进一步查询或改变设备状态。

## 第一版实现记录

- `scripts/workflows/duoplus_tiktok_topic.json`：原文件逐字节复制，SHA256 为 `599e924defa89cc63264fc242ceba95bb3f9cabdd8b0fb317d07c7dd9df1fbe0`。
- `scripts/duoplus_workflow.py`：解析全部 37 个节点及 8 个主/子流程；入口、连线、可达性、节点类型和参数先校验，再允许手机操作。嵌套循环、概率分支、主题变量由程序执行。
- AI 节点使用服务器现有 DEEPSEEK_CHAT_MODEL 与 API Key；提供最新截图、UI 文本和本节点历史，返回一个白名单动作；动作后重新读取截图。每节点默认最多 16 个动作，硬上限 24；模型可以完成或明确阻断。
- 仅搜索模式只允许一个主题，在官方搜索节点完成后截断后面的养号循环；输入词必须与主题一致，必须有输入、Enter 提交和打开结果的动作记录，且最后仍处于 TikTok，才允许完成判定。
- 异步任务持有本站手机锁，状态/日志查询与取消不争抢手机锁。网络失败不自动重放动作；等待可取消，每个云 API/LLM 请求受剩余预算限制。日志与截图保存在 `output/duoplus_workflows/<任务ID>/`。
- 控制台增加检查、JSON 导入、英文主题、运行预算、AI 步数、自动关机和停止入口。页面明确提示不能与官方任务同时运行。
- 固定坐标按 1080×1920 转成 HTTP 相对坐标；运行前只读检查物理/覆盖分辨率，尺寸不符会停止。滑动时长与原 RPA 的精确等价性仍未确认，预览中明确提示；本版本保留方向和随机距离，不声称完全等价。
- 运行状态暂保存在 Web 进程内，完整结果落盘。重启服务会中断任务，不能保证进程退出后执行自动关机；页面提示避免通过重启停止任务。
- 验证计划：运行 `test_duoplus.py` 和新增 `test_duoplus_workflow.py`（全部云 API 和模型调用使用 Mock）；检查页面 JS 语法。测试源文件经 GitHub 同步到 4003 后在现有容器内执行，不重启 Web，不调用设备。
- 4003 首次离线测试中，原有 7 项通过；新增 16 项中 2 项因测试夹具缺少模拟 phone 对象失败，补全夹具后 16 项全部通过（约 0.105 秒）。页面脚本的 `node --check` 通过。
- 后续修正了导入文件失败后不能偷偷回退内置模板的问题，以及状态轮询中断后保留任务 ID/停止入口的行为。再次点击执行按钮会恢复读取已有任务，不重复创建云手机任务。
- 当前仅将源代码通过 GitHub 同步到 4003，尚未由本任务重启 Web。新后端接口是否被运行进程加载及页面交互验收尚未验证；待官方任务结束后再启用并实测。
- 最终复验：`docker-compose -p short-video-analyzer-dev exec -T web python scripts/test_duoplus_workflow.py` 的 17 项全部通过（约 0.076 秒）；加上原有 7 项，共 24 项 Python 离线测试通过。相关 Python 源文件 AST 语法检查通过。
- `docker-compose -p short-video-analyzer-dev exec -T web node scripts/test_duoplus_workflow_ui.js` 的 4 个场景全部通过：正常完成及预算/主题传递、轮询失败后取消、恢复轮询不重复建任务、非法 JSON 禁止回退。使用伪 DOM 和完全替代的 fetch，没有浏览器、模型或云手机连接；此测试不代替真实页面视觉验收。
- 代码提交：`154df62`（执行器及控制台）、`53e1478`（导入保护/测试夹具）、`725a732`（轮询取消及参数校验）、`4c32fad`（前端离线场景）。已通过 GitHub 同步到 4003；服务激活和设备验收保持待办。
- 下一次实测准备：确认官方任务已结束和设备空闲，重启 4003 Web 以加载新接口；检查工作流预览，主题填 `AI translator`，选择“仅搜索单个主题”，预算 300 秒、16 个 AI 动作、结束后关机。核验搜索词、Top 页和打开的视频，更新日志及截图证据；不直接运行完整养号流程。

## 前端精简记录

- 按用户要求，将刷新、返回、桌面、上滑、下滑、打开应用和文字输入入口改为手机画面旁的 7 个 SVG 图标按钮；每个按钮保留中文无障碍名称及悬停提示。
- 默认工作流区域只保留英文主题、运行范围、启动/停止和结束后关机；时间/动作限额、模板导入与调试数据收进折叠项。移除重复的手写动作数组编辑器，基础动作 API 保留。
- 原始执行日志改为简短中文进度，完整数据仍可在调试信息和服务端 output 中查看。完整流程的互动范围仍明确显示，未更改执行器行为。
- 该阶段不查询或操作云手机，不重启 Web；通过隔离的离线响应验证页面。已完成桌面 1280×720、手机 390×844 和窄屏 320×740 的布局检查，均无横向溢出，7 个图标按钮均位于手机画面旁且不超出可视宽度。
- 视觉检查修正了全局 input 宽度导致的复选框拉伸、手机标签换行和顶部冗余留白；状态并入设备栏，连接配置页面保留可见的成功/失败反馈。主动停止任务显示为普通状态；关机未确认成功时仍明确提醒。
- 浏览器交互验证：刷新离线示例画面、展开/收起文字输入、发送模拟文本、切换运行范围、启动/停止模拟任务。停止后按钮恢复且未采用错误样式。页面标题/入口正确，非空，无错误覆盖层，控制台 error/warn 为空；4 个 Node 前端离线场景再次通过。
- 测试环境以 4003 当前模板与统一导航生成页面，全部 DuoPlus API 响应由临时 Node 服务模拟；不代理任何业务 API。初次直连 192.168.1.254:4993 的 HTTP 页面导航超时（浏览器 Page.navigate），服务端 loopback 检查返回 200；改走 SSH 隧道 http://127.0.0.1:4994/duoplus 后验证成功。使用 Codex 内置浏览器，不绕过安全警告。
- 截图证据保存在 C:/Users/admin/.codex/visualizations/2026/10/14/01a13852-6cec-7df3-be2e-183c6dca43ce/duoplus-desktop.png 与 duoplus-mobile.png，均为离线预览。临时服务及隧道在验证后清理，浏览器尺寸覆盖恢复默认。
- 前端代码提交包括 `f64c5c5`、`cae96ec`、`974b816`、`45306c6`，均已通过 GitHub 同步到 4003。实际设备响应及完整工作流验收仍按用户先前指示暂缓。

## 官方参考

- https://help.duoplus.cn/docs/cloud-phone-list
- https://help.duoplus.cn/docs/AI-kong-zhi-yun-ji-API
- https://help.duoplus.cn/docs/param-desc
- https://help.duoplus.cn/docs/power-on-off
- https://help.duoplus.cn/docs/batch-power-on
- https://help.duoplus.cn/docs/subscription-startup-list
- https://help.duoplus.cn/docs/execute-the-adb-command
