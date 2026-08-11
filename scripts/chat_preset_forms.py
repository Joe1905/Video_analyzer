"""Data-driven composer forms for official chat workflows.

These definitions only describe the information collected by the shared chat UI.
The backend official-Skill and tool-whitelist boundaries remain authoritative.
"""

from typing import Any


SELLERSPRITE_MARKETPLACE_OPTIONS = (
    {"value": "US", "label": "美国"},
    {"value": "UK", "label": "英国"},
    {"value": "DE", "label": "德国"},
    {"value": "FR", "label": "法国"},
    {"value": "IT", "label": "意大利"},
    {"value": "ES", "label": "西班牙"},
    {"value": "JP", "label": "日本"},
    {"value": "CA", "label": "加拿大"},
    {"value": "MX", "label": "墨西哥"},
    {"value": "AU", "label": "澳大利亚"},
)

CHUHAIJIANG_COUNTRY_OPTIONS = (
    {"value": "US", "label": "美国"},
    {"value": "GB", "label": "英国"},
    {"value": "DE", "label": "德国"},
    {"value": "FR", "label": "法国"},
    {"value": "IT", "label": "意大利"},
    {"value": "ES", "label": "西班牙"},
    {"value": "JP", "label": "日本"},
    {"value": "CA", "label": "加拿大"},
    {"value": "MX", "label": "墨西哥"},
    {"value": "BR", "label": "巴西"},
    {"value": "TH", "label": "泰国"},
    {"value": "ID", "label": "印度尼西亚"},
    {"value": "VN", "label": "越南"},
    {"value": "MY", "label": "马来西亚"},
    {"value": "PH", "label": "菲律宾"},
    {"value": "SG", "label": "新加坡"},
)

SOCIAL_PLATFORM_OPTIONS = (
    {"value": "tiktok", "label": "TikTok"},
    {"value": "instagram", "label": "Instagram"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "facebook", "label": "Facebook"},
)


def _field(
    name: str,
    label: str,
    placeholder: str,
    *,
    required: bool = False,
    value: str = "",
    multiline: bool = False,
    full: bool = False,
    parameter: str = "",
    options: tuple[dict[str, str], ...] = (),
    empty_meaning: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "placeholder": placeholder,
        "required": required,
        "value": value,
        "multiline": multiline,
        "full": full,
        "parameter": parameter,
        "options": [dict(option) for option in options],
        "empty_meaning": empty_meaning or "用户未指定，表示无额外限制；不要臆造，按官方 Skill 默认值或完整范围处理。",
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
                empty_meaning="用户没有额外补充，以其他表单项表达的意图为准。",
            ),
        ],
    }


def _marketplace(value: str = "US") -> dict[str, Any]:
    return _field(
        "marketplace",
        "亚马逊站点",
        "选择亚马逊站点",
        value=value,
        parameter="marketplace",
        options=SELLERSPRITE_MARKETPLACE_OPTIONS,
    )


