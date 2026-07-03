const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { createHash, randomUUID } = require("node:crypto");

const PORT = Number(process.env.PORT || 3001);
const HOST = process.env.HOST || "0.0.0.0";
const DEEPSEEK_API_URL = process.env.DEEPSEEK_API_URL || "https://api.deepseek.com/v1";
const DEEPSEEK_MODEL = process.env.DEEPSEEK_MODEL || "deepseek-chat";
const DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY || "";
const MCP_CHAT_TYPE = (process.env.MCP_CHAT_TYPE || "sellersprite").toLowerCase();
const MCP_CHAT_LABEL = process.env.MCP_CHAT_LABEL || (MCP_CHAT_TYPE === "fastmoss" ? "FastMoss" : "SellerSprite");
const MCP_CHAT_BASE_PATH = process.env.MCP_CHAT_BASE_PATH || (MCP_CHAT_TYPE === "fastmoss" ? "/fastmoss" : "/amazon");
const SELLERSPRITE_SECRET_KEY = process.env.SELLERSPRITE_SECRET_KEY || "";
const SELLERSPRITE_MCP_URL = process.env.SELLERSPRITE_MCP_URL || "https://mcp.sellersprite.com/mcp";
const SELLERSPRITE_CACHE_TTL_SECONDS = Number(process.env.SELLERSPRITE_CACHE_TTL_SECONDS || 86400);
const FASTMOSS_MCP_API_KEY = process.env.FASTMOSS_MCP_API_KEY || process.env.FASTMOSS_API_KEY || "";
const FASTMOSS_MCP_URL = process.env.FASTMOSS_MCP_URL || "https://mcp.fastmoss.com/mcp";
const FASTMOSS_CACHE_TTL_SECONDS = Number(process.env.FASTMOSS_CACHE_TTL_SECONDS || 86400);
const MCP_REMOTE_URL = process.env.MCP_REMOTE_URL || (MCP_CHAT_TYPE === "fastmoss" ? FASTMOSS_MCP_URL : SELLERSPRITE_MCP_URL);
const MCP_CACHE_TTL_SECONDS = Number(process.env.MCP_CACHE_TTL_SECONDS || (MCP_CHAT_TYPE === "fastmoss" ? FASTMOSS_CACHE_TTL_SECONDS : SELLERSPRITE_CACHE_TTL_SECONDS));
const PUBLIC_DIR = path.join(__dirname, "public");
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "data");
const SESSIONS_FILE = path.join(DATA_DIR, "sessions.json");
const TOOL_CACHE_FILE = path.join(DATA_DIR, "tool_cache.json");

const sessions = new Map();
let toolCache = null;
let saveTimer = null;

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

function providerCacheKey() {
  return MCP_CHAT_TYPE === "fastmoss" ? "fastmoss_mcp" : "sellersprite_mcp";
}

function providerKeywordsPattern() {
  if (MCP_CHAT_TYPE === "fastmoss") {
    return /FastMoss|TikTok Shop|TikTok|小店|达人|创作者|带货|商品|产品|店铺|竞品|类目|销量|销售额|GMV|直播|短视频|广告|素材|内容|定价|美国|美区|英区|US|UK|creator|shop|product|GMV|trend/i;
  }
  return /Amazon|亚马逊|ASIN|asin|关键词|产品|商品|类目|市场|选品|销量|销售额|BSR|竞品|品牌|评论|流量|趋势|抓取|查询|分析|US|UK|DE|JP|智能|家居/i;
}

function mcpRequestUrl() {
  if (MCP_CHAT_TYPE !== "fastmoss") return MCP_REMOTE_URL;
  if (!FASTMOSS_MCP_API_KEY || /[?&]api_key=/.test(MCP_REMOTE_URL)) return MCP_REMOTE_URL;
  const separator = MCP_REMOTE_URL.includes("?") ? "&" : "?";
  return `${MCP_REMOTE_URL}${separator}api_key=${encodeURIComponent(FASTMOSS_MCP_API_KEY)}`;
}

function mcpRequestHeaders() {
  const headers = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
  };
  if (MCP_CHAT_TYPE === "sellersprite") headers["secret-key"] = SELLERSPRITE_SECRET_KEY;
  return headers;
}

function ensureMcpConfigured() {
  if (MCP_CHAT_TYPE === "sellersprite" && !SELLERSPRITE_SECRET_KEY) throw new Error("请先设置环境变量 SELLERSPRITE_SECRET_KEY。");
  if (MCP_CHAT_TYPE === "fastmoss" && !FASTMOSS_MCP_API_KEY && !/[?&]api_key=/.test(MCP_REMOTE_URL)) {
    throw new Error("请先设置环境变量 FASTMOSS_MCP_API_KEY，或在 FASTMOSS_MCP_URL 中包含 api_key。");
  }
}

function guideMessage() {
  if (MCP_CHAT_TYPE === "fastmoss") {
    return [
      "### FastMoss MCP 使用手册",
      "",
      "直接输入自然语言即可，我会通过 DeepSeek 调用 FastMoss 官方 MCP 工具。",
      "",
      "- **选品研究**：查询 TikTok Shop 商品榜单、销量、GMV、趋势和价格带。",
      "- **竞品店铺**：分析店铺 GMV、渠道构成、热销商品和达人活跃度。",
      "- **达人建联**：按带货力、垂类、GMV 潜力和受众匹配筛选达人。",
      "- **内容辅助**：基于真实销售和互动数据生成脚本、钩子和商品文案。",
      "- **账户能力**：可询问当前 MCP 工具列表和调用方式。",
      "",
      "示例：`使用 FastMoss 查看今天美区美妆品类销量前 3 的商品，返回商品名、价格、销量、GMV 和趋势`。",
    ].join("\n");
  }
  return USER_GUIDE_MESSAGE;
}

