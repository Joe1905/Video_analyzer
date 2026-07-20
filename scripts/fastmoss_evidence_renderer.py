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
    name: ToolRenderSpec(name, profile, _entity_type_for_tool(name))
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
    "date_value": "统计日期",
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
    "page": "页码",
    "pagesize": "每页数量",
    "top_k": "候选数量",
    "keyword": "关键词",
    "search_rank": "搜索排名",
    "searches": "搜索量",
    "purchases": "购买量",
    "purchase_rate": "购买率",
    "conversion_rate": "转化率",
    "products": "商品数",
    "ad_products": "广告商品数",
    "supply_demand_ratio": "供需比",
    "monopoly_click_rate": "点击集中度",
    "title_density": "标题密度",
    "avg_price": "平均价格",
    "min_bid": "最低竞价",
    "max_bid": "最高竞价",
    "monthly_sales": "月销量",
    "monthly_revenue": "月销售额",
    "bsr": "BSR",
    "rating": "评分",
    "reviews": "评论数",
    "brand": "品牌",
    "seller_name": "卖家",
    "marketplace": "站点",
    "node_id_path": "类目节点路径",
    "node_name": "类目名称",
    "goods_count": "商品数量",
}
_SEMANTIC_FIELD_RE = re.compile(
    r"(?:^|_)(?:id|uid|name|title|nickname|region|country|currency|category|keyword|rank|score|"
    r"date|day|time|period|range|price|gmv|sale|sales|sold|unit|count|total|rate|ratio|share|"
    r"growth|follower|creator|video|live|ad|play|view|like|digg|comment|engagement|commission|"
    r"inventory|stock|status|type|brand|shop|product|quantity|amount|roas|spend|cpm|gpm|ipm|"
    r"rating|duration|listed|launch|text|content|description|desc|summary|snippet|average|window|"
    r"published|matched|source|shipping|managed|border|shelf|page|pagesize|query|top)(?:_|$)"
)


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
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value if value else '""'
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


def _known_field(key: str) -> bool:
    lowered = _normalized_field_key(key)
    return lowered in _FIELD_LABELS or bool(_SEMANTIC_FIELD_RE.search(lowered))


def _field_label(key: str) -> str:
    label = _FIELD_LABELS.get(key) or _FIELD_LABELS.get(_normalized_field_key(key))
    return f"{label}（{key}）" if label else key


