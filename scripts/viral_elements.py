#!/usr/bin/env python3
"""Evidence-backed viral element reviews and script generation."""

from __future__ import annotations

import json
import os
import sqlite3
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ELEMENT_DEFS = [
    ("positioning", "type", "视频类型"),
    ("positioning", "category", "品类"),
    ("positioning", "product", "产品"),
    ("positioning", "duration", "时长"),
    ("content", "topic", "选题"),
    ("content", "emotional_tone", "情绪基调"),
    ("content", "pov", "POV"),
    ("content", "characters", "人物"),
    ("content", "scene", "场景"),
    ("structure", "hook", "Hook"),
    ("structure", "shots_and_selling_points", "镜头与卖点"),
    ("structure", "material_structure", "素材结构"),
    ("structure", "cta", "CTA"),
    ("expression", "copy", "文案"),
    ("expression", "on_screen_text", "屏幕字幕"),
    ("expression", "visual_style", "视觉风格"),
    ("expression", "bgm", "BGM"),
    ("expression", "voiceover", "人声"),
]
GROUP_LABELS = {
    "positioning": "定位",
    "content": "内容",
    "structure": "结构",
    "expression": "表达",
}

ELEMENT_ANALYSIS_RULES = """【模式A：TikTok采集与拆解】
- 只根据真实画面、声音和已有分析证据输出，禁止分析 caption、hashtag、账号名、评论区和平台按钮。
- 不猜品牌、参数、价格、功效或人物身份；无法可靠识别时写“存在但无法可靠识别”，并列入缺失项。
- 18 个元素不得为空；确实不存在时写“无该元素｜实际替代方式”。
- 类型和品类必须保持口径稳定，多个内容使用“｜”分隔。
- 输出必须可供人工审核，AI 不得代替人工把元素设为通过。
- 类型只能从以下27项选一：带货口播视频、狗血剧情视频、开箱视频、前后对比视频、商品视频、生活场景融入型视频、挑战赛趋势借势型视频、痛点解决型视频、网红带货视频、值得买测评视频、使用教程演示视频、UGC真实体验视频、清单合集视频、竞品对比视频、实验测试视频、ASMR解压视频、街访路人反应视频、直播切片视频、知识科普视频、热点事件借势视频、悬念反转视频、用户评价见证视频、避坑纠错视频、制作过程幕后视频、定格动画创意视频、混剪合集视频、其他。
- 品类只能从以下23项选一：美妆个护、个护家清、服饰内衣、鞋靴箱包、珠宝配饰、家居日用、家具家装、厨具餐饮、家用电器、3C数码、食品饮料、健康保健、母婴用品、宠物用品、运动健身、户外用品、汽车用品、玩具兴趣、办公文具、工具工业品、园艺用品、旅行用品、其他。
- 产品只写画面中实际展示或主推的具体商品，不猜品牌、型号或参数；时长写实际总时长，例如“12秒”。
- 选题写“谁在什么情况下做了什么，或用什么解决什么问题”；情绪基调写主要情绪和情绪变化；POV写拍摄者或叙事者身份。
- 人物只写数量、可见身份、外貌服装和动作，不猜国籍、族裔、职业或关系。
- 素材结构按时间线写开场、发展、高潮和收尾；Hook写钩子类型、前3秒画面、声音或字幕及停留原因。
- 文案只写独立口播、对白和旁白，BGM歌词不算；屏幕字幕只写后期叠加文字、贴纸和表情。
- 场景写地点、室内外、时间天气、背景和道具；视觉风格写色调、光线、构图、滤镜及UGC或广告质感。
- 镜头与卖点按时间写景别、机位/运镜、动作、转场和实际卖点；CTA写真实话术、时间和位置，缺失时写“无显式CTA｜实际收尾机制”。
- BGM写曲风、速度、情绪和节拍，缺失时写“无BGM｜实际声音主体”；人声写人数、语速、音量和语气。
"""

SCRIPT_COMPOSER_RULES = """【模式B：生成三版脚本】
- 产品、卖点、目标人群为必填；产品固定，类型、品类和产品不得混淆。
- 只使用状态已审核/已通过的有效元素；忽略空值、null、公式错误和“无法识别”。
- 每个元素独立建立候选池，并按需求匹配40、用途匹配30、可执行性20、热度10评分；每池从前5名选择。
- 同一来源原片每个脚本版本最多贡献2项元素；来源不足时允许跨品类迁移或AI原创，但必须标记。
- 检查人物、场景、情绪、Hook、字幕、镜头、CTA、时间轴和卖点的一致性；不照抄原片，不编造产品事实。
- 同时生成V1稳妥转化、V2平衡创意、V3探索突破；相邻版本至少改变Hook、POV、素材结构、场景、视觉风格、CTA中的3项。
- 用户指定时长时三版严格遵守；未指定时各版可独立确定，并以V1时长作为记录时长。
- 每版时间轴连续，所有镜头时长之和必须等于总时长；每个卖点至少进入一个镜头。
- 每版包含中文策略、人物/场景设定、英文口播、英文字幕、英文CTA、分镜表和独立英文视频提示词。
- source_elements 只能列出该版本实际使用的元素记录，格式为“来源文件:key”；原创项需明确标记“AI原创:key”。
- 只生成V1/V2/V3，不生成第四版；不得覆盖人工填写的审批人、审批意见、最终采用版本或需修改/已通过状态。
"""