function systemPrompt() {
  if (MCP_CHAT_TYPE === "fastmoss") {
    return [
      "你是 FastMoss MCP 的中文 TikTok Shop 商业分析助手，面向跨境电商选品、竞品店铺、达人建联、内容脚本、广告素材和定价分析。",
      "",
      "工具调用原则：",
      "1. 用户询问 TikTok Shop、FastMoss、商品、店铺、达人、带货、销量、GMV、类目、趋势、内容、广告、定价或竞品数据时，必须先调用 FastMoss MCP 工具；不要直接凭常识回答。",
      "2. 如果缺少必要参数，先问用户补充；地区或时间窗未说明时可默认美区和近期/今天，但要在回答中说明默认值。",
      "3. 没有实际工具结果时，不得声称已查询实时数据；工具未授权、次数用尽或空数据时，要说明真实错误。",
      "4. 已有部分成功工具结果时，继续基于成功结果分析，并把缺失数据标注为未获取到。",
      "",
      "回答质量要求：",
      "1. 输出中文 Markdown 商业分析，不要只给一句概括。",
      "2. 选品/类目分析至少覆盖销量/GMV、价格带、趋势、竞争格局、内容机会、达人合作和下一步动作。",
      "3. 店铺/竞品分析至少覆盖热销商品、渠道构成、达人活跃度、内容打法、优势风险和可执行建议。",
      "4. 所有数字和事实必须来自工具结果；没有拿到的数据明确标注未获取到。",
      "5. 不要输出 HTML/H5 标签。",
    ].join("\n");
  }
  return [
    "你是 SellerSprite Open API 的中文商业分析助手，面向跨境电商选品、类目研究、ASIN/关键词/品牌/竞品分析。",
    "",
    "工具调用原则：",
    "1. 用户询问 Amazon、亚马逊、ASIN、关键词、品牌、类目、产品、销量、销售额、BSR、流量、趋势、评论、竞品、选品、市场分析或站点数据时，必须先调用 SellerSprite MCP 工具；不要直接凭常识回答。",
    "2. 如果用户给出 ASIN 或 Amazon URL，先调用适合 ASIN/商品详情的工具；如果用户给出类目/关键词，先定位类目或关键词，再补充市场/产品/分布/竞争数据。",
    "3. 如果缺少必填参数，先问用户补充；但站点未说明时可默认 US，并在回答中说明默认值。",
    "4. 没有实际工具结果时，不得声称已查询、API 未授权、次数用尽、工具异常或已得到市场数据。",
    "5. 工具返回未授权、错误或空数据时，必须说明真实错误；如果已有其他成功工具结果，要继续基于成功结果分析，不要因为部分失败而放弃回答。",
    "",
    "回答质量要求：",
    "1. 不要只给概括结论。用户问类目、市场、产品情况时，输出一份详实报告。",
    "2. 类目/市场分析至少覆盖：类目定位、市场规模与容量、价格带、评分评论、品牌集中度、卖家集中度、物流/卖家来源、头部产品或代表产品、竞争格局、机会点、风险点、可切入方向、产品改进建议、定价/运营/差异化建议、下一步补查项。",
    "3. 单品/ASIN 分析至少覆盖：产品基础信息、价格/变体、卖点、评分评论、销量/BSR/趋势、关键词与流量机会、竞品对比、用户痛点、改进方向、进入建议和行动清单。",
    "4. 具体建议要尽量落到产品形态、功能、价格带、人群、场景、包装、内容营销或供应链动作，不要只写泛泛的‘优化产品’。",
    "5. 所有数字和事实必须来自工具结果；没有拿到的数据明确标注‘未获取到’。",
    "6. 回复只能使用 Markdown，不要输出 HTML/H5 标签。",
  ].join("\n");
}
const USER_GUIDE_MESSAGE = [
  "### SellerSprite MCP 使用手册",
  "",
  "直接输入自然语言即可，我会通过 DeepSeek 调用 SellerSprite 官方 MCP 工具。",
  "",
  "- **ASIN 分析**：输入站点和 ASIN，查询详情、趋势、销量预测、BSR 等。",
  "- **关键词研究**：查询搜索量、趋势、相关词、流量词、选品机会。",
  "- **商品/品牌调研**：输入品牌、类目或商品线索，查询可用工具返回的数据。",
  "- **图片 OCR**：上传或粘贴图片时，可让模型选择 OCR 相关工具识别文字。",
  "- **账户能力**：可询问当前 MCP 工具列表和调用方式。",
  "",
  "示例：`查 US 站 ASIN B0CXYZ1234 的竞品数据`、`帮我看关键词 coffee maker 的搜索量`、`识别这张图里的文字`。",
].join("\n");

class RemoteMcpClient {
  constructor() {
    this.initialized = false;
    this.cachedTools = null;
  }

  async request(method, params = {}) {
    ensureMcpConfigured();
    const payload = { jsonrpc: "2.0", id: randomUUID(), method, params };
    const response = await fetch(mcpRequestUrl(), {
      method: "POST",
      headers: mcpRequestHeaders(),
      body: JSON.stringify(payload),
    });

    const text = await response.text();
    const data = parseMcpResponse(text);
    if (!response.ok) throw new Error(data?.error?.message || data?.message || `${MCP_CHAT_LABEL} MCP request failed: ${response.status}`);
    if (data?.error) throw new Error(data.error.message || `${MCP_CHAT_LABEL} MCP request failed.`);
    return data?.result ?? data;
  }

