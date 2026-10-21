# Buffer 发布接入（4003）

更新：2026-10-20。仅开发环境，保留现有 TikTok Studio 发布器。

## 实施范围

- [x] 发布页：视频、文案、TikTok 频道、AI 内容声明、立即 / 定时 / Buffer 草稿。
- [x] 本地持久化任务和步骤日志，区分已提交、已排期、发布成功、失败、结果待确认。
- [x] 定时使用虚拟机 America/Los_Angeles 时区，提交 Buffer 时转换 UTC；拒绝夏令时不存在 / 有歧义的时间。
- [x] Ubuntu 独立媒体目录，通过 ruitech.hk HTTPS 提供可持续读取的视频地址，上传需专用令牌。
- [x] 避免重复发布：请求去重、提交前落盘、超时及重启中断不自动重发。
- [x] 4003 Docker 检查、远程媒体 Range 验证、浏览器交互检查。

## 边界

用户选择稍后配置 Buffer Key / 频道。本次不发布任何用户视频；用生成的测试视频验证存储。真实 Buffer 草稿、定时和发布需配置后实测。

普通 TikTok 内容走 Buffer；挂车需另走 TikTok Shop Creator API，尚未接入。不假定能给 Buffer 发布的视频补挂商品。Buffer 的 TikTok 元数据没有商品绑定字段。只有 TikTok 且具备所需官方权限时，可以省去 Buffer，保留本系统的任务和日志。

媒体不设置短期过期时间，避免远期定时失效。本次不自动删除引用中的媒体，也不提供任意 URL 抓取。Key 和媒体令牌仅保存于 gitignored data/，不回传浏览器。

## 部署

Windows developer → GitHub → 4003 /home/openclaw/Video_analyzer-dev；远程 Ubuntu 从同一 GitHub 版本获取媒体服务及安装脚本。禁止传输源文件直改远端。

## 验证记录

- 首轮提交 ff1d46d：4003 Docker `python scripts/test_buffer_publish.py`，20 项通过。覆盖 LA 冬夏时转换、DST 歧义、请求幂等、超时不重发、重启中断、已发不能取消、API 预算、Origin 限制、媒体鉴权和 Range。
- Python 编译检查使用 `PYTHONPYCACHEPREFIX=/tmp/buffer-pycache`（scripts 挂载只读，不能在源目录生成 pyc）。JS `node --check`、`git diff --check` 通过。
- 4003 `/publish`、状态、任务列表、资源、healthz 返回 200；旧 `/duoplus` 仍 404。跨站配置写入 403。
- 生成 3.5 秒 360×640 MP4，从4003上传、ffprobe校验、传至Ubuntu、SHA256核验通过。公网独立请求 HEAD 200、MIME video/mp4、Accept-Ranges bytes；GET 0–31 为206；未鉴权PUT为401。原公司首页仍200。
- Ubuntu systemd 服务 active，运行用户 buffer-media，媒体目录 `/var/lib/video-analyzer-media`；令牌权限600。仅绑定Caddy所在Docker桥接网关，未增加公网监听端口。
- Buffer API 从4003直连TLS成功，未提供凭证请求返回401（预期）。没有创建任何Buffer任务。
- 桌面浏览器：页面、草稿/定时切换、连接弹窗的存储状态通过；控制台 error/warn 为空。未验证手机视口。
- 当前开发分支缺少 AGENTS.md 提到的 `scripts/test_ui_contract.py`，没有补造该检查；采用以上专用测试与浏览器检查。
- 需用户稍后配置：Buffer个人Key、已授权TikTok频道。真实草稿→定时→sent链路仍待验证。视频默认长期保留，服务器带宽套餐/流量配额尚未核实。
- 后续检查增加第21项：后台扫描和重启恢复不受前端最近100条记录的限制。

## 官方接口判断

### 2026-10-21 时间标注修正

发布输入明确标注“发布时间（洛杉矶时间）”，所选日期旁显示当日 UTC 偏移，下面实时换算北京时间。任务列表、日志使用中文时区及当时的UTC偏移；当前服务器时间持续更新，页面重新可见时重新同步。预览复用后端的时区转换，保持夏令时无效时间和重复时间的校验；发布本身仍要求未来至少两分钟。

### 2026-10-21 待发布任务编辑

官方 `editPost(input: EditPostInput!)` 支持原任务修改文案和定时时间。初版按文档省略assets/metadata，后续真实改期发现接口合并前的校验会报缺媒体，见下面修正。4003增加编辑弹窗，修改时间时显示洛杉矶/北京时间对照。保存前查询真实状态与allowedActions，以updatedAt检测其他修改；sending/sent不允许编辑。本地未提交任务直接修改持久化记录；远程任务只调用editPost，不取消重建。提交中断/超时不自动重试，保留待确认修改，查询回执匹配后再恢复状态。初版验证不改动用户既有任务的实际文案或排期。

参考：https://developers.buffer.com/types/EditPostInput.html

验证：4003 Docker 30项测试通过，包含本地编辑、文案/时间最小更新、版本冲突、已发布/无权限拦截、失败保留原排期、超时读取匹配、无改动不调用mutation。真实API确认现有任务具备文案和改期权限；浏览器验证编辑弹窗、时间对照、无改动保存通过，控制台无error/warn。复核原Post ID、文案、排期一致，未对用户任务触发真实editPost；实际有变化的远程编辑通过模拟接口测试覆盖，待用户保存修改后验证。

### 2026-10-21 实际改期校验修正

用户实际改为洛杉矶2026-10-21 08:15时报 `Invalid post: Post must have either text or media., TikTok posts require at least one image or video.`。只读核对确认原Post仍有一个video/mp4，source与原Ubuntu地址一致，AI声明为true。改为每次从Buffer读取原帖视频、封面帧/标题、AI声明，和完整文案一起提交editPost；只改期也携带原文案/媒体。视频缺失时停止修改，回执或待确认重读必须同时匹配视频和AI声明。新增模拟实际接口“先校验再合并”行为的回归用例，Docker共33项通过。

用户确认最终按输入值洛杉矶2026-10-21 08:15／北京2026-10-21 23:15执行。浏览器实际保存成功，独立读取Buffer确认dueAt为2026-10-21T15:15:00.000Z、status=scheduled、原Post ID不变、原视频source不变、文案和AI声明保留，总任务数仍为1。没有取消重建，也未立即发布。

另修正编辑时间换算残留：输入变化时立即清除旧对照，显示“换算中”，当前换算成功前禁止保存，避免输入08:15但仍显示上一时间的北京21:15。新建发布的输入也立即清除旧对照。

Buffer 的价值是现成账号接入和多平台排期；仅TikTok且有可用的官方权限时，可以直接替代Buffer。TikTok普通Content Posting API的公开发布审核要求，不能直接套用为私人内部发布工具已获准。

TikTok Shop美国站官方说明支持给近30天已发布视频通过Creator Center补挂商品；该说明属于界面功能，未验证补挂对应的开放API。因此没有实现“Buffer发布后自动补挂商品”。

参考：
- https://developers.buffer.com/examples/create-video-post.html
- https://developers.buffer.com/guides/posts-and-scheduling.html
- https://developers.buffer.com/types/TikTokPostMetadataInput.html
- https://developers.tiktok.com/docs/en/content-sharing-guidelines
- https://seller-us.tiktok.com/university/essay?knowledge_id=5804034343962411
