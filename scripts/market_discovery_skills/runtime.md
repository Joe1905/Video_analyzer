# 4003 隔离后端运行约定

此 API 供外部模型（例如本地 Luna）和后续新前端使用。它不启动模型、不创建定时任务、不自动推送消息，也不加入现有聊天或首页。研究判断依据两个 Skill；这里仅说明真实可用的工具与持久化操作。

基础路径 `/api/market-discovery/`。同一项长期研究使用稳定的 `workspace`（字母、数字、下划线、连字符）；默认 `default`。测试使用独立 workspace，不混入运营数据。请求和响应为 UTF-8 JSON；POST body 的 workspace 与 GET query 的 workspace 含义相同。

当前 4003 测试阶段禁用跨研究复用。每次独立测试开始前，由测试执行方确认没有仍在写入的研究，检查专用研究库仅含测试数据，清理上轮测试的 categories、category_versions、documents、calls，并核验四表均为空；发现非测试数据时停止清理，不扩大删除范围。每轮使用新的 workspace，模型只访问本轮记录，不加载历史日报或本地旧测试产物。单轮内部仍正常保存、检索和更新，以测试续接与状态一致性；完成后保留本轮结果供验收，下一轮开始前再清理。API 数据缓存与研究记忆不同，缓存命中须另行报告，不能当成新扣费。此约定不授权模型自行删除数据库，也不改变未来正式运营的长期档案设计。

1. GET `manifest` 取得两个 Skill 和引用文件。GET `tools` 取得当前允许调用的完整 inputSchema；SellerSprite schema 实时从已有桥获取，SociaVault schema 基于官方 MCP 2.0.0。接口未暴露的工具不能编造。
2. POST `tools/call`，body 为 `{workspace,name,arguments}`。返回 `call_id` 和原始 `result`，之后可 GET `calls/<call_id>?workspace=...` 读取。HTTP 200 不代表上游成功，检查 isError、success 和供应商业务错误。不要把请求、采集时间和来源内容时间混为一谈。
3. 新市场先调用真实趋势发现接口。SellerSprite `google_trend` 用于给定关键词的 Google 趋势验证；ABA 周/月研究可发现真实热门、异动和增长词。根据实时 schema 选参数，记录具体种子及来源，不预设品类。SociaVault TikTok trending/热门标签也可提供线索。
4. GET `categories?workspace=...&q=名称或别名` 模糊检索；逗号可分隔多个别名。match_score 只是字符串检索相关性，不是可行性分数，也不自动合并。GET `categories/<id>` 读取完整记录。
5. 选定类目后 POST `categories`，body 为 `{workspace,record:{name,aliases,region,audience,scene,need,...}}`。其余研究字段可自由补充，例如趋势 call_id、来源词、状态、判断和文档 key。返回稳定 id 与 revision。更新携带 `id`、当前 `expected_revision` 和完整 record；409 表示有新版本，先重新读取再合并，不盲目覆盖。
6. POST `documents`，body 为 `{workspace,key,content,expected_revision}`。key 是相对 `.md` 或 `.json` 路径；例如 `candidates/2026-09-22.md`、`categories/<id>/research.md`、`categories/<id>/evidence.md`、`categories/<id>/feasibility.md`、`reports/2026-09-22.md`。新文档 expected_revision 为 0；已有文档先读再用当前版本更新。内容落入独立 SQLite 并保留历史版本，不是仅存在于聊天。
7. GET `documents?workspace=...` 列索引；加 `key=...` 读取内容，另加 `revision=N` 读取历史。加 `format=markdown` 导出 Markdown 原文，供后续前端或已授权推送消费。日报链接应指向真实文档 key 或 API URL，不能声称位于不存在的本地目录。

独立数据文件为 `data/market_discovery/research.sqlite`，保存类目、历史版本、Markdown、候选记录和工具调用原始结果。证据使用 `<workspace>/<category-id>/<证据ID>` 或文档 key 与 call_id 保持引用稳定。每期日报留存当时判断；复核时更新类目当前记录并保留变更原因。模型自主选择取证路线；API 不替模型评分、选品或判定哪些市场必定成立。

SociaVault 固定研究目录现覆盖 TikTok/Shop、YouTube、X、Facebook、Instagram、Threads、Pinterest、Reddit 和 Google 搜索；具体能力以 tools 为准。Facebook/Instagram 需先定位公开账号、群组或帖子 URL，不能编造通用帖子搜索。Meta 广告工具只用于广告与供给证据。重复阅读使用 calls/{call_id}，不重新执行付费 tools/call。

