#!/usr/bin/env python3
"""PDF-compatible Feishu Bitable synchronization for the viral workflow."""

from __future__ import annotations

import json
import os
from typing import Any

from feishu_capabilities import FeishuCapabilityClient
from viral_elements import ELEMENT_DEFS, ViralElementStore


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _record_id(result: dict[str, Any]) -> str:
    record = result.get("record") if isinstance(result.get("record"), dict) else {}
    return str(result.get("recordId") or result.get("record_id") or record.get("record_id") or record.get("recordId") or "")


def _person(open_id: str) -> list[dict[str, str]] | str:
    return [{"id": open_id}] if open_id else ""


def review_fields(review: dict[str, Any], owner: str = "", owner_id: str = "",
                  video_base_url: str = "") -> dict[str, Any]:
    metrics = review.get("source_metrics") if isinstance(review.get("source_metrics"), dict) else {}
    elements = {str(item.get("key")): item for item in review.get("elements", []) if isinstance(item, dict)}
    raw = {item[2]: _text(elements.get(item[1], {}).get("value")) for item in ELEMENT_DEFS}
    evidence = {item[2]: {"value": elements.get(item[1], {}).get("value", ""),
                           "confidence": elements.get(item[1], {}).get("confidence", 0),
                           "evidence": elements.get(item[1], {}).get("evidence", ""),
                           "time_range": elements.get(item[1], {}).get("time_range", "")}
                for item in ELEMENT_DEFS}
    channel = str(review.get("source_channel") or "B").upper()
    model_json = _text({**raw, "缺失项": [name for name, value in raw.items() if not value]})
    fields: dict[str, Any] = {
        "本地文件ID": review.get("filename", ""),
        "播放量": metrics.get("views", 0), "点赞量": metrics.get("likes", 0),
        "收藏量": metrics.get("favorites", 0), "评论量": metrics.get("comments", 0),
        "内容.模型输出A": model_json if channel == "A" else "",
        "豆包Work.模型输出B": model_json if channel != "A" else "",
        "最终模型输出": model_json, "状态": "已解析", "热度分": review.get("heat_score") or 0,
        "采集状态": "已完成", "负责人": _person(owner_id) or owner,
        "审核人": _person(owner_id) if owner_id and review.get("reviewer") == owner else "",
        "元素审核状态": review.get("review_status", "待审核"),
        "元素审核意见": review.get("review_comment", ""), "元素证据": _text(evidence),
    }
    fields.update(raw)
    if review.get("source_url"):
        fields["链接"] = review["source_url"]
    filename = str(review.get("filename") or "").strip()
    if video_base_url and filename:
        from urllib.parse import quote
        fields["对标视频"] = f"{video_base_url.rstrip('/')}/{quote(filename)}"
    return fields


def _version_text(version: dict[str, Any]) -> str:
    return _text({key: version.get(key) for key in ("strategy", "hook", "duration", "shots", "cta", "risks")})