  async initialize() {
    if (this.initialized) return;
    await this.request("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: `${MCP_CHAT_TYPE}-deepseek-chat`, version: "0.3.0" },
    });
    this.initialized = true;
  }

  async listTools() {
    if (this.cachedTools) return this.cachedTools;
    await this.initialize();
    const result = await this.request("tools/list", {});
    this.cachedTools = Array.isArray(result.tools) ? result.tools : [];
    return this.cachedTools;
  }

  async callTool(name, args) {
    await this.initialize();
    return this.request("tools/call", { name, arguments: args || {} });
  }
}

const mcpClient = new RemoteMcpClient();

function parseMcpResponse(text) {
  const trimmed = text.trim();
  if (!trimmed) return {};
  if (trimmed.startsWith("{")) return safeJsonParse(trimmed, {});

  for (const block of trimmed.split(/\n\n+/)) {
    const dataLines = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim());
    if (!dataLines.length) continue;
    const parsed = safeJsonParse(dataLines.join("\n"), null);
    if (parsed) return parsed;
  }
  return { message: trimmed };
}

function getSessionIdFromUrl(url) {
  return normalizeSessionId(url.searchParams.get("session"));
}

function getSessionIdFromPayload(payload) {
  return normalizeSessionId(payload?.sessionId || payload?.session);
}

function normalizeSessionId(value) {
  const cleaned = String(value || "default").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
  return cleaned || "default";
}

function getSession(sessionId) {
  if (!sessions.has(sessionId)) {
    sessions.set(sessionId, {
      id: sessionId,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      messages: [],
      sseClients: new Set(),
    });
  }
  return sessions.get(sessionId);
}

function findSession(sessionId) {
  return sessions.get(normalizeSessionId(sessionId));
}

function serializeSessions() {
  return Array.from(sessions.values()).map((session) => ({
    id: session.id,
    title: session.title || "",
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
    messages: session.messages || [],
  }));
}

async function loadSessionsFromDisk() {
  try {
    const raw = await fs.readFile(SESSIONS_FILE, "utf8");
    const saved = safeJsonParse(raw, []);
    if (!Array.isArray(saved)) return;
    for (const item of saved) {
      const id = normalizeSessionId(item?.id);
      const messages = Array.isArray(item?.messages) ? item.messages : [];
      if (!messages.length) continue;
      sessions.set(id, {
        id,
        title: String(item.title || ""),
        createdAt: item.createdAt || new Date().toISOString(),
        updatedAt: item.updatedAt || new Date().toISOString(),
        messages,
        sseClients: new Set(),
      });
    }
    console.log(`Loaded ${sessions.size} persisted SellerSprite sessions.`);
  } catch (error) {
    if (error.code !== "ENOENT") console.warn(`Could not load sessions: ${error.message}`);
  }
}

async function saveSessionsToDisk() {
  await fs.mkdir(DATA_DIR, { recursive: true });
  const payload = JSON.stringify(serializeSessions(), null, 2);
  await fs.writeFile(SESSIONS_FILE, payload, "utf8");
}

function scheduleSessionSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    saveSessionsToDisk().catch((error) => console.warn(`Could not save sessions: ${error.message}`));
  }, 100);
}

function installShutdownSave() {
  const shutdown = async (signal) => {
    try {
      clearTimeout(saveTimer);
      await saveSessionsToDisk();
    } catch (error) {
      console.warn(`Could not save sessions before shutdown: ${error.message}`);
    } finally {
      process.exit(signal === "SIGINT" ? 130 : 143);
    }
  };
  process.once("SIGINT", () => shutdown("SIGINT"));
  process.once("SIGTERM", () => shutdown("SIGTERM"));
}

function sendJson(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload));
}

function safeJsonParse(value, fallback = {}) {
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function normalizeCacheValue(value) {
  if (Array.isArray(value)) return value.map(normalizeCacheValue);
  if (!value || typeof value !== "object") return value;
  return Object.keys(value)
    .sort()
    .reduce((acc, key) => {
      acc[key] = normalizeCacheValue(value[key]);
      return acc;
    }, {});
}

function canonicalJson(value) {
  return JSON.stringify(normalizeCacheValue(value));
}

function sellerSpriteCacheKey(toolName, args) {
  return createHash("sha256")
    .update(canonicalJson({ provider: providerCacheKey(), endpoint: toolName, request: args || {} }))
    .digest("hex");
}

async function loadToolCache() {
  if (toolCache) return toolCache;
  try {
    const raw = await fs.readFile(TOOL_CACHE_FILE, "utf8");
    const parsed = safeJsonParse(raw, {});
    toolCache = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (error) {
    if (error.code !== "ENOENT") console.warn(`Could not load ${MCP_CHAT_LABEL} tool cache: ${error.message}`);
    toolCache = {};
  }
  return toolCache;
}

async function saveToolCache() {
  if (!toolCache) return;
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.writeFile(TOOL_CACHE_FILE, JSON.stringify(toolCache, null, 2), "utf8");
}

function withCacheMeta(value, meta) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return { ...value, _cache: meta };
  }
  return { value, _cache: meta };
}

