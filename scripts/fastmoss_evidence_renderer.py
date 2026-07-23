#!/usr/bin/env python3
"""Deterministic semantic rendering for FastMoss MCP evidence.

The report model benefits from business-shaped Markdown, but the converter must
not become a second analyst.  This module only classifies response shapes,
preserves provenance, and renders observed values.  It never calculates metrics
or infers business meaning beyond field names supplied by FastMoss.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from json_to_markdown import json_to_markdown


PROFILE_REFERENCE = "reference"
PROFILE_RECORDS = "records"
PROFILE_ENTITY = "entity"
PROFILE_TREND = "trend"
PROFILE_DISTRIBUTION = "distribution"
PROFILE_RELATIONSHIP = "relationship"
PROFILE_NARRATIVE = "narrative"
PROFILE_GENERIC = "generic"


FASTMOSS_TOOL_PROFILE_GROUPS: dict[str, frozenset[str]] = {
    PROFILE_REFERENCE: frozenset({
        "credit_usage_summary",
        "fastmoss_detail_url_examples",
        "product_category_info",
        "search_category_by_words",
        "search_fastmoss_documents",
    }),
    PROFILE_RECORDS: frozenset({
        "ad_search",
        "agency_product_list",
        "agency_rank_top",
        "agency_search",
        "agency_shop_analysis",
        "creator_product_list",
        "creator_rank_top_ecommerce",
        "creator_rank_top_growth",
        "creator_rank_top_potential",
        "creator_search",
        "live_products_list",
        "live_search",
        "market_category_ranking",
        "product_rank_new_listed",
        "product_rank_top_selling",
        "product_review_list",
        "product_search",
        "product_video_list",
        "shop_rank_top_selling",
        "shop_search",
        "video_search",
    }),
    PROFILE_ENTITY: frozenset({
        "agency_profile_overview",
        "creator_profile_overview",
        "live_detail_analysis",
        "product_detail_info",
        "shop_base_info",
        "video_detail_analysis",
    }),
    PROFILE_TREND: frozenset({
        "ad_data_overview",
        "creator_data_trends",
        "product_investment",
        "product_sales_trend",
        "shop_data_trends",
        "shop_investment_analysis",
        "video_data_trends",
    }),
    PROFILE_DISTRIBUTION: frozenset({
        "agency_product_analysis",
        "creator_cargo_summary",
        "creator_fans_distribution",
        "market_category_analysis",
        "market_category_author_sales_matrix",
    }),
    PROFILE_RELATIONSHIP: frozenset({
        "agency_creator_analysis",
        "creator_video_analysis",
        "product_creator_analysis",
        "product_overview",
        "product_sku",
        "shop_creator_analysis",
        "shop_live_analysis",
        "shop_product_analysis",
        "shop_sale_analysis",
        "shop_video_analysis",
    }),
    PROFILE_NARRATIVE: frozenset({"video_script_info"}),
}

FASTMOSS_CURRENT_TOOL_NAMES = frozenset().union(*FASTMOSS_TOOL_PROFILE_GROUPS.values())


@dataclass(frozen=True)
class ToolRenderSpec:
    tool_name: str
    profile: str
    entity_type: str
    evidence_title: str = "业务证据"
    contract_source: str = "mcp_runtime"
    report_included: bool = True


FASTMOSS_TOOL_TITLES: dict[str, str] = {
    "ad_data_overview": "广告投放概览", "ad_search": "广告素材样本",
    "agency_creator_analysis": "机构合作达人分析", "agency_product_analysis": "机构商品结构分析",
    "agency_product_list": "机构带货商品样本", "agency_profile_overview": "机构概览",
    "agency_rank_top": "机构榜单", "agency_search": "机构搜索结果", "agency_shop_analysis": "机构合作店铺分析",
    "creator_cargo_summary": "达人带货概览", "creator_data_trends": "达人数据趋势",
    "creator_fans_distribution": "达人粉丝分布", "creator_product_list": "达人带货商品样本",
    "creator_profile_overview": "达人概览", "creator_rank_top_ecommerce": "带货达人榜单",
    "creator_rank_top_growth": "增长达人榜单", "creator_rank_top_potential": "潜力达人榜单",
    "creator_search": "达人搜索结果", "creator_video_analysis": "达人视频分析",
    "credit_usage_summary": "接口额度使用概览", "fastmoss_detail_url_examples": "详情链接格式说明",
    "live_detail_analysis": "直播详情分析", "live_products_list": "直播带货商品样本", "live_search": "直播搜索结果",
    "market_category_analysis": "市场类目分析", "market_category_author_sales_matrix": "类目达人销售矩阵",
    "market_category_ranking": "市场类目排名", "product_category_info": "商品类目信息",
    "product_creator_analysis": "商品关联达人分析", "product_detail_info": "商品详情",
    "product_investment": "商品广告投放分析", "product_overview": "商品经营概览",
    "product_rank_new_listed": "近期上架商品榜", "product_rank_top_selling": "热销商品榜",
    "product_review_list": "商品评论样本", "product_sales_trend": "商品销售趋势",
    "product_search": "商品搜索样本", "product_sku": "商品SKU分析", "product_video_list": "商品关联视频样本",
    "search_category_by_words": "关键词匹配类目", "search_fastmoss_documents": "FastMoss知识文档",
    "shop_base_info": "店铺概览", "shop_creator_analysis": "店铺合作达人分析",
    "shop_data_trends": "店铺经营趋势", "shop_investment_analysis": "店铺广告投放分析",
    "shop_live_analysis": "店铺直播分析", "shop_product_analysis": "店铺商品结构分析",
    "shop_rank_top_selling": "热销店铺榜", "shop_sale_analysis": "店铺销售渠道分析",
    "shop_search": "店铺搜索结果", "shop_video_analysis": "店铺视频分析",
    "video_data_trends": "视频数据趋势", "video_detail_analysis": "视频详情分析",
    "video_script_info": "视频文案与字幕", "video_search": "视频搜索结果",
}

FASTMOSS_PUBLIC_API_TOOLS = frozenset({
    "product_search", "product_rank_new_listed", "product_rank_top_selling",
})
FASTMOSS_AUDIT_ONLY_TOOLS = frozenset({"credit_usage_summary"})


def _entity_type_for_tool(tool_name: str) -> str:
    if tool_name.startswith("agency_"):
        return "agency"
    if tool_name.startswith("creator_"):
        return "creator"
    if tool_name.startswith("product_"):
        return "product"
    if tool_name.startswith("shop_"):
        return "shop"
    if tool_name.startswith("video_") or tool_name.startswith("ad_"):
        return "video"
    if tool_name.startswith("live_"):
        return "live"
    if "category" in tool_name:
        return "category"
    return "reference"


FASTMOSS_RENDER_SPECS: dict[str, ToolRenderSpec] = {
    name: ToolRenderSpec(
        name,
        profile,
        _entity_type_for_tool(name),
        FASTMOSS_TOOL_TITLES[name],
        "official_api" if name in FASTMOSS_PUBLIC_API_TOOLS else "mcp_runtime",
        name not in FASTMOSS_AUDIT_ONLY_TOOLS,
    )
    for profile, names in FASTMOSS_TOOL_PROFILE_GROUPS.items()
    for name in names
}


@dataclass(frozen=True)
class EvidenceNode:
    kind: str
    title: str
    path: str


@dataclass
class RenderedToolEvidence:
    markdown: str
    tool_name: str
    profile: str
    node_types: list[str] = field(default_factory=list)
    business_leaf_paths: set[str] = field(default_factory=set)
    consumed_paths: set[str] = field(default_factory=set)
    unmapped_paths: set[str] = field(default_factory=set)
    excluded_paths: set[str] = field(default_factory=set)
    exclusion_reasons: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    fallback: bool = False
    empty: bool = False


@dataclass
class RenderedEvidenceDocument:
    markdown: str
    tool_results: list[RenderedToolEvidence]

    @property
    def stats(self) -> dict[str, Any]:
        node_counts: dict[str, int] = {}
        for result in self.tool_results:
            for kind in result.node_types:
                node_counts[kind] = node_counts.get(kind, 0) + 1
        return {
            "tool_count": len(self.tool_results),
            "registered_tool_count": sum(
                1 for result in self.tool_results if result.tool_name in FASTMOSS_RENDER_SPECS
            ),
            "fallback_tools": [result.tool_name for result in self.tool_results if result.fallback],
            "empty_result_count": sum(1 for result in self.tool_results if result.empty),
            "business_leaf_count": sum(len(result.business_leaf_paths) for result in self.tool_results),
            "consumed_leaf_count": sum(len(result.consumed_paths) for result in self.tool_results),
            "unmapped_leaf_count": sum(len(result.unmapped_paths) for result in self.tool_results),
            "excluded_leaf_count": sum(len(result.excluded_paths) for result in self.tool_results),
            "audit_only_leaf_count": sum(len(result.exclusion_reasons) for result in self.tool_results),
            "diagnostics": [
                diagnostic
                for result in self.tool_results
                for diagnostic in result.diagnostics
            ],
            "node_counts": node_counts,
            "markdown_chars": len(self.markdown),
        }


_SIMPLE_PATH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIME_KEYS = {
    "date", "day", "datetime", "date_value", "start_time", "end_time",
    "snapshot_date", "publish_time", "create_time", "time",
}
_DISTRIBUTION_HINTS = (
    "distribution", "matrix", "price_band", "price_range", "follower_tier",
    "category_breakdown", "category_summary", "channel_distribution",
    "content_distribution", "sales_channel", "content_type",
)
_ENTITY_CONTAINER_KEYS = {
    "product", "shop", "creator", "video", "live", "agency", "category",
    "asin", "listing", "item", "keyword",
    "shop_info", "creator_info", "product_info", "live_info", "video_info",
}
_NARRATIVE_KEYS = {
    "text", "content", "snippet", "summary", "description", "desc",
    "video_desc", "subtitle", "subtitles", "script", "document", "documents",
}
_FIELD_LABELS = {
    "id": "ID",
    "code": "编码",
    "label": "名称",
    "asin": "ASIN",
    "parent_asin": "父ASIN",
    "product_id": "商品ID",
    "seller_id": "店铺ID",
    "shop_id": "店铺ID",
    "creator_uid": "达人UID",
    "uid": "达人UID",
    "video_id": "视频ID",
    "room_id": "直播间ID",
    "agency_id": "机构ID",
    "category_id": "类目ID",
    "title": "标题",
    "name": "名称",
    "nickname": "达人昵称",
    "region": "地区",
    "currency": "币种",
    "currency_code": "币种",
    "price": "价格",
    "current_price": "当前价格",
    "gmv": "GMV",
    "units_sold": "销量",
    "sales": "销量",
    "follower_count": "粉丝数",
    "creator_count": "达人数",
    "video_count": "视频数",
    "live_count": "直播场次",
    "play_count": "播放量",
    "view_count": "观看量",
    "digg_count": "点赞数",
    "like_count": "点赞数",
    "comment_count": "评论数",
    "share_count": "分享数",
    "commission_rate_percent": "佣金率",
    "gmv_share_percent": "GMV占比",
    "units_sold_share_percent": "销量占比",
    "date": "日期",
    "date_type": "统计周期类型",
    "date_value": "统计日期",
    "listing_start_date": "上架日期范围起点",
    "listing_end_date": "上架日期范围终点",
    "time_range_days": "统计天数",
    "rank": "排名",
    "score": "匹配分",
    "total": "报告总量",
    "is_fully_managed": "是否全托管",
    "is_cross_border": "是否跨境",
    "is_free_shipping": "是否包邮",
    "is_off_shelf": "是否下架",
    "average_order_value": "客单价",
    "metric_window_days": "指标窗口天数",
    "published_at": "发布时间",
    "matched_query": "命中查询词",
    "traffic_source": "流量类型",
    "traffic_type": "流量归因类型",
    "sales_channel": "成交渠道",
    "content_type": "内容类型",
    "page": "页码",
    "pagesize": "每页数量",
    "size": "返回条数",
    "pages": "总页数",
    "top_k": "候选数量",
    "query": "查询词",
    "keywords": "关键词",
    "keyword": "关键词",
    "return_fields": "返回字段",
    "keyword_cn": "关键词中文释义",
    "keyword_jp": "关键词日文释义",
    "departments": "关联类目",
    "search_rank": "搜索排名",
    "search_rank_cv": "搜索排名变化值",
    "search_rank_cr": "搜索排名变化率",
    "search_rank_growth_value": "搜索排名增长值",
    "search_rank_growth_rate": "搜索排名增长率",
    "w1_search_rank": "前1个统计周期的搜索排名",
    "w1_rank_growth_value": "相对前1个统计周期的排名变化值",
    "w1_rank_growth_rate": "相对前1个统计周期的排名变化率",
    "w4_search_rank": "前4个统计周期的搜索排名",
    "w4_rank_growth_value": "相对前4个统计周期的排名变化值",
    "w4_rank_growth_rate": "相对前4个统计周期的排名变化率",
    "w12_search_rank": "前12个统计周期的搜索排名",
    "w12_rank_growth_value": "相对前12个统计周期的排名变化值",
    "w12_rank_growth_rate": "相对前12个统计周期的排名变化率",
    "searches": "搜索量",
    "searches_growth": "搜索量增长率",
    "search_month_cr": "月搜索量同比增长率（旧字段）",
    "search_monthly_cr": "月搜索量同比增长率",
    "search_nearly_cr": "近3个月搜索量增长率",
    "purchases": "购买量",
    "purchase_rate": "购买率",
    "clicks": "点击量",
    "impressions": "曝光量",
    "click_rate": "点击率",
    "click_share_rate": "点击量前三 ASIN 的总占比",
    "cvs_share_rate": "转化量前三 ASIN 的总占比",
    "conversion_rate": "转化率",
    "products": "商品数",
    "ad_products": "广告商品数",
    "supply_demand_ratio": "供需比",
    "monopoly_click_rate": "点击集中度",
    "ara_click_rate": "点击垄断率",
    "ara_share_rate": "共享转化率",
    "click_share": "前三名点击占比",
    "conversion_share": "前三名转化占比",
    "title_density": "标题密度",
    "title_density_exact": "首页标题精确包含该词的商品数",
    "cpr_exact": "8天内使该词上首页所需销量（精确匹配）",
    "spr": "使该词上首页所需销量（SPR）",
    "relevancy": "相关度",
    "word_count": "词数",
    "avg_price": "平均价格",
    "avg_bid": "平均PPC竞价（旧字段）",
    "avg_rating": "平均评分",
    "avg_ratings": "平均评分数",
    "min_bid": "最低竞价",
    "max_bid": "最高竞价",
    "bid": "建议竞价",
    "bid_min": "最低竞价",
    "bid_max": "最高竞价",
    "top3_brands": "点击量前三品牌",
    "top3_asin_dto_list": "点击量前三 ASIN",
    "amazon_choice": "是否 Amazon Choice",
    "supplement": "是否为补充关键词（无当前月搜索量）",
    "trends": "趋势数据",
    "monthly_sales": "月销量",
    "monthly_revenue": "月销售额",
    "bsr": "BSR",
    "rating": "评分",
    "reviews": "评论数",
    "brand": "品牌",
    "seller_name": "卖家",
    "marketplace": "站点",
    "node_id_path": "类目节点路径",
    "node_id": "类目节点ID",
    "node_path": "类目节点路径",
    "node_name": "类目名称",
    "count": "记录数量",
    "goods_count": "商品数量",
    "month": "统计月份",
    "search_model": "搜索模式",
    "request": "请求",
    "filter": "筛选条件",
    "order": "排序规则",
    "orderby": "排序规则",
    "field": "排序字段",
    "desc": "是否降序",
    "analysis_type": "分析类型",
    "workflow": "调研对象类型",
    "report_date": "报告日期",
    "research_task": "研究任务",
    "objective": "研究目标",
    "entity_type": "研究对象类型",
    "entity": "研究对象",
    "entity_source": "对象来源",
    "time_window": "时间范围",
    "target_category_path": "目标类目路径",
    "analysis_targets": "分析目标",
    "quality_summary": "证据质量汇总",
    "coverage_summary": "证据覆盖汇总",
    "call_count": "调用总数",
    "data_call_count": "有数据的调用数",
    "empty_call_count": "空结果调用数",
    "error_call_count": "失败调用数",
    "all_product_list_calls": "商品榜单调用是否全部完成",
    "all_product_search_calls": "商品搜索调用是否全部完成",
    "category_search": "类目搜索",
    "segment_search": "细分方向搜索",
    "completed_pages": "已完成页码",
    "target_pages": "目标页码",
    "product_search_pages": "商品搜索页码",
    "queries": "查询词列表",
    "returned_rows": "实际返回记录数",
    "unique_products": "去重后商品数",
    "exact_empty_results": "明确为空的调用",
    "returned_rows_outside_requested_l3": "返回记录超出请求的三级类目范围",
    "derived_facts": "程序派生事实",
    "conflicts": "证据冲突",
    "limitations": "证据限制",
    "hard_fact_boundaries": "硬事实边界",
    "rules": "规则",
    "empty": "成功但无记录的工具",
    "error": "调用失败的工具",
    "source_ref": "证据引用",
    "tool_name": "工具名称",
    "arguments": "调用参数",
    "business_data": "业务返回数据",
    "evidence_fence": "证据围栏",
    "data_state": "数据状态",
    "ok": "调用是否成功",
    "enough_data": "证据是否充足",
    "returned_count": "返回记录数",
    "reported_total": "接口报告总量",
    "period": "统计周期",
    "parser_status": "解析状态",
    "scope": "证据用途",
    "metric_grain": "指标粒度",
    "entity_refs": "证据实体",
    "type": "实体类型",
    "guest_id": "访客ID",
    "guest_visited": "访客访问次数",
    "took": "接口耗时",
    "terminal": "是否到达末页",
    "has_next_page": "是否还有下一页",
    "url": "链接",
    "detail_url": "详情链接",
    "image_url": "图片链接",
    "avatar_url": "头像链接",
    "cover_url": "封面链接",
    "fastmoss_url": "FastMoss 链接",
    "fastmoss_detail_url": "FastMoss 详情链接",
    "tiktok_url": "TikTok 链接",
    "has_sku_options": "是否有 SKU 选项",
    "has_paid_promotion": "是否有付费推广",
    "popularity_index": "热度指数",
    "viral_index": "爆发指数",
    "timestamp": "接口响应时间",
    "request_id": "请求ID",
    "message": "接口消息",
    "total": "结果总数",
    "items": "记录列表",
    "data": "业务数据",
    "category": "类目信息",
    "product": "商品信息",
    "shop": "店铺信息",
    "video": "视频信息",
    "overview": "汇总概览",
    "avatar": "头像链接",
    "cover": "封面链接",
    "create_date": "创建日期",
    "create_time": "发布时间",
    "publish_time": "发布时间",
    "launch_time": "上架时间",
    "launch_date": "上架日期",
    "listing_date": "上架日期",
    "shop_name": "店铺名称",
    "floor_price": "最低价格",
    "ceiling_price": "最高价格",
    "price_display": "价格展示值",
    "first_3d_gmv": "上架后前3日销售额（GMV）",
    "first_3d_units_sold": "上架后前3日销量",
    "lifetime_gmv": "上架以来累计销售额（GMV）",
    "period_gmv": "本统计周期销售额（GMV）",
    "period_units_sold": "本统计周期销量",
    "units_sold_growth_rate_percent": "销量环比增长率",
    "category_level": "类目层级",
    "category_name": "类目名称",
    "category_gmv_yoy_percent": "类目GMV同比增长率",
    "category_units_sold": "类目销量",
    "category_units_sold_yoy_percent": "类目销量同比增长率",
    "parent_category_id": "父类目ID",
    "parent_category_name": "父类目名称",
    "ranked_category_level": "榜单类目层级",
    "channel_gmv_share": "成交渠道GMV占比",
    "live_gmv_share_percent": "直播成交GMV占比",
    "live_gmv_share_change_percent": "直播成交GMV占比变化",
    "video_gmv_share_percent": "视频成交GMV占比",
    "video_gmv_share_change_percent": "视频成交GMV占比变化",
    "other_gmv_share_percent": "其他渠道成交GMV占比",
    "other_gmv_share_change_percent": "其他渠道成交GMV占比变化",
    "top_10_shops_units_sold_share_percent": "销量前10店铺的销量占比",
    "top_50_products_units_sold_share_percent": "销量前50商品的销量占比",
    "l1": "一级类目",
    "l2": "二级类目",
    "l3": "三级类目",
    "lang": "返回语言",
    "commission_rate": "佣金比例",
    "commission_rate_percent": "佣金比例",
    "product_rating": "商品评分",
    "sku_count": "SKU 数量",
    "is_ad": "是否广告视频",
    "aweme_count": "关联视频数",
    "favoriting_count": "收藏数",
    "video_desc": "视频描述",
    "duration": "视频时长（秒）",
    "duration_seconds": "视频时长（秒）",
    "day7_units_sold": "近7日销量",
    "day7_gmv": "近7日销售额",
    "day28_units_sold": "近28日销量",
    "day28_gmv": "近28日销售额",
    "day90_units_sold": "近90日销量",
    "day90_gmv": "近90日销售额",
    "yday_sold_count": "昨日销量",
    "total_gmv": "总销售额（GMV）",
    "total_units_sold": "总销量",
    "avg_daily_gmv": "日均销售额（GMV）",
    "avg_daily_units_sold": "日均销量",
    "linked_creator_count": "关联达人数",
    "linked_live_count": "关联直播数",
    "linked_video_count": "关联视频数",
    "daily_new_linked_creator_count": "当日新增关联达人数",
    "daily_new_linked_live_count": "当日新增关联直播数",
    "daily_new_linked_video_count": "当日新增关联视频数",
    "cumulative_linked_creator_count": "累计关联达人数",
    "cumulative_linked_live_count": "累计关联直播数",
    "cumulative_linked_video_count": "累计关联视频数",
    "live_gmv": "直播销售额（GMV）",
    "live_units_sold": "直播销量",
    "video_gmv": "视频销售额（GMV）",
    "video_units_sold": "视频销量",
    "period_total_gmv": "本周期总销售额（GMV）",
    "period_total_units_sold": "本周期总销量",
    "daily_gmv": "当日销售额（GMV）",
    "daily_units_sold": "当日销量",
    "cumulative_gmv": "累计销售额（GMV）",
    "cumulative_units_sold": "累计销量",
    "average_order_value": "客单价",
    "ad_performance_summary": "广告表现汇总",
    "daily_ad_performance_trend": "每日广告表现趋势",
    "ad_gmv": "广告归因销售额（GMV）",
    "ad_gmv_share_percent": "广告归因销售额占比",
    "ad_units_sold": "广告归因销量",
    "ad_video_count": "广告视频数",
    "ad_play_count": "广告视频播放量",
    "estimated_ad_spend": "预估广告花费",
    "avg_daily_estimated_ad_spend": "日均预估广告花费",
    "avg_daily_ad_gmv": "日均广告归因销售额（GMV）",
    "avg_daily_ad_units_sold": "日均广告归因销量",
    "avg_daily_ad_play_count": "日均广告视频播放量",
    "product_total_gmv": "商品总销售额（GMV）",
    "roas": "广告投入产出比（ROAS）",
    "creator_share_percent": "达人贡献占比",
    "creator_cumulative_gmv": "达人累计销售额（GMV）",
    "creator_cumulative_units_sold": "达人累计销量",
    "creator_total_play_count": "达人视频累计播放量",
    "creator_total_like_count": "达人视频累计点赞数",
    "creator_video_count_total": "达人视频总数",
    "caption_text": "视频文案",
    "engagement_metrics": "互动指标",
    "period_summary": "周期汇总",
    "daily_trend": "每日趋势",
    "ads_distribution": "广告与非广告归因分布",
    "channel_distribution": "成交渠道分布",
    "content_distribution": "内容类型分布",
    "breakdown": "明细",
    "ranking_scope": "榜单范围",
    "ranked_categories": "类目榜单",
    "scale_metrics": "规模指标",
    "growth_metrics": "增长指标",
    "concentration_metrics": "集中度指标",
    "creator_summary": "达人汇总",
    "linked_creators": "关联达人",
    "videos": "视频记录",
    "list": "记录列表",
    "balance": "剩余额度",
    "credits": "额度",
    "credit_balance": "剩余额度",
    "used_credits": "已使用额度",
    "remaining_credits": "剩余额度",
    "subscription": "订阅方案",
    "trial_package": "试用套餐",
    "top_up_packages": "充值套餐",
    "billing_period": "计费周期",
    "category_path": "类目路径",
    "category_l1_id": "一级类目ID",
    "category_l2_id": "二级类目ID",
    "category_l3_id": "三级类目ID",
    "is_new_listed": "是否近期上架",
    "product_source": "商品来源",
    "off_shelves": "是否下架",
    "shipping_type": "配送方式",
    "shop_type": "店铺类型",
    "sales_summary": "销售表现汇总",
    "distribution_summary": "分布结构汇总",
    "commerce_summary": "带货表现汇总",
    "audience_summary": "受众概览",
    "performance_summary": "表现汇总",
    "ranking_metrics": "排名指标",
    "potential_metrics": "潜力指标",
    "follower_tier_distribution": "粉丝层级分布",
    "creator_category_distribution": "达人类目分布",
    "product_contribution": "商品贡献",
    "creator_cumulative_performance": "达人累计表现",
    "trend_series": "趋势序列",
    "sales_price_distribution": "销售价格分布",
    "sub_category_units_sold_total": "子类目总销量",
    "shop_cumulative_units_sold": "店铺累计销量",
    "sales_timeline": "销售时间线",
    "interaction_rate": "互动率",
    "linked_products": "关联商品",
    "has_email": "是否提供邮箱",
    "run_days": "投放天数",
    "landing_page": "落地页类型",
    "price_band": "价格区间",
    "price_range": "价格范围",
    "follower_tier": "粉丝层级",
    "category_summary": "类目汇总",
    "category_breakdown": "类目明细",
    "account": "账号信息",
    "creator": "达人信息",
    "live": "直播信息",
    "agency": "机构信息",
    "reviews_list": "评论记录",
    "review_id": "评论ID",
    "review_content": "评论内容",
    "review_rating": "评论评分",
    "inventory": "库存",
    "inventory_share_percent": "库存占比",
    "sku": "SKU信息",
    "sku_id": "SKU ID",
    "units_sold_ratio_percent": "销量占比",
    "status": "状态",
    "reason": "原因",
    "issue": "问题",
    "metric": "指标",
    "value": "数值",
    "unit": "单位",
    "conflict_type": "冲突类型",
    "denominator_product_count": "分母商品数",
    "denominator_units": "分母销量",
    "numerator_top_n": "分子头部商品数",
    "input_product_ids": "参与计算的商品ID",
    "claim_boundary": "结论边界",
    "coverage_complete": "是否完成计划覆盖",
    "fetched_unique": "实际获取的去重记录数",
    "attempted_pages": "已尝试页码",
    "reported_total": "接口报告总量",
    "products_with_units": "有销量的商品数",
    "sample_units_total": "样本销量合计",
    "eligible_product_count": "符合条件的商品数",
    "q1": "第一四分位数",
    "median": "中位数",
    "q3": "第三四分位数",
    "min": "最小值",
    "max": "最大值",
    "last_7d_gmv": "近7日销售额（GMV）",
    "last_7d_units_sold": "近7日销量",
    "last_28d_gmv": "近28日销售额（GMV）",
    "last_28d_units_sold": "近28日销量",
    "last_90d_gmv": "近90日销售额（GMV）",
    "last_90d_units_sold": "近90日销量",
    "yesterday_gmv": "昨日销售额（GMV）",
    "yesterday_units_sold": "昨日销量",
    "result": "查询结果",
    "summary_metrics": "汇总指标",
    "category_l1": "一级类目",
    "category_l2": "二级类目",
    "category_l3": "三级类目",
    "category_sales_rank": "类目销量排名",
    "country_sales_rank": "国家销量排名",
    "ceiling_price_display": "最高价格展示值",
    "floor_price_display": "最低价格展示值",
    "currency_symbol": "币种符号",
    "is_new_product": "是否新品",
    "review_count": "评论数",
    "shipping_fee": "运费",
    "shipping_method_code": "配送方式编码",
    "stock_count": "库存数量",
    "stock_count_label": "库存状态说明",
    "shop_category_l1": "店铺一级类目",
    "shop_total_gmv": "店铺累计销售额（GMV）",
    "shop_total_units_sold": "店铺累计销量",
    "active_product_count": "在售商品数",
    "new_product_count": "新品数量",
    "period_label": "统计周期说明",
    "selling_creator_count": "产生销售的达人数",
    "selling_live_count": "产生销售的直播数",
    "selling_video_count": "产生销售的视频数",
    "creator_handle": "达人账号",
    "window_gmv": "观察窗口销售额（GMV）",
    "window_units_sold": "观察窗口销量",
    "traffic_flags": "流量属性",
    "video_meta": "视频元数据",
    "creator_category": "达人类目",
    "age_distribution": "年龄分布",
    "gender_distribution": "性别分布",
    "creator_name": "达人名称",
    "product_gmv": "商品销售额（GMV）",
    "product_units_sold": "商品销量",
    "product_linked_live_count": "商品关联直播数",
    "product_linked_video_count": "商品关联视频数",
    "product_video_gmv": "商品视频归因销售额（GMV）",
    "product_video_units_sold": "商品视频归因销量",
    "categories": "类目记录",
    "active_product_count_average": "平均在售商品数",
    "active_product_count_total": "在售商品数合计",
    "category_units_sold_average": "类目平均销量",
    "category_units_sold_total": "类目销量合计",
    "new_product_count_average": "平均新品数量",
    "new_product_count_total": "新品数量合计",
    "selling_creator_count_average": "平均产生销售的达人数",
    "selling_creator_count_total": "产生销售的达人数合计",
    "selling_live_count_average": "平均产生销售的直播数",
    "selling_live_count_total": "产生销售的直播数合计",
    "selling_video_count_average": "平均产生销售的视频数",
    "selling_video_count_total": "产生销售的视频数合计",
    "category_id_level1": "一级类目ID",
    "category_id_level2": "二级类目ID",
    "category_id_level3": "三级类目ID",
    "cn_name": "中文类目名称",
    "cn_full_name": "中文完整类目路径",
    "max_total_results": "最多返回结果数",
}

_REPORT_VALUE_LABELS = {
    "category_sample_top1_share": "已获取类目样本中销量最高商品的占比",
    "category_sample_top3_share": "已获取类目样本中销量前三商品的占比",
    "category_sample_top10_share": "已获取类目样本中销量前十商品的占比",
    "segment_sample_units": "同一查询词样本销量合计",
    "segment_sample_price_midpoint_quartiles": "同一查询词样本价格中点四分位数",
    "fetched_category_sample_only": "仅限本轮已获取的类目样本",
    "fetched_same_query_sample_only": "仅限本轮同一查询词样本",
    "same_query_nonconflicting_sample_not_recommended_price": "仅限同一查询词且无价格冲突的样本，不代表建议售价",
    "provider_currency": "接口返回币种",
    "units": "件",
    "ratio": "比例",
    "must_not_imply_share_of_unfetched_products_or_total_market": "不得外推为未获取商品或全市场份额",
    "observed_sample_band_not_recommended_launch_price": "仅为已观察样本价格带，不是建议上市价格",
    "returned_product_outside_requested_l3": "返回记录超出请求的三级类目范围",
    "gmv_units_price_conflict": "销售额、销量与价格口径冲突",
    "period_mismatch": "统计周期不一致",
    "entity_mismatch": "业务实体不一致",
}
_AUDIT_ONLY_FIELD_KEYS = {
    "avatar", "avatar_thumb", "avatar_url", "cover", "cover_url", "detail_url",
    "fastmoss_detail_url", "fastmoss_url", "image", "image_url", "images",
    "request_id", "timestamp", "tiktok_url", "tool_id", "url_list",
}
_INTERNAL_REPORT_KEYS = {
    "source_ref", "source_tool", "source_call_index", "tool_name", "fact_id",
    "input_fact_ids", "evidence_refs", "parser_status", "metric_grain",
}

_ACRONYM_WORDS = {"asin", "bsr", "cpr", "gmv", "id", "ipm", "ppc", "roas", "sku", "spr", "uid", "url"}
_FRACTION_PERCENT_FIELDS = {
    "ara_click_rate", "ara_share_rate", "click_rate", "click_share_rate", "conversion_rate", "cvs_share_rate",
    "monopoly_click_rate", "purchase_rate", "search_rank_cr", "search_rank_growth_rate",
    "w1_rank_growth_rate", "w4_rank_growth_rate", "w12_rank_growth_rate",
}
_PERCENT_VALUE_FIELDS = {
    "growth", "search_month_cr", "search_monthly_cr", "search_nearly_cr", "searches_growth",
}
_TIMESTAMP_FIELDS = {
    "published_at", "created_at", "updated_at", "publish_time", "create_time",
    "launch_time", "timestamp",
}
_ENUM_VALUE_LABELS = {
    "data_state": {
        "data": "已返回数据",
        "empty": "调用成功但没有返回记录",
        "error": "调用失败",
    },
    "traffic_type": {
        "ad_traffic": "广告归因流量",
        "non_ad_video_traffic": "非广告视频归因流量",
    },
    "traffic_source": {
        "ad_traffic": "广告归因流量",
        "non_ad_video_traffic": "非广告视频归因流量",
    },
    "sales_channel": {
        "product_card": "商品卡成交",
        "affiliate": "联盟成交",
        "video": "视频成交",
        "live": "直播成交",
        "shop": "店铺成交",
        "shop_account": "店铺账号成交",
    },
    "content_type": {
        "product_card": "商品卡",
        "video": "视频",
        "live": "直播",
    },
    "is_ad": {"0": "否（非广告）", "1": "是（广告）"},
    "off_shelves": {"0": "否（在售）", "1": "是（已下架）"},
    "account_type": {"1": "个人达人", "2": "店铺达人"},
    "ecommerce_type": {"1": "视频带货", "2": "直播带货"},
    "search_model": {
        "1": "热门市场", "2": "异动市场", "3": "持续增长市场",
        "4": "快速飙升市场", "5": "潜力市场", "6": "长尾市场",
    },
    "match_type": {"2": "广泛匹配", "3": "词组匹配"},
    "supplement": {"N": "否", "Y": "是"},
    "date_type": {"day": "日", "week": "周", "month": "月"},
    "order": {"asc": "升序", "desc": "降序"},
    "region": {
        "US": "美国（US）", "GB": "英国（GB）", "UK": "英国（UK）",
        "CA": "加拿大（CA）", "MX": "墨西哥（MX）", "BR": "巴西（BR）",
        "JP": "日本（JP）", "DE": "德国（DE）", "FR": "法国（FR）",
        "IT": "意大利（IT）", "ES": "西班牙（ES）", "AU": "澳大利亚（AU）",
    },
    "marketplace": {
        "US": "美国站（US）", "GB": "英国站（GB）", "UK": "英国站（UK）",
        "CA": "加拿大站（CA）", "MX": "墨西哥站（MX）", "BR": "巴西站（BR）",
        "JP": "日本站（JP）", "DE": "德国站（DE）", "FR": "法国站（FR）",
        "IT": "意大利站（IT）", "ES": "西班牙站（ES）", "AU": "澳大利亚站（AU）",
    },
    "currency": {
        "USD": "美元（USD）", "GBP": "英镑（GBP）", "EUR": "欧元（EUR）",
        "CAD": "加拿大元（CAD）", "MXN": "墨西哥比索（MXN）", "BRL": "巴西雷亚尔（BRL）",
        "JPY": "日元（JPY）", "AUD": "澳大利亚元（AUD）",
    },
    "currency_code": {
        "USD": "美元（USD）", "GBP": "英镑（GBP）", "EUR": "欧元（EUR）",
        "CAD": "加拿大元（CAD）", "MXN": "墨西哥比索（MXN）", "BRL": "巴西雷亚尔（BRL）",
        "JPY": "日元（JPY）", "AUD": "澳大利亚元（AUD）",
    },
    "lang": {"ZH_CN": "简体中文", "EN": "英文"},
    "parser_status": {"supported": "已按已知接口结构解析", "unsupported_parser": "暂无专用解析规则"},
    "scope": {
        "supporting": "辅助证据", "primary": "主要证据",
        "cross_category": "跨类目", "category": "单一类目",
        "keyword": "单一关键词", "entity": "单一对象",
    },
    "objective": {
        "opportunity_discovery": "机会发现",
        "trend_analysis": "趋势分析",
        "product_research": "商品研究",
        "pricing_analysis": "定价分析",
        "competitor_analysis": "竞品分析",
        "category_analysis": "类目分析",
        "store_analysis": "店铺分析",
    },
    "entity_source": {
        "llm": "LLM 语义判断",
        "user": "用户明确输入",
        "rules": "结构化规则",
        "rules_fallback": "分类失败后的保守回退",
    },
    "entity_type": {
        "none": "无单一对象", "category": "类目", "keyword": "关键词",
        "product_id": "FastMoss 商品ID", "asin": "ASIN", "url": "链接",
        "shop_id": "店铺ID", "creator_id": "达人ID", "video_id": "视频ID",
    },
    "workflow": {
        "product": "商品研究", "category": "类目研究", "shop": "店铺研究",
        "creator": "达人研究", "video": "视频研究", "live": "直播研究",
    },
    "time_window": {
        "current": "当前请求范围",
        "today": "今日",
        "this_week": "本周",
        "this_month": "本月",
        "recent_1_2_months": "最近1至2个月",
    },
    "type": {
        "category": "类目", "product": "商品", "shop": "店铺", "creator": "达人",
        "video": "视频", "live": "直播", "asin": "ASIN", "keyword": "关键词", "none": "无单一对象",
    },
    "metric_grain": {
        "market_category_ranking": "市场类目榜单",
        "product_rank_new_listed": "新品榜单商品",
        "product_rank_top_selling": "热销榜单商品",
    },
}

_TOOL_EVIDENCE_BOUNDARIES = {
    "market_category_ranking": (
        "类目榜中的视频、直播和其他渠道 GMV 占比只描述本周期的成交结构，不能单独证明某渠道驱动了增长、某类商家具有优势或者存在特定流量因果。",
        "若报告需解释类目增长原因，应另行取得商品、视频、直播或达人粒度证据；未取得时只能描述占比现象。",
    ),
    "product_rank_new_listed": (
        "上架后前3日销售额（GMV）和前3日销量是三日累计口径，不是单日或一天内的指标。",
        "该榜单未返回具体视频、直播或达人证据；不得把新品爆发归因于某个视频、直播间或达人。需解释爆发原因时应先调用对应工具，否则只可标注为待验证假设。",
    ),
    "product_rank_top_selling": (
        "周期销量、销售额和增长率只适用于调用参数指定的地区与周期，不能改写为实时趋势或单日表现。",
        "该榜单未返回具体视频、直播或达人证据；如未调用对应工具，不得宣称某商品由单一爆款视频、直播或达人驱动。",
    ),
    "keyword_research": (
        "调用参数中的查询关键词只限定本次检索范围，不自动成为每一条返回记录的关键词名称；每条记录必须以其自身返回的关键词字段识别。",
        "如果某条记录没有返回关键词名称，该行只能视为匿名候选记录，不得根据指标、排序位置或常识为其补写长尾词名称，也不得把该行指标归到查询主词。",
    ),
    "aba_research_monthly": (
        "头部3个品牌和头部3个 ASIN 仅代表该关键词返回的头部样本，不能单独证明整个类目的品牌集中度或卖家国别结构。",
        "关联类目表示关键词可能出现的 Amazon 类目，不等同于该类目的市场规模或竞争强度。",
    ),
    "keyword_miner": (
        "商品数是工具口径下的相关商品数量，不是独立卖家数量。供需比是 SellerSprite 返回的计算指标，不能改写成搜索量除以卖家数。",
        "点击集中度描述点击向头部结果集中的程度，不等同于品牌集中度；标题密度也不等同于卖家数量。",
    ),
    "product_overview": (
        "广告归因、成交渠道和内容类型是三组并列口径，不能互相替代，也不能据此拼接出“视频种草后由商品卡成交”的因果链。",
        "广告归因占比为零只表示当前周期没有广告归因流量，不能证明广告花费为零；广告花费应以 product_investment 返回为准。",
        "商品卡占比不能证明流量由达人视频、直播或推荐系统产生。",
    ),
    "product_sales_trend": (
        "每日销量或 GMV 的同时变化只能描述时间趋势，不能单独证明由达人视频、直播、广告或其他事件导致。",
    ),
    "product_creator_analysis": (
        "关联达人数量和达人贡献描述已返回的关联结构，不能单独证明全部流量来源或达人内容与销量之间的因果关系。",
    ),
    "market_category_analysis": (
        "类目指标只适用于调用参数中的地区、类目和统计周期，不能直接外推为目标商品自身的表现。",
    ),
}


def unprefixed_tool_name(tool_name: str) -> str:
    return str(tool_name or "").split("__", 1)[-1]


def _path(parent: str, key: str) -> str:
    if _SIMPLE_PATH_KEY.fullmatch(str(key)):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(str(key), ensure_ascii=False)}]"


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_text(value: Any) -> str:
    if value is None:
        return "未返回"
    if value is True:
        return "是"
    if value is False:
        return "否"
    if isinstance(value, str):
        return value if value else "空字符串"
    return str(value)


def _escape(value: Any) -> str:
    return (
        _scalar_text(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _heading_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("#", "\\#") or "未命名"


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    headers = [str(item) for item in headers]
    lines = [
        "| " + " | ".join(_escape(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = list(row)
        cells.extend("" for _ in range(max(0, len(headers) - len(cells))))
        lines.append("| " + " | ".join(_escape(item) for item in cells[:len(headers)]) + " |")
    return lines


def business_leaf_paths(value: Any, path: str = "$.business_data") -> set[str]:
    if _scalar(value):
        return {path}
    if isinstance(value, list):
        if not value:
            return set()
        paths: set[str] = set()
        for index, item in enumerate(value):
            paths.update(business_leaf_paths(item, f"{path}[{index}]"))
        return paths
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, item in value.items():
            paths.update(business_leaf_paths(item, _path(path, str(key))))
        return paths
    raise TypeError(f"non-JSON business value at {path}: {type(value).__name__}")


def _normalized_field_key(key: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _field_label(key: str) -> str:
    normalized = _normalized_field_key(key)
    label = _FIELD_LABELS.get(key) or _FIELD_LABELS.get(normalized)
    if label:
        return label
    if not normalized:
        return str(key)
    return " ".join(
        word.upper() if word in _ACRONYM_WORDS else word
        for word in normalized.split("_")
    )


def _known_field_label(key: str) -> str | None:
    normalized = _normalized_field_key(key)
    return _FIELD_LABELS.get(key) or _FIELD_LABELS.get(normalized)


def _dotted_field_label(key: str) -> str:
    parts = [part for part in str(key).split(".") if part and part != "request"]
    return " · ".join(_field_label(part) for part in parts) or _field_label(key)


def _number_text(value: int | float) -> str:
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _percent_text(value: int | float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _semantic_value(field_name: str, value: Any) -> str:
    normalized = _normalized_field_key(str(field_name).rsplit(".", 1)[-1])
    if isinstance(value, Mapping):
        return "；".join(
            f"{_field_label(str(key))}：{_semantic_value(str(key), item)}"
            for key, item in value.items()
        ) or "空对象"
    if isinstance(value, list):
        return "；".join(_semantic_value(normalized, item) for item in value) or "空列表"
    if isinstance(value, str) and normalized == "return_fields":
        fields = [item.strip() for item in value.split(",") if item.strip()]
        return "、".join(_field_label(item) for item in fields) or "未指定（返回接口默认字段）"
    if isinstance(value, str) and normalized == "field":
        return _field_label(value)
    if isinstance(value, str) and normalized in {"date_value", "period"}:
        week = re.fullmatch(r"(\d{4})-(?:W)?(\d{1,2})", value, re.IGNORECASE)
        if week:
            return f"{week.group(1)}年第{int(week.group(2))}周"
    if value is None or isinstance(value, (str, bool)):
        text = _scalar_text(value)
        return _ENUM_VALUE_LABELS.get(normalized, {}).get(text, text)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if normalized in _TIMESTAMP_FIELDS and value >= 1_000_000_000:
            seconds = float(value) / 1000 if value >= 10_000_000_000 else float(value)
            moment = datetime.fromtimestamp(seconds, tz=timezone(timedelta(hours=8)))
            if normalized == "launch_time":
                return moment.date().isoformat()
            return moment.strftime("%Y-%m-%d %H:%M:%S（UTC+8）")
        enum_value = _ENUM_VALUE_LABELS.get(normalized, {}).get(str(value))
        if enum_value is not None:
            return enum_value
        if normalized.startswith(("is_", "has_")) and value in {0, 1}:
            return "是" if value == 1 else "否"
        if normalized in _FRACTION_PERCENT_FIELDS:
            return f"{_percent_text(float(value) * 100)}%"
        if normalized in _PERCENT_VALUE_FIELDS:
            return f"{_percent_text(value)}%"
        if normalized.endswith("_percent"):
            return f"{_percent_text(value)}%"
        return _number_text(value)
    return _scalar_text(value)


def localize_semantic_value(value: Any, field_name: str = "") -> Any:
    """Translate semantic display keys and enums without mutating source evidence."""
    if isinstance(value, Mapping):
        parent = _normalized_field_key(field_name)
        localized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _normalized_field_key(str(key))
            label = (
                "研究范围"
                if parent == "research_task" and normalized_key == "scope"
                else _field_label(str(key))
            )
            localized[label] = localize_semantic_value(item, str(key))
        return localized
    if isinstance(value, list):
        return [localize_semantic_value(item, field_name) for item in value]
    return _semantic_value(field_name, value)


def _argument_field_label(key: str) -> str:
    parts = str(key).split(".")
    if parts and _normalized_field_key(parts[-1]) == "keywords":
        parts[-1] = "查询关键词"
        return " / ".join(_field_label(part) for part in parts[:-1]) + (
            " / " if len(parts) > 1 else ""
        ) + parts[-1]
    return _dotted_field_label(key)


def _node_kind_for_list(path: str, rows: list[Any], profile: str) -> str:
    lowered_path = path.lower()
    if profile == PROFILE_NARRATIVE or any(hint in lowered_path for hint in _NARRATIVE_KEYS):
        return "NarrativeBlock"
    dict_rows = [row for row in rows if isinstance(row, dict)]
    row_keys = {_normalized_field_key(str(key)) for row in dict_rows for key in row.keys()}
    if dict_rows and row_keys.intersection(_TIME_KEYS):
        return "TimeSeries"
    if profile == PROFILE_DISTRIBUTION or any(hint in lowered_path for hint in _DISTRIBUTION_HINTS):
        return "Distribution"
    if profile in {PROFILE_RECORDS, PROFILE_REFERENCE, PROFILE_RELATIONSHIP} and dict_rows:
        return "RecordTable"
    return "RecordList"


def _entity_identity(value: Mapping[str, Any]) -> str:
    for key in (
        "asin", "parent_asin", "product_id", "seller_id", "shop_id", "creator_uid", "uid", "video_id",
        "room_id", "agency_id", "category_id", "id",
    ):
        item = value.get(key)
        if item not in (None, "", 0, "0"):
            return f"{_field_label(key)} {item}"
    for key in ("title", "name", "nickname"):
        if value.get(key):
            return f"{_field_label(key)} {value[key]}"
    return ""


def _argument_query_hint(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("keywords", "keyword", "query", "name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, list):
                words = [str(word).strip() for word in item if str(word).strip()]
                if words:
                    return "、".join(words[:3])
        for item in value.values():
            hint = _argument_query_hint(item)
            if hint:
                return hint
    return ""


def _clean_report_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\bcall:\d+\b", "本次证据", text, flags=re.IGNORECASE)
    for name, spec in FASTMOSS_RENDER_SPECS.items():
        text = text.replace(f"fastmoss__{name}", spec.evidence_title)
    for raw, label in {
        "source_ref": "证据来源", "arguments": "调用参数", "marketplace": "站点",
        "metric_grain": "指标口径", "entity_type": "研究对象类型",
        "returned_product_outside_requested_l3": "返回记录超出请求的三级类目范围",
    }.items():
        text = re.sub(rf"\b{re.escape(raw)}\b", label, text)
    return _REPORT_VALUE_LABELS.get(text, text)


def _report_semantic_metadata(value: Any, field_name: str = "") -> Any:
    if isinstance(value, Mapping):
        localized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _normalized_field_key(str(key))
            if normalized in _INTERNAL_REPORT_KEYS:
                continue
            label = _known_field_label(str(key))
            if label is None:
                continue
            child = _report_semantic_metadata(item, str(key))
            if child not in (None, "", [], {}):
                localized[label] = child
        return localized
    if isinstance(value, list):
        return [
            item
            for raw in value
            if (item := _report_semantic_metadata(raw, field_name)) not in (None, "", [], {})
        ]
    if isinstance(value, str):
        return _semantic_value(field_name, _clean_report_text(value))
    return _semantic_value(field_name, value)


def fastmoss_semantic_registry_diagnostics(runtime_tool_names: Iterable[str]) -> dict[str, Any]:
    runtime = {
        unprefixed_tool_name(name)
        for name in runtime_tool_names
        if str(name).startswith("fastmoss__") or "__" not in str(name)
    }
    registered = set(FASTMOSS_RENDER_SPECS)
    return {
        "runtime_count": len(runtime),
        "registered_count": len(registered),
        "missing_contracts": sorted(runtime - registered),
        "missing_runtime": sorted(registered - runtime),
        "ok": runtime == registered,
    }


class SemanticToolRenderer:
    def __init__(
        self,
        entry: Mapping[str, Any],
        render_specs: Mapping[str, ToolRenderSpec] | None = None,
    ) -> None:
        self.entry = dict(entry)
        self.full_tool_name = str(entry.get("tool_name") or "fastmoss__unknown")
        self.tool_name = unprefixed_tool_name(self.full_tool_name)
        specs = render_specs or FASTMOSS_RENDER_SPECS
        self.spec = specs.get(
            self.tool_name,
            ToolRenderSpec(self.tool_name, PROFILE_GENERIC, _entity_type_for_tool(self.tool_name)),
        )
        self.strict_contract = self.full_tool_name.startswith("fastmoss__")
        self.nodes: list[EvidenceNode] = []
        self.consumed: set[str] = set()
        self.unmapped: set[str] = set()
        self.excluded: set[str] = set()
        self.exclusion_reasons: dict[str, str] = {}
        self.diagnostics: list[str] = []
        self.generated_conflicts: list[str] = []

    def render(self) -> RenderedToolEvidence:
        data = self.entry.get("business_data")
        all_paths = business_leaf_paths(data)
        source_ref = str(self.entry.get("source_ref") or "call:?")
        fence = self.entry.get("evidence_fence") if isinstance(self.entry.get("evidence_fence"), dict) else {}
        data_state = str(fence.get("data_state") or "").strip().lower()
        error = str(self.entry.get("error") or "").strip()
        query_hint = _argument_query_hint(self.entry.get("arguments"))
        evidence_title = self.spec.evidence_title
        if query_hint and self.tool_name in {
            "product_search", "shop_search", "creator_search", "video_search", "live_search",
            "agency_search", "search_category_by_words", "ad_search",
        }:
            evidence_title = f"{query_hint} · {evidence_title}"
        lines = (
            [f"## {_heading_text(evidence_title)}"]
            if self.strict_contract
            else [f"## {_heading_text(source_ref)} · `{self.full_tool_name}`"]
        )
        lines.extend(["", *self._scope_lines()])
        boundary_lines = self._tool_boundary_lines()
        if boundary_lines:
            lines.extend(["", *boundary_lines])

        if not self.spec.report_included:
            self.nodes.append(EvidenceNode("AuditOnlyResult", evidence_title, "$.business_data"))
            self._exclude_value(data, "$.business_data", "接口账号或额度信息仅用于运行审计，不参与商业报告推理")
            lines.extend(["", "本段数据仅用于系统运行审计，不参与商业分析或报告推理。"])
        elif error or data_state == "error":
            self.nodes.append(EvidenceNode("ErrorResult", "调用失败", "$.business_data"))
            lines.extend([
                "", "### 调用结果", "",
                f"本次调用失败，失败范围仅限上述对象和参数。错误信息：{error or '工具返回错误状态。'}",
            ])
            self._exclude_value(data, "$.business_data", "调用失败，业务返回不参与报告推理")
        elif data_state == "empty":
            self.nodes.append(EvidenceNode("EmptyResult", "空结果", "$.business_data"))
            lines.extend([
                "", "### 调用结果", "",
                "本次调用成功，但针对上述精确对象、参数、地区和周期没有返回业务记录。"
                "这不表示平台全局为零，也不表示相关实体不存在。",
            ])
            # Empty response wrappers often contain list=[] and total=0.  They
            # describe the response state rather than an observed zero-valued
            # business metric, so the natural-language EmptyResult supersedes
            # those leaves.
            self._exclude_value(data, "$.business_data", "空结果已由自然语言状态说明替代")
        else:
            rendered = self._render_value(data, "$.business_data", 3, "业务结果")
            if rendered:
                lines.extend(["", *rendered])

        self.unmapped.update(all_paths - self.consumed - self.excluded)
        if self.unmapped:
            if self.strict_contract:
                raise ValueError(
                    f"semantic renderer left {len(self.unmapped)} business fields unmapped for {self.tool_name}"
                )

        conflicts = self.entry.get("scope_conflicts")
        if self.generated_conflicts:
            conflicts = [*(conflicts if isinstance(conflicts, list) else []), *self.generated_conflicts]
        if conflicts:
            lines.extend(["", "### 本次调用的证据边界", ""])
            lines.extend(self._render_complete_value(conflicts, "$.scope_conflicts", 4))

        result = RenderedToolEvidence(
            markdown="\n".join(lines).rstrip(),
            tool_name=self.tool_name,
            profile=self.spec.profile,
            node_types=[node.kind for node in self.nodes],
            business_leaf_paths=all_paths,
            consumed_paths=set(self.consumed),
            unmapped_paths=set(self.unmapped),
            excluded_paths=set(self.excluded),
            exclusion_reasons=dict(self.exclusion_reasons),
            diagnostics=list(self.diagnostics),
            empty=data_state == "empty",
        )
        if result.business_leaf_paths != result.consumed_paths | result.unmapped_paths | result.excluded_paths:
            raise ValueError(f"business field conservation failed for {self.tool_name}")
        if result.consumed_paths & result.unmapped_paths:
            raise ValueError(f"business field rendered twice for {self.tool_name}")
        return result

    def _exclude_value(self, value: Any, path: str, reason: str) -> None:
        for leaf_path in business_leaf_paths(value, path):
            self.excluded.add(leaf_path)
            self.exclusion_reasons[leaf_path] = reason

    def _strict_label(self, key: str, value: Any, path: str) -> str | None:
        normalized = _normalized_field_key(key)
        if normalized in _AUDIT_ONLY_FIELD_KEYS:
            self._exclude_value(value, path, f"{normalized} 为链接、图片或传输审计字段")
            return None
        label = _known_field_label(key)
        if label is None and self.strict_contract:
            self._exclude_value(value, path, f"{normalized or key} 尚无经核验的自然语言字段契约")
            self.diagnostics.append(f"{self.tool_name}: 仅审计字段 {normalized or key}")
            return None
        return label or _field_label(key)

    def _duplicate_date_keys(self, value: Mapping[str, Any], path: str) -> set[str]:
        if not self.strict_contract:
            return set()
        if "launch_date" not in value or "launch_time" not in value:
            return set()
        launch_date = str(value.get("launch_date") or "").strip()[:10]
        launch_time = value.get("launch_time")
        timestamp_date = _semantic_value("launch_time", launch_time) if launch_time not in (None, "") else ""
        launch_time_path = _path(path, "launch_time")
        self._exclude_value(launch_time, launch_time_path, "与自然化后的上架日期重复，报告只展示一个日期")
        if launch_date and timestamp_date and launch_date != timestamp_date:
            self.generated_conflicts.append(
                f"同一记录的上架日期为 {launch_date}，Unix上架时间换算日期为 {timestamp_date}，两者不一致；报告不得自行选择其一修正。"
            )
        return {"launch_time"}

    def _scope_lines(self) -> list[str]:
        arguments = self.entry.get("arguments")
        fence = self.entry.get("evidence_fence") if isinstance(self.entry.get("evidence_fence"), dict) else {}
        rows: list[tuple[str, Any]] = []
        if isinstance(arguments, dict):
            if not self.strict_contract:
                rows.extend(
                    (f"调用参数：{_argument_field_label(key)}", _semantic_value(key, value))
                    for key, value in self._flatten(arguments)
                )
            else:
                for key, value in self._flatten(arguments):
                    last_key = str(key).rsplit(".", 1)[-1]
                    label = _known_field_label(last_key)
                    if label is None:
                        self.diagnostics.append(f"{self.tool_name}: 未展示调用参数 {key}")
                        continue
                    rows.append((label, _semantic_value(key, value)))
            rows.extend(self._period_context_rows(arguments))
        if not self.strict_contract:
            for key, value in fence.items():
                if value not in (None, "", [], {}):
                    rows.append((f"证据范围：{_field_label(str(key))}", _semantic_value(str(key), value)))
        if not rows:
            return [
                "> 本段证据没有额外的业务范围参数。"
                if self.strict_contract
                else "> 本次调用没有额外参数或围栏字段。"
            ]
        return _table(
            ["证据范围", "值"] if self.strict_contract else ["调用范围", "值"],
            rows,
        )

    def _period_context_rows(self, arguments: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """The provider's period code is authoritative; never invent boundaries."""
        if self.strict_contract:
            return []
        periods: list[tuple[str, str]] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                date_type = value.get("date_type")
                date_value = value.get("date_value")
                if date_type not in (None, "") and date_value not in (None, ""):
                    periods.append((str(date_type), str(date_value)))
                date_info = value.get("date_info")
                if isinstance(date_info, Mapping):
                    info_type = date_info.get("type")
                    info_value = date_info.get("value")
                    if info_type not in (None, "") and info_value not in (None, ""):
                        periods.append((str(info_type), str(info_value)))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(arguments)
        rows: list[tuple[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for date_type, date_value in periods:
            key = (date_type.lower(), date_value)
            if key in seen:
                continue
            seen.add(key)
            if key[0] == "week":
                match = re.fullmatch(r"(\d{4})-(?:W)?(\d{1,2})", date_value, re.IGNORECASE)
                if not match:
                    continue
                try:
                    start = datetime.fromisocalendar(int(match.group(1)), int(match.group(2)), 1).date()
                except ValueError:
                    continue
                end = start + timedelta(days=6)
                rows.extend([
                    ("程序补充：ISO周日期范围", f"{start.isoformat()} 至 {end.isoformat()}"),
                    ("程序补充：周范围口径", "按 ISO 8601 的周一至周日换算；接口未提供平台自定义周边界。"),
                ])
            elif key[0] == "month":
                match = re.fullmatch(r"(\d{4})[-.]?(\d{2})", date_value)
                if not match:
                    continue
                start_dt = datetime(int(match.group(1)), int(match.group(2)), 1)
                next_month = datetime(start_dt.year + (1 if start_dt.month == 12 else 0), 1 if start_dt.month == 12 else start_dt.month + 1, 1)
                rows.append(("程序补充：自然月日期范围", f"{start_dt.date().isoformat()} 至 {(next_month - timedelta(days=1)).date().isoformat()}"))
        return rows

    def _tool_boundary_lines(self) -> list[str]:
        notes = list(_TOOL_EVIDENCE_BOUNDARIES.get(self.tool_name, ()))
        if self.tool_name == "keyword_research":
            data = self.entry.get("business_data")
            items = data.get("items") if isinstance(data, Mapping) else None
            if isinstance(items, list) and any(isinstance(item, Mapping) for item in items):
                identified = sum(
                    1 for item in items
                    if isinstance(item, Mapping)
                    and (item.get("keywords") not in (None, "") or item.get("keyword") not in (None, ""))
                )
                if identified < len(items):
                    notes.append(
                        f"本次返回 {len(items)} 条记录，其中 {len(items) - identified} 条没有关键词名称；这些匿名行的数值不得绑定到任何具体关键词。"
                    )
        if not notes:
            return []
        return ["### 指标口径与限制", "", *(f"- {note}" for note in notes)]

    def _flatten(self, value: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                child_prefix = prefix if not prefix and str(key) == "request" else name
                rows.extend(self._flatten(item, child_prefix))
            else:
                rows.append((name, item))
        return rows

    @staticmethod
    def _compact(value: Any) -> str:
        return _semantic_value("", value)

    def _render_value(self, value: Any, path: str, level: int, title: str) -> list[str]:
        if _scalar(value):
            kind = "NarrativeBlock" if isinstance(value, str) else "MetricGroup"
            self.nodes.append(EvidenceNode(kind, title, path))
            self.consumed.add(path)
            field_name = path.rsplit(".", 1)[-1]
            return [
                f"{'#' * min(level, 6)} {_heading_text(title)}",
                "",
                _semantic_value(field_name, value),
            ]
        if isinstance(value, list):
            if not value:
                return [f"{'#' * min(level, 6)} {_heading_text(title)}", "", "该字段返回空列表。"]
            return self._render_list(value, path, level, title)
        if isinstance(value, dict):
            if not value:
                return [f"{'#' * min(level, 6)} {_heading_text(title)}", "", "该字段返回空对象。"]
            return self._render_dict(value, path, level, title)
        raise TypeError(f"non-JSON business value at {path}: {type(value).__name__}")

    def _render_dict(self, value: dict[str, Any], path: str, level: int, title: str) -> list[str]:
        is_entity = bool(_entity_identity(value)) and (
            self.spec.profile == PROFILE_ENTITY
            or title.lower() in _ENTITY_CONTAINER_KEYS
            or path == "$.business_data"
        )
        kind = "EntityBlock" if is_entity else "MetricGroup"
        self.nodes.append(EvidenceNode(kind, title, path))
        heading = title
        identity = _entity_identity(value)
        if identity:
            heading = f"{title} · {identity}"
        lines = [f"{'#' * min(level, 6)} {_heading_text(heading)}"]

        scalar_rows: list[tuple[str, Any]] = []
        nested: list[tuple[str, Any, str]] = []
        duplicate_date_keys = self._duplicate_date_keys(value, path)
        for key, item in value.items():
            key = str(key)
            child_path = _path(path, str(key))
            if key in duplicate_date_keys:
                continue
            label = self._strict_label(key, item, child_path) if self.strict_contract else _field_label(key)
            if label is None:
                continue
            if _scalar(item):
                scalar_rows.append((label, _semantic_value(key, item)))
                self.consumed.add(child_path)
            elif isinstance(item, dict) and key.lower() not in _ENTITY_CONTAINER_KEYS:
                flattened, child_nested = self._flatten_dict_fields(
                    item, child_path, label if self.strict_contract else key
                )
                scalar_rows.extend(flattened)
                nested.extend(child_nested)
            else:
                nested.append((label if self.strict_contract else key, item, child_path))
        if scalar_rows:
            lines.extend(["", *_table(["指标", "值"], scalar_rows)])

        for key, item, child_path in nested:
            child_lines = self._render_value(
                item, child_path, level + 1, key if self.strict_contract else _dotted_field_label(key)
            )
            if child_lines:
                lines.extend(["", *child_lines])
        return lines

    def _render_list(self, value: list[Any], path: str, level: int, title: str) -> list[str]:
        kind = _node_kind_for_list(path, value, self.spec.profile)
        self.nodes.append(EvidenceNode(kind, title, path))
        lines = [
            f"{'#' * min(level, 6)} {_heading_text(title)}",
            "",
            f"> 本次实际返回 {len(value)} 条记录，以下完整展示全部记录。",
        ]
        if all(_scalar(item) for item in value):
            parent_key = path.rsplit(".", 1)[-1]
            rows = []
            for index, item in enumerate(value):
                item_path = f"{path}[{index}]"
                self.consumed.add(item_path)
                rows.append((index + 1, _semantic_value(parent_key, item)))
            lines.extend(["", *_table(["序号", _field_label(parent_key)], rows)])
            return lines

        if all(isinstance(item, dict) for item in value):
            records: list[dict[str, Any]] = [item for item in value if isinstance(item, dict)]
            flattened_records: list[dict[str, tuple[Any, str]]] = []
            nested_by_record: list[list[tuple[str, Any, str]]] = []
            ordered_keys: list[str] = []
            for index, record in enumerate(records):
                flattened, record_nested = self._flatten_record(
                    record, f"{path}[{index}]"
                )
                flattened_records.append(flattened)
                nested_by_record.append(record_nested)
                for key in flattened:
                    if key not in ordered_keys:
                        ordered_keys.append(key)
            primary = ordered_keys[:12]
            extra = ordered_keys[12:]
            if primary:
                rows = []
                for index, record in enumerate(flattened_records):
                    row: list[Any] = [index + 1]
                    for key in primary:
                        if key in record:
                            value_item, child_path = record[key]
                            row.append(value_item)
                            self.consumed.add(child_path)
                        else:
                            row.append("（字段缺失）")
                    rows.append(row)
                headers = primary if self.strict_contract else [_dotted_field_label(key) for key in primary]
                lines.extend(["", *_table(["序号", *headers], rows)])
            if extra:
                supplemental: list[tuple[Any, ...]] = []
                for index, record in enumerate(records):
                    for key in extra:
                        flattened_record = flattened_records[index]
                        if key not in flattened_record:
                            continue
                        value_item, child_path = flattened_record[key]
                        self.consumed.add(child_path)
                        supplemental.append((index + 1, key if self.strict_contract else _dotted_field_label(key), value_item))
                if supplemental:
                    lines.extend(["", "补充指标：", "", *_table(
                        ["记录", "指标", "值"], supplemental
                    )])
            for index, record_nested in enumerate(nested_by_record):
                for key, item, child_path in record_nested:
                    nested = self._render_value(
                        item,
                        child_path,
                        level + 1,
                        f"记录 {index + 1} · {key if self.strict_contract else _dotted_field_label(key)}",
                    )
                    if nested:
                        lines.extend(["", *nested])
            return lines

        for index, item in enumerate(value):
            nested = self._render_value(item, f"{path}[{index}]", level + 1, f"记录 {index + 1}")
            if nested:
                lines.extend(["", *nested])
        return lines

    def _flatten_dict_fields(
        self,
        value: dict[str, Any],
        path: str,
        prefix: str,
    ) -> tuple[list[tuple[str, Any]], list[tuple[str, Any, str]]]:
        rows: list[tuple[str, Any]] = []
        nested: list[tuple[str, Any, str]] = []
        for key, item in value.items():
            key = str(key)
            child_path = _path(path, key)
            label = self._strict_label(key, item, child_path) if self.strict_contract else _field_label(key)
            if label is None:
                continue
            field_name = (
                f"{prefix} · {label}" if prefix else label
            ) if self.strict_contract else (f"{prefix}.{key}" if prefix else key)
            if _scalar(item):
                rows.append((field_name if self.strict_contract else _dotted_field_label(field_name), _semantic_value(key, item)))
                self.consumed.add(child_path)
            elif isinstance(item, dict) and key.lower() not in _ENTITY_CONTAINER_KEYS:
                child_rows, child_nested = self._flatten_dict_fields(
                    item, child_path, field_name
                )
                rows.extend(child_rows)
                nested.extend(child_nested)
            else:
                nested.append((field_name, item, child_path))
        return rows, nested

    def _flatten_record(
        self,
        value: dict[str, Any],
        path: str,
        prefix: str = "",
    ) -> tuple[dict[str, tuple[Any, str]], list[tuple[str, Any, str]]]:
        fields: dict[str, tuple[Any, str]] = {}
        nested: list[tuple[str, Any, str]] = []
        duplicate_date_keys = self._duplicate_date_keys(value, path)
        for key, item in value.items():
            key = str(key)
            child_path = _path(path, key)
            if key in duplicate_date_keys:
                continue
            label = self._strict_label(key, item, child_path) if self.strict_contract else _field_label(key)
            if label is None:
                continue
            field_name = (
                f"{prefix} · {label}" if prefix else label
            ) if self.strict_contract else (f"{prefix}.{key}" if prefix else key)
            if _scalar(item):
                fields[field_name] = (_semantic_value(key, item), child_path)
            elif isinstance(item, dict):
                child_fields, child_nested = self._flatten_record(
                    item, child_path, field_name
                )
                fields.update(child_fields)
                nested.extend(child_nested)
            else:
                nested.append((field_name, item, child_path))
        return fields, nested

    def _render_complete_value(self, value: Any, path: str, level: int) -> list[str]:
        semantic = _report_semantic_metadata(value)
        rendered = json_to_markdown(semantic, title="边界详情", include_paths=False)
        body = rendered.splitlines()[1:]
        return [line for line in body if line.strip()]

    @staticmethod
    def _value_at_path(root: Any, path: str) -> Any:
        # The paths are generated internally; a compact parser is sufficient and
        # avoids duplicating the value inside another lookup map.
        if path == "$.business_data":
            return root
        cursor = root
        suffix = path[len("$.business_data"):]
        token_re = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\[(\"(?:[^\"\\]|\\.)*\")\]")
        for match in token_re.finditer(suffix):
            if match.group(1) is not None:
                cursor = cursor[match.group(1)]
            elif match.group(2) is not None:
                cursor = cursor[int(match.group(2))]
            else:
                cursor = cursor[json.loads(match.group(3))]
        return cursor


def render_fastmoss_tool_evidence(entry: Mapping[str, Any]) -> RenderedToolEvidence:
    """Render one call; isolate contract failures without leaking raw JSON."""

    tool_name = unprefixed_tool_name(str(entry.get("tool_name") or "unknown"))
    if tool_name not in FASTMOSS_RENDER_SPECS:
        data = entry.get("business_data")
        paths = business_leaf_paths(data)
        reason = "运行时工具没有登记 FastMoss Semantic 契约"
        return RenderedToolEvidence(
            markdown=(
                "## 未登记的业务证据\n\n"
                "该段返回仅保留在审计证据中，未交给报告模型推理；系统已记录缺失契约诊断。"
            ),
            tool_name=tool_name,
            profile=PROFILE_GENERIC,
            node_types=["ContractIsolation"],
            business_leaf_paths=paths,
            excluded_paths=paths,
            exclusion_reasons={path: reason for path in paths},
            diagnostics=[f"{tool_name}: {reason}"],
            fallback=True,
        )
    renderer = SemanticToolRenderer(entry)
    try:
        return renderer.render()
    except Exception as exc:
        data = entry.get("business_data")
        paths = business_leaf_paths(data)
        spec = FASTMOSS_RENDER_SPECS.get(
            tool_name, ToolRenderSpec(tool_name, PROFILE_GENERIC, "reference")
        )
        markdown = (
            f"## {spec.evidence_title}\n\n"
            "该段业务返回未通过已登记的 Semantic 字段契约，因此仅保留在审计证据中，"
            "未交给报告模型推理。"
        )
        reason = f"Semantic 契约渲染失败：{type(exc).__name__}"
        return RenderedToolEvidence(
            markdown=markdown,
            tool_name=tool_name,
            profile=spec.profile,
            node_types=["ContractIsolation"],
            business_leaf_paths=paths,
            excluded_paths=paths,
            exclusion_reasons={path: reason for path in paths},
            diagnostics=[f"{tool_name}: {type(exc).__name__}: {exc}"],
            fallback=True,
        )


def _context_markdown(dossier: Mapping[str, Any]) -> list[str]:
    category_path = dossier.get("target_category_path") or []
    category_text = " > ".join(str(item) for item in category_path) if isinstance(category_path, list) else str(category_path)
    targets = _report_semantic_metadata(dossier.get("analysis_targets") or [])
    rows = [
        ("工作流", _semantic_value("workflow", dossier.get("workflow") or "product")),
        ("报告日期", dossier.get("report_date") or ""),
        ("目标类目路径", category_text or "未指定"),
        ("分析目标", _semantic_value("analysis_targets", targets)),
    ]
    return ["## 调研上下文", "", *_table(["项目", "值"], rows)]


def render_fastmoss_evidence_document(dossier: Mapping[str, Any]) -> RenderedEvidenceDocument:
    """Render a complete dossier while isolating per-call renderer failures."""

    lines = ["# FastMoss 调研证据", "", *_context_markdown(dossier)]
    results: list[RenderedToolEvidence] = []
    for entry in dossier.get("tool_evidence") or []:
        if not isinstance(entry, dict):
            continue
        result = render_fastmoss_tool_evidence(entry)
        results.append(result)
        lines.extend(["", result.markdown])

    for title, key in (
        ("覆盖摘要", "coverage_summary"),
        ("程序派生事实", "derived_facts"),
        ("冲突", "conflicts"),
        ("限制", "limitations"),
        ("硬事实边界", "hard_fact_boundaries"),
    ):
        value = dossier.get(key)
        if value in (None, "", [], {}):
            continue
        semantic_value = _report_semantic_metadata(value, key)
        if semantic_value in (None, "", [], {}):
            continue
        lines.extend(["", f"## {title}", ""])
        generic = json_to_markdown(
            semantic_value, title=title, include_paths=False
        ).splitlines()[1:]
        lines.extend(line for line in generic if line.strip())

    markdown = "\n".join(lines).rstrip() + "\n"
    return RenderedEvidenceDocument(markdown=markdown, tool_results=results)


__all__ = [
    "EvidenceNode",
    "FASTMOSS_CURRENT_TOOL_NAMES",
    "FASTMOSS_RENDER_SPECS",
    "FASTMOSS_TOOL_PROFILE_GROUPS",
    "FASTMOSS_TOOL_TITLES",
    "RenderedEvidenceDocument",
    "RenderedToolEvidence",
    "ToolRenderSpec",
    "business_leaf_paths",
    "fastmoss_semantic_registry_diagnostics",
    "localize_semantic_value",
    "render_fastmoss_evidence_document",
    "render_fastmoss_tool_evidence",
]
