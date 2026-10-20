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

Buffer 的价值是现成账号接入和多平台排期；仅TikTok且有可用的官方权限时，可以直接替代Buffer。TikTok普通Content Posting API的公开发布审核要求，不能直接套用为私人内部发布工具已获准。

TikTok Shop美国站官方说明支持给近30天已发布视频通过Creator Center补挂商品；该说明属于界面功能，未验证补挂对应的开放API。因此没有实现“Buffer发布后自动补挂商品”。

参考：
- https://developers.buffer.com/examples/create-video-post.html
- https://developers.buffer.com/guides/posts-and-scheduling.html
- https://developers.buffer.com/types/TikTokPostMetadataInput.html
- https://developers.tiktok.com/docs/en/content-sharing-guidelines
- https://seller-us.tiktok.com/university/essay?knowledge_id=5804034343962411