def _normalized_field_key(key: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _dotted_field_label(key: str) -> str:
    parts = str(key).split(".")
    if len(parts) == 1:
        return _field_label(key)
    return " · ".join([*parts[:-1], _field_label(parts[-1])])


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
            return f"{key}={item}"
    for key in ("title", "name", "nickname"):
        if value.get(key):
            return f"{key}={value[key]}"
    return ""


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
        self.nodes: list[EvidenceNode] = []
        self.consumed: set[str] = set()
        self.unmapped: set[str] = set()
        self.excluded: set[str] = set()

    def render(self) -> RenderedToolEvidence:
        data = self.entry.get("business_data")
        all_paths = business_leaf_paths(data)
        source_ref = str(self.entry.get("source_ref") or "call:?")
        fence = self.entry.get("evidence_fence") if isinstance(self.entry.get("evidence_fence"), dict) else {}
        data_state = str(fence.get("data_state") or "").strip().lower()
        error = str(self.entry.get("error") or "").strip()
        lines = [f"## {_heading_text(source_ref)} · `{self.full_tool_name}`"]
        lines.extend(["", *self._scope_lines()])

        if error or data_state == "error":
            self.nodes.append(EvidenceNode("ErrorResult", "调用失败", "$.business_data"))
            lines.extend([
                "", "### 调用结果", "",
                f"本次调用失败，失败范围仅限上述对象和参数。错误信息：{error or '工具返回错误状态。'}",
            ])
            self.excluded.update(all_paths)
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
            self.excluded.update(all_paths)
        else:
            rendered = self._render_value(data, "$.business_data", 3, "业务结果")
            if rendered:
                lines.extend(["", *rendered])

        self.unmapped.update(all_paths - self.consumed - self.excluded)
        if self.unmapped:
            lines.extend(["", "### 未映射业务字段", ""])
            rows = []
            for path in sorted(self.unmapped):
                rows.append((path, self._value_at_path(data, path)))
            lines.extend(_table(["JSON路径", "原始值"], rows))

        conflicts = self.entry.get("scope_conflicts")
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
            empty=data_state == "empty",
        )
        if result.business_leaf_paths != result.consumed_paths | result.unmapped_paths | result.excluded_paths:
            raise ValueError(f"business field conservation failed for {self.tool_name}")
        if result.consumed_paths & result.unmapped_paths:
            raise ValueError(f"business field rendered twice for {self.tool_name}")
        return result

    def _scope_lines(self) -> list[str]:
        arguments = self.entry.get("arguments")
        fence = self.entry.get("evidence_fence") if isinstance(self.entry.get("evidence_fence"), dict) else {}
        rows: list[tuple[str, Any]] = []
        if isinstance(arguments, dict):
            rows.extend((f"参数 {key}", value) for key, value in self._flatten(arguments))
        for key, value in fence.items():
            if value not in (None, "", [], {}):
                rows.append((f"围栏 {key}", value))
        if not rows:
            return ["> 本次调用没有额外参数或围栏字段。"]
        return _table(
            ["调用范围", "值"],
            ((label, self._compact(value)) for label, value in rows),
        )

    def _flatten(self, value: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                rows.extend(self._flatten(item, name))
            else:
                rows.append((name, item))
        return rows

    @staticmethod
    def _compact(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return _scalar_text(value)

    def _render_value(self, value: Any, path: str, level: int, title: str) -> list[str]:
        if _scalar(value):
            kind = "NarrativeBlock" if isinstance(value, str) else "MetricGroup"
            self.nodes.append(EvidenceNode(kind, title, path))
            self.consumed.add(path)
            return [f"{'#' * min(level, 6)} {_heading_text(title)}", "", _scalar_text(value)]
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

        scalar_rows: list[tuple[str, Any, str]] = []
        nested: list[tuple[str, Any, str]] = []
        for key, item in value.items():
            child_path = _path(path, str(key))
            if _scalar(item):
                if _known_field(str(key)):
                    scalar_rows.append((_field_label(str(key)), item, str(key)))
                    self.consumed.add(child_path)
            elif isinstance(item, dict) and str(key).lower() not in _ENTITY_CONTAINER_KEYS:
                flattened, child_nested = self._flatten_dict_fields(
                    item, child_path, str(key)
                )
                scalar_rows.extend(flattened)
                nested.extend(child_nested)
            else:
                nested.append((str(key), item, child_path))
        if scalar_rows:
            lines.extend(["", *_table(["指标", "值", "原字段"], scalar_rows)])

        for key, item, child_path in nested:
            child_lines = self._render_value(item, child_path, level + 1, key)
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
            if _known_field(parent_key):
                rows = []
                for index, item in enumerate(value):
                    item_path = f"{path}[{index}]"
                    self.consumed.add(item_path)
                    rows.append((index + 1, item))
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
                lines.extend(["", *_table(["序号", *(_dotted_field_label(key) for key in primary)], rows)])
            if extra:
                supplemental: list[tuple[Any, ...]] = []
                for index, record in enumerate(records):
                    for key in extra:
                        flattened_record = flattened_records[index]
                        if key not in flattened_record:
                            continue
                        value_item, child_path = flattened_record[key]
                        self.consumed.add(child_path)
                        supplemental.append((index + 1, _dotted_field_label(key), value_item, key))
                if supplemental:
                    lines.extend(["", "补充指标：", "", *_table(
                        ["记录", "指标", "值", "原字段"], supplemental
                    )])
            for index, record_nested in enumerate(nested_by_record):
                for key, item, child_path in record_nested:
                    nested = self._render_value(item, child_path, level + 1, f"记录 {index + 1} · {key}")
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
    ) -> tuple[list[tuple[str, Any, str]], list[tuple[str, Any, str]]]:
        rows: list[tuple[str, Any, str]] = []
        nested: list[tuple[str, Any, str]] = []
        for key, item in value.items():
            key = str(key)
            child_path = _path(path, key)
            field_name = f"{prefix}.{key}" if prefix else key
            if _scalar(item):
                if _known_field(key):
                    rows.append((_dotted_field_label(field_name), item, field_name))
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
        for key, item in value.items():
            key = str(key)
            child_path = _path(path, key)
            field_name = f"{prefix}.{key}" if prefix else key
            if _scalar(item):
                if _known_field(key):
                    fields[field_name] = (item, child_path)
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
        # Conflict/boundary metadata is already program-authoritative.  Generic
        # Markdown keeps it complete without adding it to business conservation.
        rendered = json_to_markdown(value, title="边界详情", include_paths=False)
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
    """Render one call; fall back locally without dropping its source payload."""

    renderer = SemanticToolRenderer(entry)
    try:
        return renderer.render()
    except Exception:
        tool_name = unprefixed_tool_name(str(entry.get("tool_name") or "unknown"))
        data = entry.get("business_data")
        paths = business_leaf_paths(data)
        markdown = json_to_markdown(
            dict(entry),
            title=f"{entry.get('source_ref') or 'call:?'} · {entry.get('tool_name') or tool_name}",
            include_paths=True,
        ).rstrip()
        return RenderedToolEvidence(
            markdown=markdown,
            tool_name=tool_name,
            profile=FASTMOSS_RENDER_SPECS.get(
                tool_name, ToolRenderSpec(tool_name, PROFILE_GENERIC, "reference")
            ).profile,
            node_types=["GenericFallback"],
            business_leaf_paths=paths,
            unmapped_paths=paths,
            fallback=True,
        )


def _context_markdown(dossier: Mapping[str, Any]) -> list[str]:
    rows = [
        ("工作流", dossier.get("workflow") or "product"),
        ("报告日期", dossier.get("report_date") or ""),
        ("目标类目路径", json.dumps(dossier.get("target_category_path") or [], ensure_ascii=False)),
        ("分析目标", json.dumps(dossier.get("analysis_targets") or [], ensure_ascii=False)),
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
        lines.extend(["", f"## {title}", ""])
        generic = json_to_markdown(value, title=title, include_paths=False).splitlines()[1:]
        lines.extend(line for line in generic if line.strip())

    markdown = "\n".join(lines).rstrip() + "\n"
    return RenderedEvidenceDocument(markdown=markdown, tool_results=results)


__all__ = [
    "EvidenceNode",
    "FASTMOSS_CURRENT_TOOL_NAMES",
    "FASTMOSS_RENDER_SPECS",
    "FASTMOSS_TOOL_PROFILE_GROUPS",
    "RenderedEvidenceDocument",
    "RenderedToolEvidence",
    "ToolRenderSpec",
    "business_leaf_paths",
    "render_fastmoss_evidence_document",
    "render_fastmoss_tool_evidence",
]
