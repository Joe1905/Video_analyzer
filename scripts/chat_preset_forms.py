"""Data-driven composer forms for official chat workflows.

These definitions only describe the information collected by the shared chat UI.
The backend official-Skill and tool-whitelist boundaries remain authoritative.
"""

from typing import Any


def _field(
    name: str,
    label: str,
    placeholder: str,
    *,
    required: bool = False,
    value: str = "",
    multiline: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "placeholder": placeholder,
        "required": required,
        "value": value,
        "multiline": multiline,
        "full": full,
    }


def _form(label: str, prompt: str, fields: list[dict[str, Any]], intro: str = "填写后发送，系统会按当前官方流程执行。") -> dict[str, Any]:
    return {
        "label": label,
        "intro": intro,
        "prompt": prompt,
        "fields": [
            *fields,
            _field(
                "additional_notes",
                "补充说明",
                "可选：补充目标、限制条件或希望重点关注的内容",
                multiline=True,
                full=True,
            ),
        ],
    }


def _marketplace(value: str = "US") -> dict[str, Any]:
    return _field("marketplace", "亚马逊站点", "例如 US、UK、DE、JP", value=value)


def _asin(label: str = "ASIN", *, required: bool = True) -> dict[str, Any]:
    return _field("asin", label, "例如 B0XXXXXXXX", required=required)


def _keyword(label: str = "关键词 / 类目", *, required: bool = True) -> dict[str, Any]:
    return _field("keyword", label, "输入关键词、类目或类目节点", required=required)