class ViralElementError(RuntimeError):
    pass


class ViralElementStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "viral_elements.sqlite"
        data_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS viral_reviews (
                    filename TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS viral_scripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    brief_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def get_review(self, filename: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json, created_at, updated_at FROM viral_reviews WHERE filename = ?",
                (filename,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload.update({"created_at": row["created_at"], "updated_at": row["updated_at"]})
        return payload

    def save_review(self, filename: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO viral_reviews(filename, payload_json, created_at, updated_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at""",
                (filename, json.dumps(payload, ensure_ascii=False), now, now),
            )
            conn.commit()
        return self.get_review(filename) or payload

    def save_scripts(self, filename: str, brief: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        payload = dict(payload)
        payload.setdefault("workflow", {
            "status": "待审核", "reviewer": "", "approval_comment": "",
            "final_version": "", "last_modified_by": "system", "updated_at": now,
        })
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO viral_scripts(filename, brief_json, payload_json, created_at) VALUES(?, ?, ?, ?)",
                (filename, json.dumps(brief, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), now),
            )
            script_id = cursor.lastrowid
            cursor.close()
            conn.commit()
        return {"id": script_id, "filename": filename, "brief": brief, "scripts": payload, "created_at": now}

    def update_script_approval(self, script_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "待审核").strip()
        if status not in {"待审核", "需修改", "已通过"}:
            raise ViralElementError("协作状态必须是待审核、需修改或已通过")
        final_version = str(payload.get("final_version") or "").strip().upper()
        if final_version not in {"", "V1", "V2", "V3"}:
            raise ViralElementError("最终采用版本必须是 V1、V2 或 V3")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT filename, brief_json, payload_json, created_at FROM viral_scripts WHERE id=?", (script_id,)).fetchone()
            if not row:
                raise ViralElementError("脚本记录不存在")
            scripts = json.loads(row["payload_json"])
            now = datetime.now(timezone.utc).isoformat()
            scripts["workflow"] = {
                "status": status,
                "reviewer": str(payload.get("reviewer") or "").strip()[:120],
                "approval_comment": str(payload.get("approval_comment") or "").strip()[:4000],
                "final_version": final_version,
                "last_modified_by": str(payload.get("last_modified_by") or "人工审核").strip()[:120],
                "updated_at": now,
            }
            conn.execute("UPDATE viral_scripts SET payload_json=? WHERE id=?", (json.dumps(scripts, ensure_ascii=False), script_id))
            conn.commit()
        return {"id": script_id, "filename": row["filename"], "brief": json.loads(row["brief_json"]),
                "scripts": scripts, "created_at": row["created_at"]}

    def latest_scripts(self, filename: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, brief_json, payload_json, created_at FROM viral_scripts WHERE filename=? ORDER BY id DESC LIMIT 1",
                (filename,),
            ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "filename": filename, "brief": json.loads(row["brief_json"]),
                "scripts": json.loads(row["payload_json"]), "created_at": row["created_at"]}

    def list_library(self, approved_only: bool = True, limit: int = 300) -> list[dict[str, Any]]:
        """Return reusable elements across videos without exposing SQLite details to the UI."""
        limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT filename, payload_json, updated_at FROM viral_reviews ORDER BY updated_at DESC"
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            review = json.loads(row["payload_json"])
            for element in review.get("elements", []):
                if approved_only and not element.get("approved"):
                    continue
                items.append({
                    **element,
                    "filename": row["filename"],
                    "review_summary": review.get("summary", ""),
                    "heat_score": review.get("heat_score"),
                    "updated_at": row["updated_at"],
                })
                if len(items) >= limit:
                    return items
        return items


def _model_json(prompt: str, max_tokens: int = 6000) -> dict[str, Any]:
    from deepseek_postprocess import call_deepseek, extract_content, parse_json_content

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ViralElementError("服务器未配置 DEEPSEEK_API_KEY")
    response = call_deepseek(
        api_key,
        prompt,
        os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions"),
        os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash"),
        max_tokens,
        reasoning_effort="disabled",
    )
    return parse_json_content(extract_content(response))


def _compact_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": source.get("summary"),
        "metadata": source.get("metadata"),
        "transcript": source.get("transcript"),
        "timeline": source.get("timeline"),
        "visual_evidence": source.get("visual_evidence"),
        "video_description": source.get("video_description"),
    }


def analyze_elements(filename: str, source: dict[str, Any]) -> dict[str, Any]:
    schema = [{"group": g, "key": k, "label": label} for g, k, label in ELEMENT_DEFS]
    prompt = f"""你是短视频创意拆解分析师。执行以下从豆包工作流程迁移的规则：
{ELEMENT_ANALYSIS_RULES}
只根据给定的视频分析证据，提取恰好 18 个元素。
证据不足时 value 写“存在但无法可靠识别”，confidence 不高于 0.3，并把元素名加入 missing_items。
返回严格 JSON：{{"summary":"一句话总结","elements":[{{"group":"positioning","key":"type","label":"视频类型","value":"...","confidence":0.0,"evidence":"具体依据","time_range":"00:00-00:03","approved":false}}]}}。
elements 必须严格按下面 schema 顺序、每项一次；confidence 范围 0-1；time_range 无法确定时为空字符串。
schema={json.dumps(schema, ensure_ascii=False)}
视频分析={json.dumps(_compact_source(source), ensure_ascii=False)}"""
    result = _model_json(prompt)
    by_key = {str(item.get("key")): item for item in result.get("elements", []) if isinstance(item, dict)}
    elements = []
    for group, key, label in ELEMENT_DEFS:
        item = by_key.get(key, {})
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        elements.append({
            "group": group, "group_label": GROUP_LABELS[group], "key": key, "label": label,
            "value": str(item.get("value") or "存在但无法可靠识别")[:4000], "confidence": confidence,
            "evidence": str(item.get("evidence") or "")[:4000],
            "time_range": str(item.get("time_range") or "")[:80], "approved": bool(item.get("approved", False)),
        })
    metrics = _source_metrics(source)
    return {"filename": filename, "summary": str(result.get("summary") or ""), "elements": elements,
            "source_metrics": metrics, "heat_score": _heat_score(metrics),
            "source_channel": str(source.get("source_channel") or "B"),
            "review_status": "待审核", "schema_version": 3}


def _source_metrics(source: dict[str, Any]) -> dict[str, int]:
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    social = source.get("social_context") if isinstance(source.get("social_context"), dict) else {}
    merged = {**metadata, **social}
    aliases = {
        "views": ("views", "view_count", "play_count", "plays"),
        "likes": ("likes", "like_count", "digg_count"),
        "comments": ("comments", "comment_count"),
        "favorites": ("favorites", "favorite_count", "collect_count", "saves"),
        "shares": ("shares", "share_count"),
    }
    result: dict[str, int] = {}
    for key, names in aliases.items():
        value = next((merged.get(name) for name in names if merged.get(name) is not None), 0)
        try:
            result[key] = max(0, int(float(str(value).replace(",", ""))))
        except (TypeError, ValueError):
            result[key] = 0
    return result


def _heat_score(metrics: dict[str, int]) -> float | None:
    views = metrics.get("views", 0)
    if not views:
        return None
    weighted = metrics.get("likes", 0) + metrics.get("comments", 0) * 3
    weighted += metrics.get("favorites", 0) * 2
    return round(weighted / views * 100, 2)


def validate_review(payload: dict[str, Any]) -> dict[str, Any]:
    filename = Path(str(payload.get("filename") or "")).name
    incoming = payload.get("elements")
    if not filename or not isinstance(incoming, list):
        raise ViralElementError("filename 和 elements 必填")
    by_key = {str(item.get("key")): item for item in incoming if isinstance(item, dict)}
    elements = []
    for group, key, label in ELEMENT_DEFS:
        item = by_key.get(key, {})
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        elements.append({"group": group, "group_label": GROUP_LABELS[group], "key": key, "label": label,
                         "value": str(item.get("value") or "存在但无法可靠识别")[:4000], "confidence": confidence,
                         "evidence": str(item.get("evidence") or "")[:4000],
                         "time_range": str(item.get("time_range") or "")[:80], "approved": bool(item.get("approved"))})
    approved_count = sum(bool(item.get("approved")) for item in elements)
    metrics = payload.get("source_metrics") if isinstance(payload.get("source_metrics"), dict) else {}
    heat_score = payload.get("heat_score")
    try:
        heat_score = None if heat_score in (None, "") else round(float(heat_score), 2)
    except (TypeError, ValueError):
        heat_score = None
    return {"filename": filename, "summary": str(payload.get("summary") or "")[:4000], "elements": elements,
            "source_metrics": metrics, "heat_score": heat_score,
            "source_channel": str(payload.get("source_channel") or "B")[:1],
            "review_status": "已审核" if approved_count else "待审核", "schema_version": 3}


def generate_scripts(filename: str, review: dict[str, Any], brief: dict[str, Any],
                     library: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    required = {"product": "产品名称", "selling_points": "核心卖点", "audience": "目标人群"}
    cleaned = {key: str(brief.get(key) or "").strip()[:4000] for key in required}
    cleaned["duration"] = str(brief.get("duration") or "").strip()[:80]
    missing = [label for key, label in required.items() if not cleaned[key]]
    if missing:
        raise ViralElementError("缺少：" + "、".join(missing))
    approved = [item for item in review.get("elements", []) if item.get("approved")]
    if not approved:
        raise ViralElementError("请至少审核通过一个元素后再生成脚本")
    candidate_elements = [item for item in (library or approved) if item.get("approved")]
    prompt = f"""你是短视频转化脚本总监。执行以下从 tiktok-viral-script-composer 工作提示词迁移的规则：
{SCRIPT_COMPOSER_RULES}
根据产品 Brief 和候选元素生成三个差异明显、可直接拍摄的脚本版本。产品事实只能来自 Brief，不能从参考元素继承。
每版必须返回 strategy（中文）、hook、duration、shots（数组，每项含 time_range、visual、voiceover、on_screen_text、selling_point）、cta、video_prompt（独立完整英文提示词）、source_elements（实际使用的 key 数组）、risks（数组）。
严格返回 JSON：{{"versions":[{{"id":"V1","name":"稳妥转化",...}},...]}}，不要 Markdown。
Brief={json.dumps(cleaned, ensure_ascii=False)}
当前视频已审核元素={json.dumps(approved, ensure_ascii=False)}
跨视频候选元素库={json.dumps(candidate_elements[:300], ensure_ascii=False)}"""
    result = _model_json(prompt, max_tokens=8000)
    errors = validate_generated_scripts(result, cleaned)
    if errors:
        repair_prompt = prompt + "\n上次输出存在以下错误，请修复后重新返回完整 JSON：\n- " + "\n- ".join(errors)
        result = _model_json(repair_prompt, max_tokens=8000)
        errors = validate_generated_scripts(result, cleaned)
        if errors:
            raise ViralElementError("三版脚本校验失败：" + "；".join(errors[:8]))
    versions = result["versions"]
    return {"versions": versions, "brief": cleaned, "filename": filename}


def _seconds(value: Any) -> int | None:
    text = str(value or "").strip()
    match = re.search(r"(?:(\d+):)?(\d+)(?:秒|s)?$", text, re.I)
    if not match:
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2))


def _time_range(value: Any) -> tuple[int, int] | None:
    parts = re.split(r"\s*[-–—~至]\s*", str(value or "").strip())
    if len(parts) != 2:
        return None
    start, end = _seconds(parts[0]), _seconds(parts[1])
    return (start, end) if start is not None and end is not None else None


def validate_generated_scripts(result: dict[str, Any], brief: dict[str, str]) -> list[str]:
    versions = result.get("versions")
    if not isinstance(versions, list) or len(versions) != 3:
        return ["必须恰好返回 V1、V2、V3 三版"]
    errors: list[str] = []
    if [str(item.get("id") or "").upper() for item in versions if isinstance(item, dict)] != ["V1", "V2", "V3"]:
        errors.append("版本顺序必须是 V1、V2、V3")
    for version in versions:
        if not isinstance(version, dict):
            errors.append("版本必须是对象")
            continue
        version_id = str(version.get("id") or "版本")
        for key in ("strategy", "hook", "duration", "cta", "video_prompt"):
            if not str(version.get(key) or "").strip():
                errors.append(f"{version_id} 缺少 {key}")
        shots = version.get("shots")
        if not isinstance(shots, list) or not shots:
            errors.append(f"{version_id} 缺少分镜")
            continue
        previous_end = 0
        for index, shot in enumerate(shots, 1):
            time_range = _time_range(shot.get("time_range") if isinstance(shot, dict) else "")
            if not time_range:
                errors.append(f"{version_id} 第{index}镜时间格式无效")
                continue
            start, end = time_range
            if start != previous_end or end <= start:
                errors.append(f"{version_id} 第{index}镜时间轴不连续")
            previous_end = end
        duration = _seconds(version.get("duration"))
        if duration is not None and previous_end != duration:
            errors.append(f"{version_id} 镜头总时长与 duration 不一致")
        if not isinstance(version.get("source_elements"), list):
            errors.append(f"{version_id} 缺少 source_elements")
    return errors
