# Buffer 发布接入（4003）

更新：2026-10-20。仅开发环境，保留现有 TikTok Studio 发布器。

## 实施范围

- [ ] 发布页：视频、文案、TikTok 频道、AI 内容声明、立即 / 定时 / Buffer 草稿。
- [ ] 本地持久化任务和步骤日志，区分已提交、已排期、发布成功、失败、结果待确认。
- [ ] 定时使用虚拟机 America/Los_Angeles 时区，提交 Buffer 时转换 UTC；拒绝夏令时不存在 / 有歧义的时间。
- [ ] Ubuntu 独立媒体目录，通过 ruitech.hk HTTPS 提供可持续读取的视频地址，上传需专用令牌。
- [ ] 避免重复发布：请求去重、提交前落盘、超时及重启中断不自动重发。
- [ ] 4003 Docker 检查、远程媒体 Range 验证、浏览器交互检查。

## 边界

用户选择稍后配置 Buffer Key / 频道。本次不发布任何用户视频；用生成的测试视频验证存储。真实 Buffer 草稿、定时和发布需配置后实测。

普通 TikTok 内容走 Buffer；挂车需另走 TikTok Shop Creator API，尚未接入。不假定能给 Buffer 发布的视频补挂商品。Buffer 的 TikTok 元数据没有商品绑定字段。只有 TikTok 且具备所需官方权限时，可以省去 Buffer，保留本系统的任务和日志。

媒体不设置短期过期时间，避免远期定时失效。本次不自动删除引用中的媒体，也不提供任意 URL 抓取。Key 和媒体令牌仅保存于 gitignored data/，不回传浏览器。

## 部署

Windows developer → GitHub → 4003 /home/openclaw/Video_analyzer-dev；远程 Ubuntu 从同一 GitHub 版本获取媒体服务及安装脚本。禁止传输源文件直改远端。

## 验证记录

待实施。