async function callSellerSpriteToolCached(name, args) {
  const cache = await loadToolCache();
  const key = sellerSpriteCacheKey(name, args);
  const now = Date.now();
  const ttlMs = Math.max(1, MCP_CACHE_TTL_SECONDS) * 1000;
  const entry = cache[key];
  if (entry && now - Number(entry.createdAt || 0) <= ttlMs) {
    entry.hitCount = Number(entry.hitCount || 0) + 1;
    entry.lastHitAt = now;
    saveToolCache().catch((error) => console.warn(`Could not save ${MCP_CHAT_LABEL} tool cache: ${error.message}`));
    return withCacheMeta(entry.response, {
      hit: true,
      label: "缓存命中",
      provider: providerCacheKey(),
      endpoint: name,
      ttl_seconds: MCP_CACHE_TTL_SECONDS,
      age_seconds: Math.round((now - Number(entry.createdAt || now)) / 1000),
    });
  }

  const response = await mcpClient.callTool(name, args);
  cache[key] = {
    provider: providerCacheKey(),
    endpoint: name,
    request: normalizeCacheValue(args || {}),
    response,
    createdAt: now,
    lastHitAt: null,
    hitCount: 0,
  };
  await saveToolCache();
  return withCacheMeta(response, {
    hit: false,
    label: "实时调用",
    provider: providerCacheKey(),
    endpoint: name,
    ttl_seconds: MCP_CACHE_TTL_SECONDS,
    age_seconds: 0,
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 10_000_000) {
        reject(new Error("Request body is too large."));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

function createMessage(role, content, extras = {}) {
  return {
    id: extras.id || randomUUID(),
    role,
    content,
    attachments: extras.attachments || [],
    request: extras.request || null,
    raw: extras.raw || null,
    status: extras.status || "done",
    createdAt: extras.createdAt || new Date().toISOString(),
  };
}

function broadcast(session, event, payload) {
  const data = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
  for (const client of session.sseClients) client.write(data);
}

function addMessage(session, message) {
  session.messages.push(message);
  session.updatedAt = new Date().toISOString();
  broadcast(session, "message", message);
  scheduleSessionSave();
  return message;
}

function updateMessage(session, id, patch) {
  const message = session.messages.find((item) => item.id === id);
  if (!message) return null;
  Object.assign(message, patch);
  session.updatedAt = new Date().toISOString();
  broadcast(session, "message", message);
  scheduleSessionSave();
  return message;
}

function listSessions() {
  return Array.from(sessions.values())
    .map((session) => {
      const lastMessage =
        [...session.messages].reverse().find((message) => message.role === "user") ||
        [...session.messages].reverse().find((message) => message.role === "assistant");
      return {
        id: session.id,
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        messageCount: session.messages.length,
        title: session.title || session.id,
        lastMessage: lastMessage ? String(lastMessage.content || "").slice(0, 80) : "",
      };
    })
    .filter((session) => session.messageCount > 0)
    .sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt));
}

function getMessagesPage(session, url) {
  const all = session?.messages || [];
  const maxChars = Math.min(Math.max(Number(url.searchParams.get("maxChars") || 20000), 2000), 100000);
  const before = url.searchParams.get("before");
  let end = all.length;
  if (before) {
    const index = all.findIndex((message) => message.id === before);
    end = index >= 0 ? index : all.length;
  }
  let start = end;
  let usedChars = 0;
  while (start > 0) {
    const message = all[start - 1];
    const messageChars = JSON.stringify(message).length;
    if (usedChars > 0 && usedChars + messageChars > maxChars) break;
    usedChars += messageChars;
    start -= 1;
  }
  const messages = all.slice(start, end);
  return {
    ok: true,
    messages,
    total: all.length,
    usedChars,
    maxChars,
    hasMore: start > 0,
    nextBefore: messages[0]?.id || null,
  };
}

function summarizeSessionTitle(text) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return "Untitled";
  return clean.length > 24 ? `${clean.slice(0, 24)}...` : clean;
}

function normalizeAttachments(value) {
  if (!Array.isArray(value)) return [];
  return value
    .slice(0, 6)
    .filter((item) => item?.dataUrl?.startsWith("data:image/"))
    .map((item) => ({
      id: randomUUID(),
      name: String(item.name || "image").slice(0, 120),
      type: String(item.type || "image/png").slice(0, 80),
      size: Number(item.size || 0),
      dataUrl: item.dataUrl,
    }))
    .filter((item) => item.dataUrl.length <= 8_000_000);
}

function sanitizeDeepSeekPayload(value) {
  if (Array.isArray(value)) return value.map(sanitizeDeepSeekPayload);
  if (!value || typeof value !== "object") return value;

  const sanitized = {};
  for (const [key, child] of Object.entries(value)) {
    if (key === "reasoning_content") continue;
    sanitized[key] = sanitizeDeepSeekPayload(child);
  }
  return sanitized;
}

function recentChatForModel(session) {
  return session.messages
    .filter((message) => message.status === "done" && ["user", "assistant"].includes(message.role))
    .slice(-12)
    .map((message) => ({
      role: message.role,
      content: message.attachments?.length
        ? `${message.content || ""}\n\n[用户上传了 ${message.attachments.length} 张图片。图片内容可交给 MCP OCR 或图片相关工具处理，或根据工具参数要求补充。]`
        : message.content,
    }));
}

function toDeepSeekTool(tool) {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.description || tool.name,
      parameters: tool.inputSchema || { type: "object", properties: {}, additionalProperties: true },
    },
  };
}

function parseSellerSpriteToolPayload(result) {
  const text = result?.content?.find((item) => item?.type === "text")?.text;
  if (!text) return null;
  const parsed = safeJsonParse(text, null);
  return parsed && typeof parsed === "object" ? parsed : null;
}

function getSellerSpriteToolError(result) {
  if (result?.isError) return { code: "MCP_TOOL_ERROR", message: "SellerSprite MCP 工具返回错误。" };
  const payload = parseSellerSpriteToolPayload(result);
  if (!payload) return null;
  const code = String(payload.code || "");
  const message = String(payload.message || "");
  if (code.startsWith("ERROR") || code.includes("UNAUTHORIZED") || message.includes("未授权")) {
    return { code: code || "SELLERSPRITE_ERROR", message: message || "SellerSprite MCP 工具返回错误。", data: payload.data ?? null };
  }
  return null;
}

