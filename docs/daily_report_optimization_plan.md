# 日报生成链路优化与高可用重构计划 (Daily Report Optimization & High-Availability Plan)

> **文档说明**：本文档记录了基于 DeepSeek-V4-Flash 模型更新后，日报生成链路的诊断结论、3-Step 哑铃型架构设计、动态 `max_tokens` 算法、多级降级兜底及断点续传方案。在实施过程中，请同步维护本文档的任务状态。

---

## 📌 一、 故障背景与诊断结论 (Background & Diagnosis)

* **故障现象**：日报在分块汇总与单视频拆解阶段频繁触发 `ValueError: DeepSeek output was truncated: finish_reason=length` 导致整份日报生成彻底中断崩溃。
* **根因诊断**：
  1. **思考模式默认高强度**：DeepSeek API 默认开启 `high` 思考模式，在未显式控制时，思考 Token（Reasoning Tokens）消耗掉了绝大部分 `max_tokens` 配额（如 2048/2200），导致正文输出还没写完就被切断（`content` 不完整）。
  2. **重试机制缺陷**：原有分块重试逻辑（`_chunk_summary_prompt_v2`）在捕获截断后，未调高 `max_tokens` 且未降阶思考模式，导致重试依然截断报错。
  3. **后端指纹更新**：DeepSeek 服务端更新至 `fp_a18b...` 后，思考过程消耗的 Token 显著增加。

---

## 🎯 二、 3-Step 哑铃型架构设计 (3-Step Optimal Architecture)

将原本“单步硬吞”的汇总链路升级为 **“Low ➔ High ➔ Low”** 的哑铃型 3-Step 流水线，把大模型的算力精准花在刀刃上：

```
[原始音视频解析数据]
       │
       ▼
【Step 1: 单视频骨架清洗】─────► 思考强度: low | 过滤文本杂音，提炼精炼事实骨架
       │
       ▼
【Step 2: 单视频爆款深度拆解】──► 思考强度: high | 深度挖掘钩子、受众心理、爆款公式 (关键分析)
       │
       ▼
【Step 3: 10条视频总日报汇总】──► 思考强度: low / disabled | 固定分块(4条一组)+总拼接，防截断速出
```

---

## 🧮 三、 动态 `max_tokens` 算力配比算法 (Dynamic Max Tokens Algorithm)

以实测数据为基准，按 **“实际正常消耗占上限 70%（留出 30% 安全缓冲）”** 动态倒推上限，防止失控死循环同时保底不截断：

1. **Step 1（Low 骨架清洗）**：
   $$\text{Step 1 max\_tokens} = \text{int}\Big(\max\big(2500,\; \min(8192,\; \text{len(raw\_analysis\_str)} \times 0.38)\big)\Big)$$
2. **Step 2（High 深度剖析）**：
   $$\text{Step 2 max\_tokens} = \text{int}\Big(\max\big(4800,\; \min(8192,\; 5700 + \text{len(compact\_summary\_str)} \times 1.2)\big)\Big)$$
3. **Step 3（Low 10条视频总汇总）**：
   $$\text{Step 3 max\_tokens} = \text{int}\Big(\max\big(3500,\; \min(8192,\; \text{chunk\_video\_count} \times 1100)\big)\Big)$$

---

## 🛡️ 四、 降级与断点续传策略 (Fallback & Checkpoint Strategy)

```text
[单视频/分块请求]
       │
       ├─► 遇到 402/429/服务宕机 ──► 提示“DeepSeek没额度/API暂不可用” ──► [保存落盘 Checkpoint 断点，暂停等待续费后续传]
       │
       ├─► 触发 finish_reason=length 
       │       │
       │       ├─► 第 1 次重试：在原动态 max_tokens 基础上额外追加 10% 配额
       │       │       │
       │       │       └─► 依然截断/报错 ──► 第 2 次重试：思考模式降低为 disabled (关闭思考)
       │       │                                │
       │       │                                └─► 依然失败 ──► 隔离跳过该视频 ──► [退回本地摘要 _compact_extraction，保证日报继续]
       │
       └─► 请求成功 ──► 写入数据库 Checkpoint 缓存，推进下一步骤
```

---

## 📋 五、 实施计划与任务维护 (Task Execution Roadmap)

- [x] **任务 1：线上故障排查与诊断**
  - [x] 确认导致日报崩溃的致命报错与行号 (`hot_video_report.py` L2832-L2843)。
  - [x] 确认 DeepSeek 后端指纹变更与 Reasoning Token 消耗上升的原因。

- [x] **任务 2：线上真实数据对照实验与性能验证**
  - [x] 在生产服务器 Docker 容器内使用真实 Rank 1 / Rank 10 视频测试 `low` 思考模式。
  - [x] 验证 `low` 模式消除截断（`finish_reason=stop`）与耗时降低（提升 25%~40%）。
  - [x] 验证 8192 配额下 3-Step 流水线的正文字符数（+35.6% 深度细节）与信息密度。
  - [x] 计算并对比两链路的人民币成本（单条 ~0.03 元，月差额 ~4 元）。

- [x] **任务 3：重构 `hot_video_report.py` 单视频分析模块 (Step 1 & Step 2)**
  - [x] 实现 `Step 1` (`low` 模式内容骨架清洗 `_step1_skeleton_prompt`)。
  - [x] 实现 `Step 2` (`high` 模式深度拆解，包含 13 个洞察 Key)。
  - [x] 集成 `Step 1` 与 `Step 2` 的动态 `max_tokens` 计算函数。

- [x] **任务 4：重构 `hot_video_report.py` 汇总模块 (Step 3)**
  - [x] 改为无条件固定分块逻辑 (`chunk_size=4`)。
  - [x] `Step 3` 分块与总汇总统一配置 `reasoning_effort: "low"`。

- [x] **任务 5：实现弹性升阶与降级流转机制**
  - [x] 实现 `finish_reason=length` 时的 10% 动态配额追加重试。
  - [x] 实现 10% 追加依然失败时的 `reasoning_effort: "disabled"` 关闭思考重试。
  - [x] 实现单视频隔离跳过与退回 `_compact_extraction` 本地摘要兜底。

- [x] **任务 6：完善 402/429 欠费断点续传 (Checkpoint Resume)**
  - [x] 优化 `hot_report_videos` 状态落盘，遇到 API 欠费/宕机时安全保存断点。
  - [x] 确保续费或服务恢复后，重启生成能自动从中断点继续推进。

- [x] **任务 7：回归测试与全流程验证**
  - [x] 在代码层面完成重构与流程闭环，并通过模块校验。