SELLERSPRITE_PRESET_FORMS: dict[str, dict[str, Any]] = {
    "comprehensive/product-research": _form(
        "智能选品助手",
        "请使用卖家精灵官方 Skill「智能选品助手」完成选品研究。",
        [
            _marketplace(),
            _keyword("关键词 / 类目"),
            _field("price_range", "价格区间", "例如 20-50 美元"),
            _field("monthly_sales", "最低月销量", "例如 300"),
            _field("rating", "最低评分", "例如 4.2"),
            _field("seller_type", "卖家类型", "例如 FBA / FBM / 不限"),
        ],
    ),
    "comprehensive/market-analysis": _form(
        "市场全景分析",
        "请使用卖家精灵官方 Skill「市场全景分析」评估目标市场。",
        [_marketplace(), _keyword("关键词 / 类目 / 节点"), _field("month", "数据月份", "例如 202607；留空使用最新月份")],
    ),
    "comprehensive/competitor-analysis": _form(
        "竞品深度拆解",
        "请使用卖家精灵官方 Skill「竞品深度拆解」分析竞品。",
        [_marketplace(), _asin(), _field("focus", "重点关注", "例如流量结构、关键词、评论痛点", multiline=True, full=True)],
    ),
    "comprehensive/keyword-research": _form(
        "关键词选品研究",
        "请使用卖家精灵官方 Skill「关键词选品研究」完成关键词机会分析。",
        [
            _marketplace(),
            _keyword("核心关键词"),
            _field("min_search", "最低月搜索量", "例如 3000"),
            _field("max_products", "最高商品数", "例如 5000"),
        ],
    ),
    "comprehensive/listing-optimizer": _form(
        "Listing 优化诊断",
        "请使用卖家精灵官方 Skill「Listing 优化诊断」检查并优化 Listing。",
        [_marketplace(), _asin(), _field("competitor_asins", "对标 ASIN", "可输入多个，用逗号分隔"), _field("goal", "优化目标", "例如提升自然流量或转化率", multiline=True, full=True)],
    ),
    "comprehensive/traffic-analysis": _form(
        "流量结构分析",
        "请使用卖家精灵官方 Skill「流量结构分析」拆解商品流量。",
        [_marketplace(), _asin(), _field("comparison_asin", "对比 ASIN", "可选，用于横向比较")],
    ),
    "comprehensive/opportunity-finder": _form(
        "蓝海机会挖掘",
        "请使用卖家精灵官方 Skill「蓝海机会挖掘」寻找增长机会。",
        [
            _marketplace(),
            _keyword("关键词 / 类目"),
            _field("search_mode", "机会模式", "热销 / 异动 / 增长 / 飙升 / 潜力 / 长尾", value="增长"),
        ],
    ),
    "comprehensive/review-insights": _form(
        "买家评论洞察",
        "请使用卖家精灵官方 Skill「买家评论洞察」分析评论。",
        [_marketplace(), _field("asins", "目标 ASIN", "多个 ASIN 用逗号分隔", required=True), _field("competitor_asins", "竞品 ASIN", "可输入多个，用逗号分隔")],
    ),
    "comprehensive/pricing-strategy": _form(
        "定价策略分析",
        "请使用卖家精灵官方 Skill「定价策略分析」制定价格策略。",
        [_marketplace(), _field("target", "ASIN / 类目关键词", "输入目标 ASIN 或类目关键词", required=True), _field("cost", "产品成本", "例如 8 美元"), _field("margin_goal", "目标毛利率", "例如 40%")],
    ),
    "comprehensive/ad-optimizer": _form(
        "广告投放优化",
        "请使用卖家精灵官方 Skill「广告投放优化」制定 PPC 优化方案。",
        [_marketplace(), _asin(), _field("budget", "日预算范围", "例如 50-100 美元"), _field("goal", "投放目标", "例如控制 ACOS、拓展关键词", multiline=True, full=True)],
    ),
    "tactical/new-product-burst": _form(
        "新品快速爆发",
        "请按卖家精灵官方战术 Skill「新品快速爆发」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("available_month", "上架月数上限", "例如 2", value="2"), _field("min_units", "最低月销量", "例如 300", value="300"), _field("max_ratings", "最高评论数", "例如 100", value="100"), _field("min_rating", "最低评分", "例如 4.2", value="4.2")],
    ),
    "tactical/hidden-bestseller": _form(
        "隐形爆款",
        "请按卖家精灵官方战术 Skill「隐形爆款」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("available_month", "上架月数上限", "例如 3", value="3"), _field("min_units", "最低月销量", "例如 500", value="500"), _field("max_ratings", "最高评论数", "例如 50", value="50")],
    ),
    "tactical/aba-high-growth-trend": _form(
        "ABA 高增长趋势词",
        "请按卖家精灵官方战术 Skill「ABA 高增长趋势词」筛选趋势。",
        [_marketplace(), _keyword(required=False), _field("min_searches", "最低搜索量", "例如 3000", value="3000"), _field("min_growth", "最低近三月增长率", "例如 5%", value="5%"), _field("max_click_rate", "最高点击集中度", "例如 60%", value="60%")],
    ),
    "tactical/low-monopoly-keyword": _form(
        "流量分散关键词",
        "请按卖家精灵官方战术 Skill「流量分散关键词」筛选关键词。",
        [_marketplace(), _keyword("核心关键词"), _field("min_search", "最低月搜索量", "例如 5000", value="5000"), _field("max_click_rate", "最高点击集中度", "例如 50%", value="50%")],
    ),
    "tactical/title-density-gap": _form(
        "标题密度漏洞",
        "请按卖家精灵官方战术 Skill「标题密度漏洞」寻找长尾词。",
        [_marketplace(), _keyword("核心关键词"), _field("search_range", "月搜索量区间", "例如 1000-5000", value="1000-5000"), _field("max_title_density", "最高标题密度", "例如 5", value="5"), _field("min_purchase_rate", "最低购买率", "例如 5%", value="5%")],
    ),
    "tactical/hot-low-rating": _form(
        "热销低评分产品",
        "请按卖家精灵官方战术 Skill「热销低评分产品」寻找改良机会。",
        [_marketplace(), _keyword(required=False), _field("min_units", "最低月销量", "例如 1000", value="1000"), _field("max_rating", "最高评分", "例如 4.2", value="4.2"), _field("min_price", "最低价格", "例如 15 美元", value="15 美元")],
    ),
    "tactical/review-sentiment": _form(
        "评论语义分析",
        "请按卖家精灵官方战术 Skill「评论语义分析」聚类差评并提出改良方案。",
        [_marketplace(), _asin(), _field("stars", "评论星级", "例如 1,2,3", value="1,2,3"), _field("review_count", "分析评论数", "例如 50", value="50")],
    ),
    "tactical/low-brand-monopoly": _form(
        "低品牌垄断类目",
        "请按卖家精灵官方战术 Skill「低品牌垄断类目」筛选市场。",
        [_marketplace(), _keyword(required=False), _field("max_brand_share", "最高品牌集中度", "例如 45%", value="45%"), _field("max_amazon_share", "最高亚马逊自营占比", "例如 10%", value="10%"), _field("min_avg_units", "最低平均月销量", "例如 200", value="200"), _field("min_avg_price", "最低平均价格", "例如 15 美元", value="15 美元")],
    ),
    "tactical/high-new-product-ratio": _form(
        "高新品占比市场",
        "请按卖家精灵官方战术 Skill「高新品占比市场」筛选市场。",
        [_marketplace(), _keyword(required=False), _field("min_new_ratio", "最低新品占比", "例如 5%", value="5%"), _field("min_new_units", "新品最低平均月销量", "例如 100", value="100"), _field("new_product_months", "新品定义（月）", "例如 6", value="6")],
    ),
    "tactical/high-margin-lightweight": _form(
        "高毛利轻小品",
        "请按卖家精灵官方战术 Skill「高毛利轻小品」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("min_price", "最低价格", "例如 20 美元", value="20 美元"), _field("max_fba_fee", "最高 FBA 配送费", "例如 4 美元", value="4 美元"), _field("min_margin", "最低毛利率", "例如 50%", value="50%"), _field("min_units", "最低月销量", "例如 200", value="200")],
    ),
    "tactical/natural-traffic-audit": _form(
        "自然流量反查",
        "请按卖家精灵官方战术 Skill「自然流量反查」审计商品流量。",
        [_marketplace(), _asin(), _field("comparison_asin", "对比 ASIN", "可选")],
    ),
    "tactical/variant-gap-analysis": _form(
        "变体拆解模型",
        "请按卖家精灵官方战术 Skill「变体拆解模型」寻找变体缺口。",
        [_marketplace(), _asin(), _field("focus", "关注维度", "例如颜色、尺寸、套装或价格带", multiline=True, full=True)],
    ),
    "tactical/local-premium-disruption": _form(
        "本土溢价降维",
        "请按卖家精灵官方战术 Skill「本土溢价降维」寻找切入机会。",
        [_marketplace(), _keyword(required=False), _field("seller_nation", "卖家国家", "例如 US", value="US"), _field("min_price", "最低价格", "例如 35 美元", value="35 美元"), _field("min_units", "最低月销量", "例如 500", value="500")],
    ),
    "tactical/fbm-intercept": _form(
        "FBM 拦截",
        "请按卖家精灵官方战术 Skill「FBM 拦截」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("fulfillment", "配送方式", "FBM", value="FBM"), _field("min_units", "最低月销量", "例如 300", value="300"), _field("has_variants", "需要变体", "是 / 否", value="是")],
    ),
    "tactical/poor-listing-winner": _form(
        "低质量 Listing 高销量",
        "请按卖家精灵官方战术 Skill「低质量 Listing 高销量」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("max_lqs", "最高 Listing 质量分", "例如 60", value="60"), _field("min_units", "最低月销量", "例如 400", value="400"), _field("min_price", "最低价格", "例如 15 美元", value="15 美元")],
    ),
    "tactical/high-ticket-long-tail": _form(
        "高客单长尾",
        "请按卖家精灵官方战术 Skill「高客单长尾」筛选关键词。",
        [_marketplace(), _keyword("核心关键词"), _field("min_price", "最低价格", "例如 80 美元", value="80 美元"), _field("search_range", "月搜索量区间", "例如 500-4000", value="500-4000"), _field("max_ads", "最高广告商品数", "例如 10", value="10")],
    ),
    "tactical/seasonal-prepositioning": _form(
        "季节前置爆破",
        "请按卖家精灵官方战术 Skill「季节前置爆破」寻找季节性机会。",
        [_marketplace(), _keyword("核心关键词"), _field("history_month", "历史对比月份", "YYYYMM，例如 202509", required=True), _field("min_search", "最低月搜索量", "例如 500", value="500")],
    ),
}