function formatToolError(toolName, error) {
  if (error?.code === "ERROR_UNAUTHORIZED" || error?.message?.includes("未授权")) {
    return [
      "### SellerSprite MCP 授权异常",
      "",
      `工具 \`${toolName}\` 返回：\`${error.code || "ERROR"}\`，${error.message || "未授权"}。`,
      "",
      "这不是前端显示问题，也不是 DeepSeek 初始回复问题；是 SellerSprite MCP 在实际工具调用阶段拒绝了请求。",
      "",
      "请检查：",
      "- 当前 `SELLERSPRITE_SECRET_KEY` 是否仍有效。",
      "- 该密钥对应账号是否有调用这个工具/API 的权限。",
      "- SellerSprite 后台是否需要重新生成或绑定 MCP secret。",
      "",
      "我不会基于未授权结果编造实时市场数据。授权恢复后再发送同样的问题即可继续查询。",
    ].join("\n");
  }

  return [
    "### SellerSprite MCP 工具调用失败",
    "",
    `工具 \`${toolName}\` 返回：\`${error?.code || "ERROR"}\`，${error?.message || "未知错误"}。`,
    "",
    "请调整参数或稍后重试。",
  ].join("\n");
}

function parseWrappedToolData(wrapped) {
  const text = wrapped?.data?.content?.find((item) => item?.type === "text")?.text;
  const parsed = text ? safeJsonParse(text, null) : null;
  return parsed && typeof parsed === "object" ? parsed : null;
}

function formatValue(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "number") return `${value.toLocaleString("en-US")}${suffix}`;
  return `${value}${suffix}`;
}

function latestRecoverableToolResult(session) {
  const messages = [...(session?.messages || [])].reverse();
  return messages.find((message) => {
    const results = message?.raw?.toolResults;
    return (
      message.role === "assistant" &&
      Array.isArray(results) &&
      results.some((item) => item?.result?.ok) &&
      results.some((item) => item?.result?.ok === false)
    );
  });
}

