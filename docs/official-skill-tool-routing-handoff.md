# 4004 官方依据工具暴露：后续实现交接

更新时间：2026-07-31  
目标分支：`codex/ui-beautification-4004`  
目标环境：仅 4004；不得修改或部署 4002、4003  
当前基线提交：`ef19d3f`（“智能选品助手”单预设工具硬边界）

## 1. 一句话原则

**官方材料决定“这个预设需要哪些工具”，4004 自己的代码只负责识别预设、加载对应官方内容、实施工具白名单、验证执行边界和记录诊断。**

必须始终区分两件事：

| 层次 | 归属 | 说明 |
|---|---|---|
| 工具选择依据 | 官方 | SellerSprite 官方 Skill，或 SociaVault 官方 MCP 工具目录与文档 |
| 预设识别、白名单、执行拦截、降级和日志 | 4004 自建 | 这是项目自己的实现规则，不得描述成官方权限系统 |

禁止根据 UI 卡片文案、工具英文名、模型猜测或旧版意图分类器自行决定预设工具。

## 2. 本次交接目标

以后续已经完成的 SellerSprite“智能选品助手”为蓝本，完善：

1. SellerSprite 剩余 26 个官方预设；
2. SellerSprite 前端预设 ID 到后端工具白名单的稳定传递；
3. SociaVault 在官方 MCP 工具目录基础上的平台/能力路由；
4. 所有站点统一的工具越界测试、运行时目录校验和答案事实边界验收。

本交接不授权：

- 修改或部署 4002、4003；
- 恢复用户工具选择；
- 改变现有 MCP 协议、缓存键或 TTL；
- 增加 REST 回退；
- 混用 `sellersprite__*`、`sociavault__*`；
- 把项目自建分类宣称为官方规则；
- 因为某个预设暂未完成就把错误工具临时塞进去。

## 3. 已完成蓝本：智能选品助手

### 3.1 官方依据

固定版本 SellerSprite CLI Skills：

- 版本：`0.1.17`
- 提交：`afea6ad232b3bcae38704b1e5a5953f82492bdf1`
- SHA-256：见 `scripts/sellersprite_official_skill.py`
- 当前预设文件：`comprehensive/product-research.md`

该官方文件在执行步骤和参考文档中明确使用：

- `product_node`
- `product_research`
- `asin_detail`
- `asin_prediction`
- `market_research_statistics`
- `google_trend`（官方标记为可选，但属于允许工具）

### 3.2 4004 自建规则

当前后端在 `scripts/web_app.py` 中实现：

- `SELLERSPRITE_PRODUCT_RESEARCH_PRESET_ID`
- `SELLERSPRITE_PRODUCT_RESEARCH_SKILL_FILE`
- `SELLERSPRITE_PRODUCT_RESEARCH_TOOL_IDS`
- `sellersprite_official_skill_route(...)`

当前 Skill 加载与隔离在 `scripts/sellersprite_official_skill.py` 中实现：

- `load_official_sellersprite_skill_prompt(...)` 加载固定版本完整包；
- `select_official_sellersprite_skill_prompt(...)` 只返回来源头和指定的一份官方 Skill；
- 未知或不在固定文件清单内的文件会被拒绝。

当前请求与执行链路：

1. 用户选择“智能选品助手”；
2. 前端写入受控提示前缀；
3. `/api/chat/ask` 接受 Amazon 请求及可选 `officialPresetId`；
4. 后端识别 `comprehensive/product-research`；
5. 只加载 `comprehensive/product-research.md`；
6. 模型只看到上述 6 个 `sellersprite__*` 工具；
7. 实际执行仍通过允许工具 ID 集合做第二次校验。

### 3.3 已验证结果

线上最近一次“解压玩具”会话：

- SellerSprite 运行时目录：43 个工具；
- 实际暴露：6 个工具；
- 实际调用：14 次，工具名称全部属于这 6 个；
- 无其他站点或本地功能工具越界；
- 无未知工具执行；
- 官方完整提示约 69,481 字符，单预设提示约 2,414 字符；
- 路由日志：`effective=43 tools=6 official_preset=comprehensive/product-research`。

这个结果是其他预设的最低边界标准。

## 4. 官方依据的优先级

为每个站点建立清单时，严格按照以下顺序判断：