CHUHAIJIANG_PRESET_FORMS: dict[str, dict[str, Any]] = {
    "chuhaijiang/product-selection": _form(
        "选品与市场调研",
        "请按出海匠官方 Skill 的「选品与市场调研」流程处理以下信息。",
        [
            _field("market", "目标市场", "国家或地区，例如 US / 美国", required=True),
            _field("category", "类目 / 关键词", "例如 家居收纳 / storage", required=True),
            _field("price_range", "目标价格带", "例如 20-40 美元"),
            _field("seller_type", "卖家 / 履约偏好", "例如本土店、跨境店、FBT"),
            _field("goal", "调研目标", "例如建立 20 个候选商品池并判断机会", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/profit-calculation": _form(
        "利润测算",
        "请按出海匠官方 Skill 的「利润测算」流程处理以下信息。",
        [
            _field("market", "目标市场", "国家或地区，例如 US / 美国", required=True),
            _field("product", "商品 / 商品 ID", "商品名称、链接或 ID", required=True),
            _field("selling_price", "预计售价", "填写币种，例如 29.99 USD", required=True),
            _field("purchase_cost", "采购成本", "填写币种，例如 45 CNY", required=True),
            _field("weight", "单件重量 / 体积", "例如 0.8 kg；体积 30×20×10 cm", required=True),
            _field("first_mile", "头程方式与费用", "例如海运 12 CNY/件"),
            _field("creator_commission", "达人佣金", "例如 15%"),
            _field("ad_cost", "广告成本", "例如目标投产比 2.5 或 5 USD/单"),
        ],
        "至少填写售价、采购成本和重量；信息越完整，测算越接近实际。",
    ),
    "chuhaijiang/creator-outreach": _form(
        "达人筛选与建联",
        "请按出海匠官方 Skill 的「达人筛选与建联」流程处理以下信息。",
        [
            _field("market", "目标市场", "国家或地区，例如 US / 美国", required=True),
            _field("product", "商品 / 类目", "商品名称、链接或类目", required=True),
            _field("budget", "合作预算 / 佣金", "例如 1000 USD；佣金 15%"),
            _field("creator_profile", "达人画像", "例如女性、10万粉内、近30天有带货", multiline=True, full=True),
            _field("contact_requirement", "建联要求", "例如必须有邮箱；先出名单，不直接发送", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/competitor-analysis": _form(
        "竞品、店铺与广告分析",
        "请按出海匠官方 Skill 的「竞品、店铺与广告分析」流程处理以下信息。",
        [
            _field("market", "目标市场", "国家或地区，例如 US / 美国", required=True),
            _field("target", "分析对象", "商品、店铺或广告链接 / ID / 名称", required=True),
            _field("object_type", "对象类型", "商品 / 店铺 / 广告", required=True),
            _field("focus", "重点关注", "例如销量趋势、内容打法、价格与差异化机会", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/content-generation": _form(
        "AI 内容生成",
        "请按出海匠官方 Skill 的「AI 内容生成」流程先产出可审阅方案。",
        [
            _field("content_type", "内容类型", "例如商品标题、详情页、短视频脚本", required=True),
            _field("product", "商品 / 品牌", "商品信息、卖点或品牌名称", required=True),
            _field("market_platform", "目标市场 / 平台", "例如美国 TikTok Shop", required=True),
            _field("goal", "内容目标", "例如提升点击、转化或品牌认知", required=True),
            _field("assets", "参考素材 / 链接", "已有图片、视频、文案或参考链接", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/canvas-creation": _form(
        "AI 画布创作",
        "请按出海匠官方 Skill 的「AI 画布创作」流程先规划画布，不直接生成或发布。",
        [
            _field("usage", "画布用途", "例如商品详情图、广告图、社媒帖子", required=True),
            _field("market", "目标市场 / 受众", "例如美国 25-35 岁女性", required=True),
            _field("message", "核心卖点 / CTA", "希望突出的卖点、标题与行动号召", required=True),
            _field("assets", "已有素材", "图片、Logo、品牌色或参考链接", multiline=True, full=True),
            _field("size", "尺寸 / 版式", "例如 1080×1350、1:1"),
        ],
    ),
    "chuhaijiang/video-editing": _form(
        "视频剪辑",
        "请按出海匠官方 Skill 的「视频剪辑」流程先生成剪辑方案。",
        [
            _field("material", "已有素材", "素材名称、链接或内容说明", required=True),
            _field("platform", "目标平台 / 比例", "例如 TikTok 9:16", required=True),
            _field("duration", "目标时长", "例如 20 秒", required=True),
            _field("goal", "剪辑目标", "例如突出前三秒钩子和使用效果", required=True),
            _field("requirements", "剪辑要求", "字幕、音乐、节奏、转场与禁用内容", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/social-operation": _form(
        "社媒运营",
        "请按出海匠官方 Skill 的「社媒运营」流程处理；默认只查看，不执行发布、回复或私信。",
        [
            _field("account", "平台 / 已绑定账号", "例如 TikTok @brand", required=True),
            _field("task", "想查看或规划的任务", "例如查看近 7 天数据、评论或待办", required=True),
            _field("time_range", "时间范围", "例如近 7 天"),
            _field("content_target", "内容 / 对象", "指定帖子、评论、达人或主题"),
            _field("action_boundary", "操作边界", "默认：只查看，不发布、不回复、不私信", value="只查看，不发布、不回复、不私信", multiline=True, full=True),
        ],
    ),
}


CHAT_PRESET_FORMS: dict[str, dict[str, Any]] = {
    **SELLERSPRITE_PRESET_FORMS,
    **CHUHAIJIANG_PRESET_FORMS,
}


def preset_forms_for_provider(provider: str) -> dict[str, dict[str, Any]]:
    if provider == "amazon":
        return SELLERSPRITE_PRESET_FORMS
    if provider == "chuhaijiang":
        return CHUHAIJIANG_PRESET_FORMS
    return {}