function buildRecoveredAnswer(message) {
  const toolResults = message?.raw?.toolResults || [];
  const successful = toolResults.filter((item) => item?.result?.ok);
  const failed = toolResults.filter((item) => item?.result?.ok === false);
  const statsTool = successful.find((item) => {
    const name = item?.toolCall?.function?.name || "";
    return name.includes("statistics") || name === "market_research";
  });
  const statsPayload = parseWrappedToolData(statsTool?.result);
  const stats = statsPayload?.data && typeof statsPayload.data === "object" ? statsPayload.data : null;
  const nodeTool = successful.find((item) => item?.toolCall?.function?.name === "product_node");
  const nodePayload = parseWrappedToolData(nodeTool?.result);
  const firstNode = Array.isArray(nodePayload?.data) ? nodePayload.data[0] : null;

  const title = stats?.nodeLabelPathLocale || stats?.nodeLabelPath || firstNode?.nodeLabelPathLocale || firstNode?.nodeLabelPath || "已识别类目";
  const rows = [
    ["类目路径", title],
    ["商品样本数", stats?.products ?? stats?.totalProducts],
    ["品牌数", stats?.brands],
    ["卖家数", stats?.sellers],
    ["平均月销量", stats?.avgUnits],
    ["平均月销售额", stats?.avgRevenue === undefined ? undefined : `$${formatValue(stats.avgRevenue)}`],
    ["平均售价", stats?.avgPrice === undefined ? undefined : `$${formatValue(stats.avgPrice)}`],
    ["平均评分", stats?.avgRating],
    ["平均评分数", stats?.avgRatings],
    ["平均利润率", stats?.avgProfit === undefined ? undefined : `${formatValue(stats.avgProfit)}%`],
    ["新品数量", stats?.newProducts],
    ["新品占比", stats?.newProductProportion === undefined ? undefined : `${formatValue(stats.newProductProportion)}%`],
    ["新品平均销量", stats?.newAvgUnits],
    ["新品平均销售额", stats?.newAvgRevenue === undefined ? undefined : `$${formatValue(stats.newAvgRevenue)}`],
    ["最早上架", stats?.firstShelfDate],
    ["最新上架", stats?.lastShelfDate],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");

  const failedLines = failed.map((item) => {
    const name = item?.toolCall?.function?.name || item?.result?.request?.apiPath || "unknown_tool";
    const code = item?.result?.error?.code || "ERROR";
    return `- \`${name}\`: \`${code}\``;
  });

  return [
    "### 降级分析结果（基于已成功返回的数据）",
    "",
    "上一轮部分补充工具失败，但已有核心查询结果可用。下面不重新消耗 SellerSprite 次数，只基于已成功返回的数据整理：",
    "",
    "| 指标 | 数据 |",
    "|---|---:|",
    ...rows.map(([label, value]) => `| ${label} | ${formatValue(value)} |`),
    "",
    "### 初步判断",
    "",
    `- 该类目可先按 \`${title}\` 作为分析口径。`,
    `- 样本商品数为 ${formatValue(stats?.products ?? stats?.totalProducts)}，品牌数 ${formatValue(stats?.brands)}，卖家数 ${formatValue(stats?.sellers)}，竞争主体并不少。`,
    `- 平均售价约 ${stats?.avgPrice === undefined ? "-" : `$${formatValue(stats.avgPrice)}`}，平均利润率约 ${stats?.avgProfit === undefined ? "-" : `${formatValue(stats.avgProfit)}%`}，可以作为初步价格和毛利参考。`,
    `- 新品占比约 ${stats?.newProductProportion === undefined ? "-" : `${formatValue(stats.newProductProportion)}%`}，说明该类目仍有新品进入，但需要结合具体竞品和价格分布进一步确认。`,
    "",
    "### 本轮已跳过的失败数据",
    "",
    failedLines.length ? failedLines.join("\n") : "- 无",
    "",
    "以上失败工具返回的是 SellerSprite MCP 内部错误，不代表前面成功返回的数据无效；后续可以等接口恢复后再补查价格分布、竞品列表等细分数据。",
  ].join("\n");
}

function isBlockingToolError(error, successfulToolCount) {
  const code = String(error?.code || "");
  const message = String(error?.message || "");
  if (code === "ERROR_UNAUTHORIZED" || code.includes("UNAUTHORIZED") || message.includes("未授权")) return true;
  if (message.includes("没有可使用的次数") || message.includes("当前没有可使用的次数")) return true;
  return successfulToolCount <= 0;
}

function shouldRequireSellerSpriteTool(text) {
  return providerKeywordsPattern().test(
    String(text || "")
  );
}

async function synthesizeSellerSpriteAnswer(conversation, deepseekResponses, toolResults, firstRequest, reason) {
  const finalConversation = conversation.slice();
  while (finalConversation.length && finalConversation.at(-1)?.role === "assistant" && Array.isArray(finalConversation.at(-1)?.tool_calls)) {
    finalConversation.pop();
  }
  finalConversation.push({
    role: "user",
    content: [
      `工具调用边界已触发：${reason}。现在必须停止调用工具，直接基于上文已有 SellerSprite 工具结果输出最终中文 Markdown 总结。`,
      "",
      "输出要求：",
      "1. 不要再请求任何工具，不要输出 DSML、tool_calls、invoke、parameter、JSON 工具请求或伪工具调用标签。",
      "2. 不要只做一句话摘要。即使数据不完整，也要把已获得的数据整理成一份可读的商业分析。",
      "3. 类目/市场分析至少包含：类目定位、市场规模与容量、价格带、评分/评论、品牌与卖家集中度、头部产品或代表产品、竞争格局、机会点、风险点、可切入产品方向、运营/定价/差异化建议、下一步需要补查的数据。",
      "4. 单品/ASIN 分析至少包含：产品定位、价格与变体、评分评论、卖点、竞品/替代品、关键词机会、风险、改进方向、是否值得进入、具体行动建议。",
      "5. 每个建议必须说明依据来自哪些已返回数据；缺失的数据明确写“未获取到”，不要编造数值。",
      "6. 使用中文 Markdown，优先用二级/三级标题、表格和项目符号，让回答详实、可执行。",
    ].join("\n"),
  });
  const response = await callDeepSeek({
    model: DEEPSEEK_MODEL,
    messages: finalConversation,
  });
  deepseekResponses.push(sanitizeDeepSeekPayload(response));
  const assistant = response.choices?.[0]?.message || {};
  const content = assistant.content || "";
  if (/DSML|tool_calls|invoke name=|parameter name=/i.test(content)) {
    return {
      content: formatToolBoundaryFallback(toolResults, reason),
      request: firstRequest,
      raw: { deepseek: deepseekResponses, toolResults, toolBoundary: reason, boundaryFallback: true },
    };
  }
  return {
    content: content || "已达到工具调用边界，但没有拿到可用总结。请缩小问题范围后重试。",
    request: firstRequest,
    raw: { deepseek: deepseekResponses, toolResults, toolBoundary: reason },
  };
}

function formatToolBoundaryFallback(toolResults, reason) {
  const successful = (toolResults || []).filter((item) => item?.result?.ok);
  const failed = (toolResults || []).filter((item) => item?.result?.ok === false);
  const lines = successful.map((item, index) => {
    const name = item?.toolCall?.function?.name || item?.result?.request?.apiPath || "unknown_tool";
    const cache = item?.result?.cache?.label ? `，${item.result.cache.label}` : "";
    return `${index + 1}. \`${name}\`：成功${cache}`;
  });
  const failedLines = failed.map((item) => {
    const name = item?.toolCall?.function?.name || item?.result?.request?.apiPath || "unknown_tool";
    const code = item?.result?.error?.code || item?.result?.error?.message || "ERROR";
    return `- \`${name}\`：${code}`;
  });
  return [
    "### 已停止继续调用工具",
    "",
    `本轮已触发工具调用边界：\`${reason}\`。模型仍尝试继续发起工具调用，为避免循环和消耗次数，系统已停止后续调用。`,
    "",
    "### 已成功获取的数据",
    "",
    lines.length ? lines.join("\n") : "- 暂无成功工具结果",
    "",
    failedLines.length ? "### 跳过或失败的数据\n\n" + failedLines.join("\n") + "\n" : "",
    "### 下一步建议",
    "",
    "- 如果要继续做类目分析，请指定更明确的类目节点、关键词或筛选条件。",
    "- 如果要分析单品，请直接给 ASIN；ASIN 查询会走更适合的工具集合。",
    "- 如果要减少工具调用，可直接说明只查市场概览、只查产品列表或只查价格分布。",
  ].filter(Boolean).join("\n");
}

async function callDeepSeek(payload) {
  if (!DEEPSEEK_API_KEY) throw new Error("请先设置环境变量 DEEPSEEK_API_KEY。");

  const response = await fetch(`${DEEPSEEK_API_URL.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${DEEPSEEK_API_KEY}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  const data = safeJsonParse(text, { error: { message: text } });
  if (!response.ok) throw new Error(data.error?.message || data.message || `DeepSeek 请求失败：${response.status}`);
  return data;
}

async function answerWithDeepSeek(session, userText) {
  const history = recentChatForModel(session);
  if (history.at(-1)?.role === "user" && history.at(-1)?.content === userText) history.pop();

  const tools = await mcpClient.listTools();
  const conversation = [
    {
      role: "system",
      content: systemPrompt(),
    },
    ...history,
    { role: "user", content: userText },
  ];
  const deepseekResponses = [];
  const toolResults = [];
  let firstRequest = null;
  const requireToolFirst = shouldRequireSellerSpriteTool(userText);
  conversation.splice(1, 0, {
    role: "system",
    content: [
      "When an MCP tool result has ok:false but at least one previous MCP tool result has ok:true, continue using only the successful tool data.",
      "Mention failed tool names and error codes briefly at the end as skipped or missing data. Do not invent missing values from failed tools.",
      "If successful MCP results exist, the final answer must be a complete Chinese Markdown business report, not a short refusal or a tool-status summary.",
      "For category and product-market questions, include actionable recommendations and explain what each recommendation is based on.",
    ].join(" "),
  });

  for (let step = 0; step < 6; step += 1) {
    const response = await callDeepSeek({
      model: DEEPSEEK_MODEL,
      messages: conversation,
      tools: tools.map(toDeepSeekTool),
      tool_choice: "auto",
    });
    deepseekResponses.push(sanitizeDeepSeekPayload(response));

    const assistant = response.choices?.[0]?.message || {};
    conversation.push(assistant);
    const toolCalls = Array.isArray(assistant.tool_calls) ? assistant.tool_calls : [];
    if (!toolCalls.length) {
      if (requireToolFirst && step === 0) {
        return {
          content: [
            "### 需要实际调用 SellerSprite MCP",
            "",
            "这个问题属于 Amazon/SellerSprite 数据查询，必须先调用 MCP 工具才能给出结果。",
            "",
            "本轮 DeepSeek 没有选择任何工具，因此我不会采纳它的直接回答，也不会编造授权、次数或市场数据。",
            "",
            "你可以补充更明确的查询条件后重试，例如：",
            "- 站点：US",
            "- ASIN、关键词或类目节点",
            "- 想查的维度：产品列表、类目结构、关键词、销量、竞品等",
          ].join("\n"),
          request: firstRequest,
          raw: { deepseek: deepseekResponses, toolResults },
        };
      }
      return { content: assistant.content || "我没有拿到有效回复。", request: firstRequest, raw: { deepseek: deepseekResponses, toolResults } };
    }

    for (const toolCall of toolCalls) {
      const name = toolCall.function?.name;
      const args = safeJsonParse(toolCall.function?.arguments, {});
      const result = await callSellerSpriteToolCached(name, args);
      const toolError = getSellerSpriteToolError(result);
      const wrapped = {
        ok: !toolError,
        status: toolError ? 400 : 200,
        request: { method: "MCP", apiPath: name },
        error: toolError,
        data: result,
        cache: result?._cache || null,
      };
      if (!firstRequest) firstRequest = wrapped.request;
      toolResults.push({ toolCall, args, result: wrapped });
      if (toolError) {
        const successfulToolCount = toolResults.filter((item) => item?.result?.ok).length;
        if (isBlockingToolError(toolError, successfulToolCount)) {
          return { content: formatToolError(name, toolError), request: firstRequest, raw: { deepseek: deepseekResponses, toolResults } };
        }
      }
      conversation.push({ role: "tool", tool_call_id: toolCall.id, content: JSON.stringify(wrapped) });
    }
  }

  if (toolResults.some((item) => item?.result?.ok)) {
    return synthesizeSellerSpriteAnswer(conversation, deepseekResponses, toolResults, firstRequest, "max tool rounds reached");
  }
  return { content: "本轮没有拿到可用的 SellerSprite 工具结果。请补充更明确的站点、ASIN、关键词或类目节点后重试。", request: firstRequest, raw: { deepseek: deepseekResponses, toolResults, toolBoundary: "max tool rounds reached" } };
}

async function handleAsk(req, res) {
  let session;
  let assistantMessage;
  try {
    const raw = await readBody(req);
    const payload = raw ? JSON.parse(raw) : {};
    const sessionId = getSessionIdFromPayload(payload);
    session = getSession(sessionId);
    const text = String(payload.message || "").trim();
    const attachments = normalizeAttachments(payload.attachments);
    if (!text && !attachments.length) {
      sendJson(res, 400, { ok: false, data: { code: "EMPTY_MESSAGE", message: "请输入消息。" } });
      return;
    }

    const userMessage = addMessage(session, createMessage("user", text, { attachments }));
    if (!session.title) session.title = summarizeSessionTitle(userMessage.content || userMessage.attachments?.[0]?.name || "Image");
    assistantMessage = addMessage(session, createMessage("assistant", `正在通过 DeepSeek 和 ${MCP_CHAT_LABEL} MCP 处理...`, { status: "pending" }));
    sendJson(res, 202, { ok: true, message: assistantMessage });

    try {
      const modelText = attachments.length
        ? `${text || "用户发送了图片。"}\n\n用户上传了图片。如需识别图片文字，请优先考虑调用 MCP OCR 或图片相关工具。`
        : text;
      const answer = await answerWithDeepSeek(session, modelText);
      updateMessage(session, assistantMessage.id, { content: answer.content, request: answer.request, raw: answer.raw, status: "done" });
    } catch (error) {
      updateMessage(session, assistantMessage.id, { content: error.message || "请求失败。", raw: { error: error.stack || String(error) }, status: "error" });
    }
  } catch (error) {
    if (session && assistantMessage) {
      updateMessage(session, assistantMessage.id, { content: error.message || "请求解析失败。", raw: { error: error.stack || String(error) }, status: "error" });
      return;
    }
    sendJson(res, 400, { ok: false, data: { code: "BAD_REQUEST", message: error.message || "请求解析失败。" } });
  }
}

function handleClearMessages(res, session) {
  if (!session) {
    sendJson(res, 200, { ok: true });
    return;
  }
  session.messages.splice(0);
  session.updatedAt = new Date().toISOString();
  broadcast(session, "clear", { ok: true });
  scheduleSessionSave();
  sendJson(res, 200, { ok: true });
}

function handleRecoverLast(res, session) {
  if (!session) {
    sendJson(res, 404, { ok: false, error: "Session not found" });
    return;
  }

  const source = latestRecoverableToolResult(session);
  if (!source) {
    sendJson(res, 404, { ok: false, error: "No recoverable tool result found" });
    return;
  }

  const message = addMessage(
    session,
    createMessage("assistant", buildRecoveredAnswer(source), {
      status: "done",
      request: source.request || null,
      raw: { recoveredFrom: source.id },
    })
  );
  sendJson(res, 200, { ok: true, message });
}

function handleDeleteSession(res, sessionId) {
  const session = findSession(sessionId);
  if (session) {
    broadcast(session, "deleted", { ok: true, sessionId });
    for (const client of session.sseClients) client.end();
    sessions.delete(normalizeSessionId(sessionId));
    scheduleSessionSave();
  }
  sendJson(res, 200, { ok: true });
}

async function handleMcp(req, res) {
  const raw = await readBody(req);
  const payload = raw ? JSON.parse(raw) : {};
  if (payload.method === "initialize") {
    await mcpClient.initialize();
    sendJson(res, 200, { jsonrpc: "2.0", id: payload.id ?? null, result: { serverInfo: { name: "sellersprite-mcp-bridge", version: "0.2.0" }, capabilities: { tools: {} } } });
    return;
  }
  if (payload.method === "tools/list") {
    sendJson(res, 200, { jsonrpc: "2.0", id: payload.id ?? null, result: { tools: await mcpClient.listTools() } });
    return;
  }
  if (payload.method === "tools/call") {
    const result = await callSellerSpriteToolCached(payload.params?.name, payload.params?.arguments || {});
    sendJson(res, 200, { jsonrpc: "2.0", id: payload.id ?? null, result });
    return;
  }
  sendJson(res, 200, { jsonrpc: "2.0", id: payload.id ?? null, error: { code: -32601, message: `Method not found: ${payload.method}` } });
}

function handleEvents(req, res, session) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "access-control-allow-origin": "*",
  });
  res.write(`event: ready\ndata: ${JSON.stringify({ ok: true })}\n\n`);
  if (!session) return;
  session.sseClients.add(res);
  req.on("close", () => session.sseClients.delete(res));
}

async function handleStatic(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const requested = url.pathname === "/" ? "/index.html" : url.pathname;
  const safePath = path.normalize(requested).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(PUBLIC_DIR, safePath);
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }
  try {
    const content = await fs.readFile(filePath);
    const headers = { "content-type": MIME_TYPES[path.extname(filePath)] || "application/octet-stream" };
    if (path.extname(filePath) === ".html") headers["cache-control"] = "no-store";
    res.writeHead(200, headers);
    res.end(content);
  } catch {
    res.writeHead(404);
    res.end("Not found");
  }
}

function lanUrls() {
  const urls = [];
  for (const entries of Object.values(os.networkInterfaces())) {
    for (const entry of entries || []) {
      if (entry.family === "IPv4" && !entry.internal) urls.push(`http://${entry.address}:${PORT}`);
    }
  }
  return urls;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (req.method === "POST" && url.pathname === "/api/ask") return handleAsk(req, res);
  if (req.method === "GET" && url.pathname === "/api/sessions") return sendJson(res, 200, { ok: true, sessions: listSessions() });
  if (req.method === "GET" && url.pathname === "/api/meta") return sendJson(res, 200, { ok: true, type: MCP_CHAT_TYPE, label: MCP_CHAT_LABEL, basePath: MCP_CHAT_BASE_PATH });
  const sessionId = getSessionIdFromUrl(url);
  const existingSession = findSession(sessionId);
  if (req.method === "GET" && url.pathname === "/api/messages") return sendJson(res, 200, getMessagesPage(existingSession, url));
  if (req.method === "GET" && url.pathname === "/api/guide") {
    return sendJson(res, 200, { ok: true, guide: createMessage("assistant", guideMessage(), { id: "guide", status: "done" }) });
  }
  if (req.method === "GET" && url.pathname === "/api/events") return handleEvents(req, res, getSession(sessionId));
  if (req.method === "POST" && url.pathname === "/api/messages/clear") return handleClearMessages(res, existingSession);
  if (req.method === "POST" && url.pathname === "/api/recover-last") return handleRecoverLast(res, existingSession);
  if (req.method === "DELETE" && url.pathname === "/api/session") return handleDeleteSession(res, sessionId);
  if (req.method === "POST" && url.pathname === "/mcp") {
    return handleMcp(req, res).catch((error) => sendJson(res, 500, { jsonrpc: "2.0", id: null, error: { code: -32603, message: error.message || "MCP server error" } }));
  }
  if (req.method === "GET") return handleStatic(req, res);
  res.writeHead(405);
  res.end("Method not allowed");
});

loadSessionsFromDisk().then(() => {
  installShutdownSave();
  server.listen(PORT, HOST, () => {
    console.log(`${MCP_CHAT_LABEL} MCP + DeepSeek chat running at http://localhost:${PORT}`);
    for (const url of lanUrls()) console.log(`LAN: ${url}`);
    console.log(`Session data: ${SESSIONS_FILE}`);
  });
});