1. **固定版本官方 Skill 的执行步骤**；
2. **同一 Skill 的参考文档链接或明确列出的工具名**；
3. **运行时 MCP `tools/list` 返回的真实工具名称和 schema**；
4. 4004 已有语义注册表，仅用于渲染、诊断和完整性检查；
5. UI 名称和简介只用于展示，不是工具选择依据。

如果执行步骤和参考文档不一致：

- 取两者明确提到工具的并集作为“允许工具候选”；
- 将官方明确标记“可选”的工具保留在允许集合中；
- 不得自行补入“看起来可能有用”的工具；
- 在清单中记录歧义，并添加对应测试；
- 无法确认时暂停该预设，不要伪造官方依据。

禁止让 LLM 在运行时阅读全部工具描述后自行生成白名单。白名单必须是固定版本、可审查、可测试的静态清单。

## 5. SellerSprite 预设清单

固定版本官方文件位于 `scripts/sellersprite_official_skill.py` 的
`OFFICIAL_SELLERSPRITE_PROMPT_FILES`。

### 5.1 综合分析（10）

| 预设 ID | UI 名称 | 官方文件 | 状态 |
|---|---|---|---|
| `comprehensive/product-research` | 智能选品助手 | `comprehensive/product-research.md` | 已完成 |
| `comprehensive/market-analysis` | 市场全景分析 | `comprehensive/market-analysis.md` | 待完成 |
| `comprehensive/competitor-analysis` | 竞品深度拆解 | `comprehensive/competitor-analysis.md` | 待完成 |
| `comprehensive/keyword-research` | 关键词选品研究 | `comprehensive/keyword-research.md` | 待完成 |
| `comprehensive/listing-optimizer` | Listing 优化诊断 | `comprehensive/listing-optimizer.md` | 待完成 |
| `comprehensive/traffic-analysis` | 流量结构分析 | `comprehensive/traffic-analysis.md` | 待完成 |
| `comprehensive/opportunity-finder` | 蓝海机会挖掘 | `comprehensive/opportunity-finder.md` | 待完成 |
| `comprehensive/review-insights` | 买家评论洞察 | `comprehensive/review-insights.md` | 待完成 |
| `comprehensive/pricing-strategy` | 定价策略分析 | `comprehensive/pricing-strategy.md` | 待完成 |
| `comprehensive/ad-optimizer` | 广告投放优化 | `comprehensive/ad-optimizer.md` | 待完成 |

### 5.2 战术选品（17）

| 预设 ID | UI 名称 | 官方文件 | 状态 |
|---|---|---|---|
| `tactical/new-product-burst` | 新品快速爆发 | `tactical/new-product-burst.md` | 待完成 |
| `tactical/hidden-bestseller` | 隐形爆款 | `tactical/hidden-bestseller.md` | 待完成 |
| `tactical/aba-high-growth-trend` | ABA 高增长趋势词 | `tactical/aba-high-growth-trend.md` | 待完成 |
| `tactical/low-monopoly-keyword` | 流量分散关键词 | `tactical/low-monopoly-keyword.md` | 待完成 |
| `tactical/title-density-gap` | 标题密度漏洞 | `tactical/title-density-gap.md` | 待完成 |
| `tactical/hot-low-rating` | 热销低评分产品 | `tactical/hot-low-rating.md` | 待完成 |
| `tactical/review-sentiment` | 评论语义分析 | `tactical/review-sentiment.md` | 待完成 |
| `tactical/low-brand-monopoly` | 低品牌垄断类目 | `tactical/low-brand-monopoly.md` | 待完成 |
| `tactical/high-new-product-ratio` | 高新品占比市场 | `tactical/high-new-product-ratio.md` | 待完成 |
| `tactical/high-margin-lightweight` | 高毛利轻小品 | `tactical/high-margin-lightweight.md` | 待完成 |
| `tactical/natural-traffic-audit` | 自然流量反查 | `tactical/natural-traffic-audit.md` | 待完成 |
| `tactical/variant-gap-analysis` | 变体拆解模型 | `tactical/variant-gap-analysis.md` | 待完成 |
| `tactical/local-premium-disruption` | 本土溢价降维 | `tactical/local-premium-disruption.md` | 待完成 |
| `tactical/fbm-intercept` | FBM 拦截 | `tactical/fbm-intercept.md` | 待完成 |
| `tactical/poor-listing-winner` | 低质量 Listing 高销量 | `tactical/poor-listing-winner.md` | 待完成 |
| `tactical/high-ticket-long-tail` | 高客单长尾 | `tactical/high-ticket-long-tail.md` | 待完成 |
| `tactical/seasonal-prepositioning` | 季节前置爆破 | `tactical/seasonal-prepositioning.md` | 待完成 |