def _target_market(value: str = "US") -> dict[str, Any]:
    return _field(
        "market",
        "目标市场",
        "选择国家或地区",
        required=True,
        value=value,
        parameter="country",
        options=CHUHAIJIANG_COUNTRY_OPTIONS,
    )


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
            _field(
                "seller_type",
                "配送方式",
                "选择配送方式",
                value="ANY",
                parameter="fulfillment",
                options=(
                    {"value": "ANY", "label": "不限"},
                    {"value": "FBA", "label": "亚马逊配送（FBA）"},
                    {"value": "FBM", "label": "卖家自配送（FBM）"},
                ),
            ),
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
            _field(
                "search_mode",
                "机会模式",
                "选择机会类型",
                value="growth",
                parameter="search_mode",
                options=(
                    {"value": "hot", "label": "热销"},
                    {"value": "anomaly", "label": "异动"},
                    {"value": "growth", "label": "持续增长"},
                    {"value": "surge", "label": "快速飙升"},
                    {"value": "potential", "label": "潜力机会"},
                    {"value": "long_tail", "label": "长尾机会"},
                ),
            ),
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
        [_marketplace(), _keyword(required=False), _field("seller_nation", "卖家国家", "选择卖家所在国家", value="US", parameter="sellerNation", options=CHUHAIJIANG_COUNTRY_OPTIONS), _field("min_price", "最低价格", "例如 35 美元", value="35 美元"), _field("min_units", "最低月销量", "例如 500", value="500")],
    ),
    "tactical/fbm-intercept": _form(
        "FBM 拦截",
        "请按卖家精灵官方战术 Skill「FBM 拦截」筛选机会。",
        [_marketplace(), _keyword(required=False), _field("fulfillment", "配送方式", "选择配送方式", value="FBM", parameter="fulfillment", options=({"value": "FBM", "label": "卖家自配送（FBM）"}, {"value": "FBA", "label": "亚马逊配送（FBA）"})), _field("min_units", "最低月销量", "例如 300", value="300"), _field("has_variants", "需要变体", "选择是否需要变体", value="Y", parameter="variation", options=({"value": "Y", "label": "是"}, {"value": "N", "label": "否"}))],
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
            _target_market(),
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
            _target_market(),
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
            _target_market(),
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
            _target_market(),
            _field("target", "分析对象", "商品、店铺或广告链接 / ID / 名称", required=True),
            _field(
                "object_type",
                "对象类型",
                "选择分析对象类型",
                required=True,
                value="products",
                parameter="entity",
                options=(
                    {"value": "products", "label": "商品"},
                    {"value": "sellers", "label": "店铺"},
                    {"value": "ads", "label": "广告"},
                ),
            ),
            _field("focus", "重点关注", "例如销量趋势、内容打法、价格与差异化机会", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/content-generation": _form(
        "AI 内容生成",
        "请按出海匠官方 Skill 的「AI 内容生成」流程先产出可审阅方案。",
        [
            _field("content_type", "内容类型", "例如商品标题、详情页、短视频脚本", required=True),
            _field("product", "商品 / 品牌", "商品信息、卖点或品牌名称", required=True),
            _target_market(),
            _field("platform", "目标平台", "选择目标平台", required=True, value="tiktok", parameter="platform", options=SOCIAL_PLATFORM_OPTIONS),
            _field("goal", "内容目标", "例如提升点击、转化或品牌认知", required=True),
            _field("assets", "参考素材 / 链接", "已有图片、视频、文案或参考链接", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/canvas-creation": _form(
        "AI 画布创作",
        "请按出海匠官方 Skill 的「AI 画布创作」流程先规划画布，不直接生成或发布。",
        [
            _field("usage", "画布用途", "例如商品详情图、广告图、社媒帖子", required=True),
            _target_market(),
            _field("audience", "目标受众", "例如 25-35 岁女性、户外爱好者", required=True),
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
            _field("platform", "目标平台", "选择目标平台", required=True, value="tiktok", parameter="platform", options=SOCIAL_PLATFORM_OPTIONS),
            _field("aspect_ratio", "画面比例", "选择输出画面比例", required=True, value="9:16", parameter="aspect_ratio", options=({"value": "9:16", "label": "竖屏 9:16"}, {"value": "1:1", "label": "方形 1:1"}, {"value": "16:9", "label": "横屏 16:9"}, {"value": "4:5", "label": "竖版 4:5"})),
            _field("duration", "目标时长", "例如 20 秒", required=True),
            _field("goal", "剪辑目标", "例如突出前三秒钩子和使用效果", required=True),
            _field("requirements", "剪辑要求", "字幕、音乐、节奏、转场与禁用内容", multiline=True, full=True),
        ],
    ),
    "chuhaijiang/social-operation": _form(
        "社媒运营",
        "请按出海匠官方 Skill 的「社媒运营」流程处理；默认只查看，不执行发布、回复或私信。",
        [
            _field("platform", "社媒平台", "选择社媒平台", required=True, value="tiktok", parameter="platform", options=SOCIAL_PLATFORM_OPTIONS),
            _field("account", "已绑定账号", "输入账号名称，例如 @brand", required=True),
            _field("task", "运营任务", "选择需要查看或规划的任务", required=True, value="analytics", parameter="action", options=({"value": "analytics", "label": "查看运营数据"}, {"value": "comments", "label": "查看评论"}, {"value": "content_plan", "label": "规划内容"}, {"value": "todo", "label": "整理运营待办"})),
            _field("time_range", "时间范围", "例如近 7 天"),
            _field("content_target", "内容 / 对象", "指定帖子、评论、达人或主题"),
            _field("action_boundary", "操作边界", "默认：只查看，不发布、不回复、不私信", value="只查看，不发布、不回复、不私信", multiline=True, full=True),
        ],
    ),
}


HOME_PRESET_FORMS: dict[str, dict[str, Any]] = {
    "home/video-analysis": _form(
        "跨平台短视频深度分析",
        "请按跨平台短视频深度分析工作流处理：先识别视频平台并取得内容、转录与评论；仅在本地分析支持时下载并补充画面和音频证据。",
        [
            _field("video_url", "视频链接", "粘贴 TikTok、Instagram、YouTube、Facebook 或 X 公共视频链接", required=True, full=True),
            _field("focus", "分析重点", "例如前三秒钩子、叙事结构、评论反馈、转化线索或脚本结构", multiline=True, full=True),
        ],
        "只暴露视频详情、转录、评论和本地分析相关工具；不会开放无关的平台能力。",
    ),
    "home/tiktok-trends": _form(
        "跨平台热点与选题",
        "请按跨平台热点与选题工作流处理，从短视频、社区、搜索和视觉平台交叉验证趋势并形成选题建议。",
        [
            _field("market", "目标市场", "例如 US、GB、JP；留空时说明数据地域限制", parameter="region"),
            _field("topic", "行业 / 主题", "例如美妆、宠物、露营；留空则做综合趋势扫描", required=True),
            _field("focus", "选题目标", "例如寻找增长话题、热门音乐、视觉趋势或差异化切口", multiline=True, full=True),
        ],
        "限定调用趋势、搜索、Shorts/Reels 与视觉发现相关工具。",
    ),
    "home/shop-research": _form(
        "商品与商业机会",
        "请按商品与商业机会工作流处理，联查 TikTok Shop、Facebook Marketplace、Amazon 和公开搜索数据。",
        [
            _field("query", "商品 / 类目 / 链接", "输入商品关键词、类目、商品链接、ASIN 或商品 ID", required=True, full=True),
            _field("market", "目标市场", "例如 US、GB、DE", parameter="region"),
            _field("focus", "关注维度", "例如价格带、评论痛点、供需、竞品或内容机会", multiline=True, full=True),
        ],
        "只开放商城、Marketplace、Amazon 抓取与网页核验工具。",
    ),
    "home/creator-competitor": _form(
        "达人与账号对标",
        "请按达人与账号对标工作流处理，跨平台比较账号、受众、作品结构与内容打法。",
        [
            _field("targets", "达人 / 品牌账号", "输入一个或多个 @账号、主页链接或搜索关键词，并注明平台", required=True, full=True),
            _field("focus", "对标维度", "例如受众画像、内容结构、更新频率、爆款主题或账号差异", multiline=True, full=True),
        ],
        "限定调用账号、受众和账号内容列表工具，不开放评论、广告或商城工具。",
    ),
    "home/cross-platform-research": _form(
        "品牌与竞品舆情",
        "请按品牌与竞品舆情工作流处理，从搜索、社媒和社区信号交叉核验品牌动态与公众反馈。",
        [
            _field("targets", "品牌 / 竞品", "输入品牌、产品、竞品名称或公开主页", required=True, full=True),
            _field("market", "市场 / 语言", "例如美国英语、英国、东南亚"),
            _field("focus", "研究目标", "例如声量来源、争议、增长信号、竞品动态或风险", multiline=True, full=True),
        ],
        "限定调用品牌发现、公开内容与社区检索工具。",
    ),
    "home/comment-demand-insights": _form(
        "评论与用户需求洞察",
        "请按评论与用户需求洞察工作流处理，跨平台汇总评论、回复及传播互动并提炼需求。",
        [
            _field("targets", "内容链接 / 关键词", "输入一个或多个视频、帖子链接或产品关键词", required=True, full=True),
            _field("focus", "洞察目标", "例如用户痛点、购买阻力、满意点、常见问题或功能诉求", multiline=True, full=True),
        ],
        "限定调用内容详情、评论、回复和必要的搜索工具。",
    ),
    "home/ad-creative-research": _form(
        "广告素材与投放研究",
        "请按广告素材与投放研究工作流处理，比较 TikTok、Meta、Google 与 LinkedIn 广告库中的公开投放。",
        [
            _field("targets", "品牌 / 广告主 / 关键词", "输入品牌、广告主、竞品或素材关键词", required=True, full=True),
            _field("market", "目标市场", "例如 US、GB、DE", parameter="region"),
            _field("focus", "研究目标", "例如活跃广告、创意卖点、CTA、素材形式或投放周期", multiline=True, full=True),
        ],
        "只开放四类广告库工具，不开放平台账号或普通内容工具。",
    ),
    "home/community-listening": _form(
        "社区口碑与话题追踪",
        "请按社区口碑与话题追踪工作流处理，聚合 Reddit、Threads、X 社群和 Facebook 群组讨论。",
        [
            _field("topic", "品牌 / 产品 / 话题", "输入需要追踪的品牌、产品、事件或关键词", required=True, full=True),
            _field("communities", "指定社区", "可选：Subreddit、X Community、Threads 账号或 Facebook 群组", full=True),
            _field("focus", "研究目标", "例如口碑、争议、真实体验、观点分歧或趋势变化", multiline=True, full=True),
        ],
        "限定调用社区、群组、帖子与评论工具。",
    ),
    "home/live-content-monitor": _form(
        "直播与短内容监测",
        "请按直播与短内容监测工作流处理，联查 TikTok、YouTube、Twitch 与 Instagram 的直播和短内容。",
        [
            _field("targets", "账号 / 频道 / 主题", "输入主播、频道、账号、主页链接或主题关键词", required=True, full=True),
            _field("focus", "监测目标", "例如直播状态、开播排期、Shorts/Reels 更新或精彩片段", multiline=True, full=True),
        ],
        "限定调用直播、排期、短内容和账号识别工具。",
    ),
    "home/visual-inspiration": _form(
        "视觉灵感与创意趋势",
        "请按视觉灵感与创意趋势工作流处理，从音乐、Reels、Shorts、Pins 与看板提炼创意方向。",
        [
            _field("topic", "品类 / 视觉主题", "例如春季露营、极简家居、宠物用品", required=True, full=True),
            _field("focus", "创意目标", "例如视觉风格、音乐、构图、短视频形式、看板或素材方向", multiline=True, full=True),
        ],
        "限定调用视觉发现、热门音乐、Reels、Shorts 与 Pinterest 工具。",
    ),
    "home/web-verification": _form(
        "联网资料验证",
        "请按联网资料验证工作流处理，只检索公开网页并标注来源与检索时间。",
        [
            _field("query", "检索问题", "输入需要核验的品牌、产品、趋势或公开资料问题", required=True, full=True),
        ],
        "此工作流只调用联网检索外挂，不调用社媒 MCP 或下载功能。",
    ),
    "home/sociavault-credits": _form(
        "SociaVault 额度查询",
        "请查询当前 SociaVault API 的可用额度，并简洁说明结果。",
        [],
        "此工作流只调用 SociaVault 额度查询 MCP 工具。",
    ),
}


CHAT_PRESET_FORMS: dict[str, dict[str, Any]] = {
    **HOME_PRESET_FORMS,
    **SELLERSPRITE_PRESET_FORMS,
    **CHUHAIJIANG_PRESET_FORMS,
}


def preset_forms_for_provider(provider: str) -> dict[str, dict[str, Any]]:
    if provider == "home":
        return HOME_PRESET_FORMS
    if provider == "amazon":
        return SELLERSPRITE_PRESET_FORMS
    if provider == "chuhaijiang":
        return CHUHAIJIANG_PRESET_FORMS
    return {}
