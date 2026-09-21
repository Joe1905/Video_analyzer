# 4002 发布问题记录与第二轮日志审计

采集时间：2026-09-20 18:22:23（北京时间）。范围：任务 updated_at >= 2026-09-20 07:11 UTC（北京时间15:11），包含软删除和重试任务。
服务器当前提交：f3d7af8e，包含发布修复34c6de9b；新增提交是网盘保留15天功能。本轮没有修改生产代码、配置、任务状态，没有重新发布或重启。
本地诊断快照：tmp/publish-audit-20260920-round2.json。含任务快照、会话记录、诊断文件索引、error.json 内容及容器异常摘要；图片只索引，未逐张下载核验。容器日志限最近2500行，不是完整历史日志归档。

## 已确认问题

### P1：排队发布未复用已打开的空闲观测窗口

- 账号：hihiokivivi，account_id=15。
- 任务cab7da237b104b8ea34a5e6a1dc92881、480836051a2f40d9a91a9a7c22fcc3b4均记录“账号已经处于唤醒状态”，session_id为空，未记录最终发布点击。
- 两条任务当前为cancelled且deleted_at非空。只能确认发生过会话冲突，不能把最终取消直接归因于系统自动取消。
- 前端submitPublish仅在immediate/manual操作时传observation_session_id；queue不传。
- 后端claim_observation_session_for_job在session_id为空时直接返回None，随后尝试start_automation_session；已有活动会话时被start_login_session拒绝。
- 建议修复：排队任务能够认领同账号的空闲会话；会话忙时等待；认领必须避免并发任务同时占用。任务完成后保留原人工窗口的所有权语义。
- 验收：空闲观测窗口中排队发布不重复创建浏览器；忙碌窗口不被抢占；关闭/换窗口后不存在使用过期session_id的问题。

### P2：已唤醒的观测会话显示为“唤醒中”

- 截图对应会话843，status=observing、owner=manual、current_job_id为空。
- accountRuntimeStatus把无任务的活动会话统一显示为“唤醒中”，导致用户以为启动或发布尚未完成。
- 建议区分starting“启动中”和observing“已唤醒/观测中”，有任务时显示实际发布或采集状态，并同步筛选项。
- 验收：状态由真实会话和任务驱动，终止任务后不能残留发布中或启动中。

## 本轮新异常线索

### P1：浏览器弹窗处理协议异常，与页面关闭错误时间相邻

- 容器2026-09-20 09:55:33 UTC（北京时间17:55:33）出现未处理Promise异常：ProtocolError: Protocol error (Page.handleJavaScriptDialog): No dialog is showing。
- 调用栈：Dialog._onHandle → Dialog._accept → Dialog._close → DialogManager.dialogDidOpen。
- 同时间段任务01e63cd2e0874580a4d2aad7ac37e43b（hihiokivivi）失败；attempt-1-ea55651f/error.json保留两层TargetClosedError，外层为Page.screenshot，内层同样是页面/上下文/浏览器已关闭。
- 其会话837在北京时间17:57:03记有“用户终止发布任务并休眠账号”。因此无法仅凭这些日志判断是协议异常造成关闭，还是人工终止导致任务失败；截图异常仍占据顶层错误，诊断呈现可改进。
- 下一步：核查多个CDP连接是否重复处理同一个对话框、对话框处理是否存在竞态，关联driver退出与用户停止的精确顺序。不要将其归为“实际发布成功的误判”。

### P2：丢弃旧编辑内容后未等到上传控件

- 任务7a849174f85e4995afa4429fdfdf6cdc（hihiokivivi），累计attempt_count=3。
- attempt-2-79aded0c/error.json：“未找到 TikTok Studio 视频选择控件”。同时存在stale-edit-before-discard.png、stale-edit-discarded.png和last-state.png。
- 时间：北京时间18:18:19；第三次尝试18:19:35被系统记录为scheduled_on_tiktok，final_click_at为18:19:33。
- 证据指向“丢弃旧草稿后的页面切换/就绪检查”需要核查，但尚未查看截图，不能确认是等待不足、跳错页面还是控件改变；后续成功不能证明前一次是误判。
- 下一步：对照截图和页面状态，在选择文件前确认上传入口真正就绪。

## 任务统计与口径

- 按更新时间筛选共13条：published 4、scheduled_on_tiktok 4、failed 2、result_uncertain 1、cancelled 2。
- 其中b0714c9edbde4d3a835415afdf1f831c与c0dcfd1d17df420cbd2cc693dd92bc37是旧任务本轮被删除，不能作为修复后的新失败。旧错误分别为音乐/时间线面板遮挡Add按钮，以及商品绑定未确认。
- c2a1f16b786f4391b69da04e7bd948a8在上一轮采集边界前创建，本轮结束；同样不能单凭updated_at判断使用了新版代码。
- 本轮新创建10条：4 published、3 scheduled_on_tiktok、1 failed、2 cancelled（带会话冲突错误且已软删除）。
- 同账号最新7a849174任务被系统记录为已排程，说明不是整个账号持续无法发布；平台最终状态未独立核验。
- 不计算“误判率”：缺少各次尝试的完整平台结果和截图复核。本轮未新增“已成功发布却报失败”的确证。

## 后续优先级

1. 修复空闲会话复用和状态标签，优先解决已明确复现的用户阻塞。
2. 核查弹窗处理竞态与driver异常，保留原始错误及人工终止时间线。
3. 补齐丢弃旧编辑内容后的页面就绪等待，并用回放验证。
4. 只在确认修复后另行验证，不自动重试已取消、已排程或结果不明确的历史任务。