## 6. 每个 SellerSprite 预设的实现步骤

对每一份官方 Skill 单独执行以下流程，不要一次性凭经验填写 26 份。

### 步骤 A：读取官方文件

使用固定版本加载器取得完整官方包，再通过
`select_official_sellersprite_skill_prompt(...)` 选取单文件。

需要记录：

- 预设 ID；
- UI 名称；
- 官方文件；
- 官方执行步骤中出现的工具；
- 参考文档中出现的工具；
- 哪些工具被官方标记为可选；
- 是否存在顺序、并行、实体来源或站点要求。

### 步骤 B：建立静态清单

建议把当前三个单独常量收敛为一个简单的数据表，不要继续在
`web_app.py` 中复制 27 组 `if/elif`。

最小字段：

```python
SELLERSPRITE_OFFICIAL_PRESETS = {
    "comprehensive/product-research": {
        "label": "智能选品助手",
        "skill_file": "comprehensive/product-research.md",
        "tools": frozenset({
            "sellersprite__product_node",
            "sellersprite__product_research",
            "sellersprite__asin_detail",
            "sellersprite__asin_prediction",
            "sellersprite__market_research_statistics",
            "sellersprite__google_trend",
        }),
    },
}
```

这只是数据清单，不要在这里重新编码官方执行顺序。调用顺序仍由选中的官方 Skill 指导模型。

### 步骤 C：做四层校验

每个清单必须同时满足：

1. `skill_file` 在固定版本官方文件列表中；
2. 所有未加前缀工具名都能在选中官方文件中找到依据；
3. 所有 `sellersprite__*` 工具都存在于运行时 `tools/list`；
4. 清单中不存在其他 provider 前缀。

已知官方别名或文档名称与 MCP 运行时名称不一致时，必须建立显式、带注释的别名表，并在测试中引用官方原文位置。禁止静默猜测。

### 步骤 D：通用化路由

`sellersprite_official_skill_route(...)` 应做到：

- 显式 `officialPresetId` 优先；
- 受控中文前缀仅作为旧页面兼容；
- 已知预设返回单文件和对应工具集合；
- 未选择预设时保持现有完整官方目录行为；
- 未知 ID 保持当前记录日志并回退完整 SellerSprite 目录的兼容行为；
- 已知预设发生文件或运行时工具缺失时明确报错，不得替换成无依据工具；
- 始终只处理 `sellersprite__*`。

### 步骤 E：执行边界

工具 schema 的裁剪不是唯一保护。调用执行前仍需验证：

```text
requested_tool_id ∈ request_scoped_allowed_tool_ids
```

禁止只依赖“模型应该不会调用看不到的工具”。测试必须主动构造一个未授权工具调用，确认执行被拒绝。

## 7. 前端预设 ID 传递

当前 UI 只把中文受控前缀写入输入框，`/api/chat/ask` 已能接收
`officialPresetId`，但前端尚未实际发送。

后续实现要求：

1. 每张卡片增加稳定的 `data-official-preset-id`；
2. 中文 `data-official-preset` 继续只负责展示；
3. 选择卡片后，把预设 ID 保存在当前待发送请求状态中；
4. `askPayload` 对 Amazon 请求发送 `officialPresetId`；
5. 待发送队列、修改待发送内容和失败重试都要保留同一个预设 ID；
6. 成功发送或用户主动取消预设后清空；
7. 继续保留中文前缀兼容，避免旧缓存页面失效；
8. 不恢复工具选择按钮、位图或 `enabledToolMasks`。

不得把预设 ID长期写入全局 localStorage。它是单次请求范围，不应污染后续自由对话。

## 8. SociaVault 的适配边界

SociaVault 当前没有与 SellerSprite 等价的 27 份官方 Skill。其官方依据是：

- SociaVault MCP `tools/list`；
- 官方 MCP 文档；
- 运行时发现的官方工具名称和 schema。