def script_fields(saved: dict[str, Any], review: dict[str, Any] | None = None, owner: str = "",
                  owner_id: str = "", source_record_ids: list[str] | None = None) -> dict[str, Any]:
    brief = saved.get("brief") if isinstance(saved.get("brief"), dict) else {}
    payload = saved.get("scripts") if isinstance(saved.get("scripts"), dict) else {}
    workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
    versions = {str(item.get("id")): item for item in payload.get("versions", []) if isinstance(item, dict)}
    elements = {str(item.get("key")): item.get("value", "") for item in (review or {}).get("elements", []) if isinstance(item, dict)}
    source_elements = sorted({str(value) for version in versions.values() for value in version.get("source_elements", [])})
    fields: dict[str, Any] = {
        "产品": brief.get("product", ""), "卖点": brief.get("selling_points", ""),
        "目标人群": brief.get("audience", ""), "补充需求": brief.get("supplemental_requirements", ""),
        "类型": elements.get("type", ""), "品类": elements.get("category", ""),
        "时长": (versions.get("V1") or {}).get("duration") or brief.get("duration", ""),
        "脚本负责人": _person(owner_id) or owner,
        "审核人": _person(owner_id) if owner_id and workflow.get("reviewer") == owner else "",
        "协作状态": workflow.get("status", "待审核"), "审核意见": workflow.get("approval_comment", ""),
        "最终采用版本": workflow.get("final_version", ""),
        "采用元素": source_record_ids if source_record_ids is not None else "\n".join(source_elements),
        "脚本记录ID": saved.get("id", ""), "最后变更时间": workflow.get("updated_at") or saved.get("created_at", ""),
    }
    for key, label in (("topic", "选题"), ("emotional_tone", "情绪基调"), ("pov", "POV"),
                       ("characters", "人物"), ("material_structure", "素材结构"), ("hook", "Hook"),
                       ("copy", "文案"), ("on_screen_text", "屏幕字幕"), ("scene", "场景"),
                       ("visual_style", "视觉风格"), ("shots_and_selling_points", "镜头与卖点"),
                       ("cta", "CTA"), ("bgm", "BGM"), ("voiceover", "人声")):
        fields[label] = elements.get(key, "")
    for version_id in ("V1", "V2", "V3"):
        version = versions.get(version_id) or {}
        fields[f"{version_id}脚本"] = _version_text(version)
        fields[f"{version_id}视频提示词"] = version.get("video_prompt", "")
    return fields


class ViralFeishuSync:
    def __init__(self, store: ViralElementStore, client: FeishuCapabilityClient):
        self.store = store
        self.client = client
        self.elements_url = os.getenv("VIRAL_FEISHU_ELEMENTS_URL", "").strip()
        self.scripts_url = os.getenv("VIRAL_FEISHU_SCRIPTS_URL", "").strip()
        self.owner = os.getenv("VIRAL_FEISHU_OWNER", "刘鹏飞").strip()
        self.owner_id = os.getenv("VIRAL_FEISHU_OWNER_ID", "").strip()
        self.video_base_url = os.getenv("VIRAL_FEISHU_VIDEO_BASE_URL", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.elements_url and self.scripts_url)

    def _sync(self, entity_type: str, local_key: str, table_url: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not table_url:
            return {"enabled": False, "status": "disabled"}
        link = self.store.get_feishu_link(entity_type, local_key) or {}
        payload = {"url": table_url, "fields": fields}
        try:
            if link.get("record_id"):
                payload["recordId"] = link["record_id"]
                self.client.update_bitable_record(payload)
                record_id, action = link["record_id"], "updated"
            else:
                result = self.client.create_bitable_record(payload)
                record_id = _record_id(result)
                if not record_id:
                    raise RuntimeError("飞书创建记录成功但未返回 record_id")
                action = "created"
            state = self.store.save_feishu_link(entity_type, local_key, table_url, record_id)
            return {"enabled": True, "status": "synced", "action": action, **state}
        except Exception as exc:
            self.store.save_feishu_link(entity_type, local_key, table_url, error=str(exc))
            raise

    def sync_review(self, review: dict[str, Any]) -> dict[str, Any]:
        return self._sync("review", str(review.get("filename") or ""), self.elements_url,
                          review_fields(review, self.owner, self.owner_id, self.video_base_url))

    def sync_scripts(self, saved: dict[str, Any], review: dict[str, Any] | None = None) -> dict[str, Any]:
        source_ids: list[str] = []
        payload = saved.get("scripts") if isinstance(saved.get("scripts"), dict) else {}
        for version in payload.get("versions", []):
            if not isinstance(version, dict):
                continue
            for source in version.get("source_elements", []):
                filename = str(source).split(":", 1)[0]
                link = self.store.get_feishu_link("review", filename) or {}
                record_id = str(link.get("record_id") or "")
                if record_id and record_id not in source_ids:
                    source_ids.append(record_id)
        return self._sync("scripts", str(saved.get("id") or ""), self.scripts_url,
                          script_fields(saved, review, self.owner, self.owner_id, source_ids))
