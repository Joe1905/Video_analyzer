# 市场发现 v1：4003 隔离后端

入口：`http://192.168.1.254:4003/api/market-discovery/manifest`。

本阶段提供可供 Luna 或后续新前端驱动的研究后端，不增加网页、导航、普通聊天预设、后台模型循环或推送配置。模型读取 Skill，自主取数和判断，按 API 保存记录。当前现有站点访问边界沿用原系统；不要将这个内部接口直接暴露到公网。

## 接口

| 方法 | 相对路径 | 用途 |
|---|---|---|
| GET | manifest | 两个 Skill、引用及运行约定 |
| GET | tools | 隔离白名单中的 SellerSprite/SociaVault schema |
| POST | tools/call | 只读外部取数，保存原始结果并返回 call_id |
| GET | calls/{id} | 回看原始请求、结果和时间 |
| GET | categories?q=关键词 | 数据库模糊检索；名称、别名、人群、需求、场景、地域 |
| GET | categories/{id} | 读取类目当前记录 |
| POST | categories | 新建/更新类目，乐观版本校验，历史保留 |
| GET | documents | 文档列表；key 查询正文，revision 查询历史，format=markdown 导出 |
| POST | documents | 保存候选池、证据、研究、可行性和日报文本 |

GET query 或 POST body 支持 `workspace`，所有数据和检索均按 workspace 隔离。读写示例和字段详见 manifest 内的 runtime。客户端需要正确处理 409，不能用重试覆盖其他研究的新版本。

## 数据与工具边界

Skill 随代码放在 `scripts/market_discovery_skills/`，通过现有 scripts 只读挂载加载。数据独立保存到 `data/market_discovery/research.sqlite`，不使用 chat_session 或商品实体注册表。研究文档及日报以 UTF-8 文本存入数据库，可导出标准 Markdown；原始工具结果和所有文档版本保留。

SellerSprite 使用现有 MCP 桥和服务端凭据，仅开放代码中明确列出的研究工具，同时要求实时 tools/list 中存在。SociaVault 使用固定官方 REST 路径及官方 MCP 2.0.0 的对应 schema，调用实时数据，不复用历史数据作为当日趋势。客户端不能传任意 endpoint、API key 或工具域。该隔离白名单不改变任何官方预设或现有聊天权限。

Google Trends 的 `google_trend` 是指定关键词趋势验证；候选应先来自 ABA 趋势发现或 TikTok 趋势等真实列表。具体日期和参数以实时 schema 为准。

SociaVault 研究白名单含 46 个工具：原有 19 个，加 YouTube 转录/回复/频道视频，X 搜索/原帖/评论/引用/转录，Facebook 公开主页/群组/帖子/评论/转录与 Meta 广告搜索，Instagram 内容/评论/转录、Threads 搜索/内容、Pinterest 搜索/pin。新增 schema 与路径来自官方 npm sociavault-mcp@2.0.0 的 dist/endpoints.js；保留现有工具定义。Facebook/Instagram 没有通用帖子关键词搜索，需定位公开 URL 后取数；官方预设权限不变。

## 验证

在 4003 checkout：

```bash
docker-compose -p short-video-analyzer-dev exec -T web python scripts/test_market_discovery.py
```

修改 Python 和 Skill 后通过 GitHub 同步，再 `docker-compose -p short-video-analyzer-dev restart web`。无需新增依赖或重建镜像。测试 workspace 与运营 workspace 分开；测试不会向运营人员发送消息。