现有 `scripts/social_tool_router.py` 的平台/能力分类属于 4004 自建路由，不是 SociaVault 官方权限规则。

后续完善必须保持：

- 平台规则优先、轻量模型兜底；
- 低置信度、非法输出、未知工具或空候选回退全部 SociaVault 工具；
- `off|shadow|enforce` 三种模式；
- 只裁剪 `sociavault__*`，系统/本地功能工具保持原规则；
- 不创建需要模型调用的“平台元工具”；
- 不增加前端工具选择；
- MCP 错误不回退旧 REST；
- `check_credits` 保持不缓存；
- 其他工具沿用当前缓存键与 TTL；
- 每个运行时官方工具都必须有平台/能力映射，未知新增工具在测试和启动诊断中暴露。

平台/能力映射可以参照 SellerSprite 的“静态清单 + 运行时校验 + 执行白名单”机制，但不得称为官方 Skill 分类。

## 9. 缓存、TTL 和 MCP 生命周期

本任务只改工具暴露，不改数据调用语义。

必须保持：

- MCP `tools/list` 运行时发现机制；
- 现有工具目录内存缓存；
- 现有工具结果缓存键；
- 各活动站点当前 TTL；
- 缓存命中/实时调用标识；
- stdio/HTTP MCP 的初始化、超时、并发和异常恢复；
- SociaVault `check_credits` 不缓存；
- 未经用户授权不主动执行付费 smoke query。

不要在预设清单中实现第二套缓存，也不要把“预设 ID”加入业务结果缓存键，除非工具名和参数之外确实改变了上游请求语义。

## 10. 答案事实边界

工具边界通过不代表答案质量自动通过。最新“智能选品助手”线上 review 暴露了以下通用问题，后续预设必须加入验收：

1. **派生指标复算**：环比、同比、增长率必须由时间序列复算，不能直接把含义不明的增长字段改名；
2. **实体粒度标记**：父体、子 ASIN、变体聚合数据必须明确区分；
3. **样本范围标记**：第一页样本、筛选样本、类目样本和全市场不能混用；
4. **时间口径标记**：不同接口价格、BSR、评分不一致时说明时间点和来源；
5. **推断标记**：投资、回报周期、转化率、季节归因、IP/专利风险必须标明推断和缺失条件；
6. **禁止过度完整性声明**：只读取第一页时不得写“所有市场数据已收集完毕”；
7. **筛选偏差检查**：例如设置 `maxRatings=500` 后，不能用筛选结果证明全市场评论门槛只有 500；
8. **官方声明不等于外部核验**：Listing 写有 `Patented Design` 只能表述为卖家声称，除非调用了对应核验工具。

不要直接修改固定官方 Skill 文本来塞入项目规则。事实边界应通过已有系统级防幻觉约束、结果规范化、确定性计算或独立质量校验完成。

## 11. 自动测试最低要求

### 12.1 清单完整性

- UI 27 张 SellerSprite 卡片都对应唯一预设 ID；
- 27 个预设 ID 都对应唯一官方文件；
- 官方文件都在固定版本清单内；
- 每个预设至少有一个允许工具；
- 没有跨 provider 工具；
- 全部运行时工具名称可解析。

### 12.2 官方来源

- 每个允许工具都能在对应官方文件的执行步骤或参考文档找到；
- 单预设提示包含来源头和目标官方文件；
- 单预设提示不包含其他综合/战术 Skill；
- 固定版本、提交和哈希校验继续通过。

### 12.3 路由与执行

- 显式 ID 与旧中文前缀得到相同路由；
- 已知预设只暴露其清单；
- 未选择预设仍保持当前站点默认行为；
- 未知预设保持兼容降级并有结构化日志；
- 已知预设工具缺失时明确报错；
- 人工构造的越界工具调用在执行层被拒绝；
- 旧 `enabledToolMasks` 继续被忽略；
- 总工具数不超过 DeepSeek 128 函数上限。

### 12.4 回归

至少继续运行：

```bash
python scripts/test_chat_tool_normalization.py
python scripts/test_social_tool_router.py
python scripts/test_ui_contract.py
python scripts/test_semantic_chinese_rendering.py
python scripts/test_api_cache.py
```

仓库没有统一 pytest/tox 配置，不要虚构测试命令。实际运行环境优先使用 Docker Compose；Windows 仅可使用项目已配置的 Python runtime 做静态和无密钥测试。

### 12.5 受控问答验收

每个预设至少准备：

- 1 个正常输入；
- 1 个缺少必要实体的输入；
- 1 个多意图输入；
- 1 个诱导调用白名单外工具的输入；
- 1 个工具空数据或错误 fixture；
- 1 个父体/子体或样本范围容易混淆的输入。

默认使用 fixture、mock 或已有缓存，不主动触发付费查询。真实付费 smoke query 必须获得用户明确授权。

## 12. 日志要求

每次官方预设请求至少记录：

- provider；
- preset ID；
- official skill file；
- runtime provider 工具总数；
- 暴露工具数；
- 实际调用工具；
- 是否发生越界拒绝；
- 是否存在运行时目录缺失；
- 是否发生兼容降级；
- 缓存命中状态沿用现有工具结果日志。

禁止记录：

- API Key；
- 完整用户输入；
- 完整工具返回；
- Cookie、代理凭据或其他敏感配置。

## 13. 多 Agent 协作建议

这些文件是冲突热点：

- `scripts/web_app.py`
- `scripts/static/chat.html`
- `scripts/test_chat_tool_normalization.py`

不要让多个 Agent 同时修改同一个热点文件。推荐按提交顺序串行合并：

1. SellerSprite 官方预设静态清单和来源测试；
2. SellerSprite 通用后端路由；
3. 前端显式 `officialPresetId`；
4. SellerSprite 27 预设回归；
5. SociaVault 映射完整性和现有路由回归；
6. 统一线上验收。

如果必须并行，先明确文件所有权，其他 Agent 只提交独立模块或独立测试文件，由一个协调 Agent 最终接入 `web_app.py` 和 `chat.html`。

## 14. 4004 部署流程

所有源代码从 Windows 到服务器必须经 GitHub：

1. Windows `codex/ui-beautification-4004` 提交；
2. 使用 `http://127.0.0.1:7892` 推送；
3. 服务器 `/home/openclaw/Video_analyzer-ui-4004` 使用
   `http://127.0.0.1:7890` 快进拉取同一分支；
4. 只执行 `scripts/deploy_ui_4004.sh`；
5. 不执行 4002/4003 Compose 命令。

部署前后记录：

- 4002 容器 ID 和健康状态；
- 4003 容器 ID 和健康状态；
- 4004 容器 ID 和健康状态；
- 4004 `/healthz`；
- Amazon/Home 只读工具目录；
- 已知预设的实际暴露工具数；
- 4002/4003 容器 ID 必须保持不变。

部署脚本执行 Docker build 可能超过 60 秒。若前台 SSH 等待超时，先检查远端进程和旧容器状态，再使用后台执行和轮询日志；不要盲目重复部署。

## 15. Definition of Done

只有同时满足以下条件才算完成：

- [ ] SellerSprite 27 个 UI 预设均有官方文件和工具清单；
- [ ] 清单依据可追溯到固定版本官方文本；
- [ ] 前端发送稳定预设 ID；
- [ ] 单预设只加载自己的官方 Skill；
- [ ] schema 暴露和执行层都实施相同白名单；
- [ ] 越界调用测试通过；
- [ ] 未知/缺失/空数据路径行为明确；
- [ ] SociaVault 没有被伪装成 SellerSprite 式官方预设；
- [ ] 缓存、TTL、MCP 生命周期和 REST 边界未改变；
- [ ] 事实边界回归覆盖派生指标、粒度、样本和推断；
- [ ] 自动回归通过；
- [ ] 4004 线上健康；
- [ ] 4002、4003 容器未变化；
- [ ] 未泄露或提交任何 Key。

## 16. 后续 Agent 开始前的最短检查

```text
1. 确认当前仓库是 Video_analyzer-ui-4004。
2. 确认分支是 codex/ui-beautification-4004。
3. 运行 git status，保留已有改动。
4. 先用 CodeGraph 定位相关符号和调用路径。
5. 读取目标官方 Skill 全文。
6. 写下“执行步骤工具、参考文档工具、可选工具”三张清单。
7. 与运行时 tools/list 对照。
8. 先补测试，再接入通用预设表。
9. 不修改缓存、TTL、REST 或其他端口。
10. 部署前再次确认只执行 4004 专用脚本。
```
