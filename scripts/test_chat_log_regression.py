#!/usr/bin/env python3
"""Replay completed commerce chats and compare new reports with stored baselines."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import web_app  # noqa: E402
from chat_session import Message  # noqa: E402


def load_samples(path: Path, limit: int) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    sessions = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for session in sessions:
        messages = session.get("messages") or []
        for index, message in enumerate(messages):
            if message.get("role") != "assistant" or not message.get("tool_results"):
                continue
            if not str(message.get("content") or "").strip():
                continue
            user_text = next((
                str(messages[previous].get("content") or "")
                for previous in range(index - 1, -1, -1)
                if messages[previous].get("role") == "user"
            ), str(session.get("title") or ""))
            candidates.append((session, user_text, message))
    candidates.sort(key=lambda item: str(item[0].get("updated_at") or ""), reverse=True)
    return candidates[:limit]


def replay_route(provider: str, user_text: str) -> dict[str, Any]:
    if provider == "fastmoss":
        trend = any(word in user_text.lower() for word in ("趋势", "新品", "爆卖", "trend", "new"))
        return {
            "intent": "fastmoss_product",
            "task_depth": "workflow",
            "playbook": "product",
            "dynamic_planner": True,
            "route_source": "llm",
            "research_task": {
                "objective": "trend_discovery" if trend else "product_research",
                "entity": "",
                "entity_type": "none",
                "scope": "cross_category" if trend else "single_market",
            },
        }
    discovery = any(word in user_text.lower() for word in (
        "选品", "爆品", "蓝海", "新品", "趋势", "opportunity", "trend",
    ))
    return {
        "intent": "product_research",
        "task_depth": "analysis",
        "dynamic_planner": True,
        "route_source": "llm",
        "research_task": {
            "objective": "opportunity_discovery",
            "entity": "" if discovery else user_text.strip(),
            "entity_type": "none" if discovery else "keyword",
            "scope": "cross_category" if discovery else "single_market",
        },
    }


def to_message(raw: dict[str, Any]) -> Message:
    return Message(
        id=str(raw.get("id") or "replay"),
        role="assistant",
        content=str(raw.get("content") or ""),
        attachments=raw.get("attachments"),
        tool_calls=raw.get("tool_calls") or [],
        tool_results=raw.get("tool_results") or [],
        status=str(raw.get("status") or "done"),
    )


def report_metrics(text: str) -> dict[str, Any]:
    numbers = set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", text))
    entities = set(re.findall(r"\bB0[A-Z0-9]{8}\b|\b\d{12,}\b", text, re.IGNORECASE))
    return {
        "chars": len(text),
        "headings": sum(1 for line in text.splitlines() if re.match(r"^#{1,6}\s+", line)),
        "table_rows": sum(1 for line in text.splitlines() if line.lstrip().startswith("|")),
        "numbers": numbers,
        "entities": {item.upper() for item in entities},
    }


def compare_reports(old: str, new: str, provider: str) -> dict[str, Any]:
    old_metrics = report_metrics(old)
    new_metrics = report_metrics(new)
    length_ratio = new_metrics["chars"] / max(1, old_metrics["chars"])
    numeric_recall = len(old_metrics["numbers"] & new_metrics["numbers"]) / max(1, len(old_metrics["numbers"]))
    entity_recall = len(old_metrics["entities"] & new_metrics["entities"]) / max(1, len(old_metrics["entities"]))
    expected_notice = (
        web_app.FASTMOSS_REPORT_NOTICE if provider == "fastmoss"
        else web_app.SELLERSPRITE_REPORT_NOTICE
    )
    failures: list[str] = []
    if not (0.60 <= length_ratio <= 2.50):
        failures.append("report_length_drift")
    if old_metrics["headings"] >= 4 and new_metrics["headings"] < max(2, old_metrics["headings"] // 3):
        failures.append("structure_regression")
    if len(old_metrics["numbers"]) >= 8 and numeric_recall < 0.25:
        failures.append("numeric_coverage_drift")
    if 0 < len(old_metrics["entities"]) <= 5 and entity_recall < 0.25:
        failures.append("entity_coverage_drift")
    if expected_notice not in new:
        failures.append("missing_programmatic_notice")
    if "报告模型暂时无法生成" in new or "Request failed" in new:
        failures.append("report_generation_failed")
    return {
        "passed": not failures,
        "failures": failures,
        "length_ratio": round(length_ratio, 3),
        "numeric_recall": round(numeric_recall, 3),
        "entity_recall": round(entity_recall, 3),
        "old": {key: value for key, value in old_metrics.items() if not isinstance(value, set)},
        "new": {key: value for key, value in new_metrics.items() if not isinstance(value, set)},
    }


def judge_material_deviation(
    old: str,
    new: str,
    api_key: str,
    api_url: str,
    model: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "system",
            "content": (
                "你是报告回归测试员。两份报告基于同一批工具证据。忽略措辞、排版和合理的分析顺序差异；"
                "只判断新报告是否出现明显功能错误、关键证据覆盖严重缩减、核心结论反转或新增无依据断言。"
                "返回 JSON：passed(boolean), severity(none|minor|major), reasons(array), coverage_comment(string)。"
                "只有实质性可用性退化才判 major/failed。"
            ),
        }, {
            "role": "user",
            "content": "旧报告：\n" + old + "\n\n新报告：\n" + new,
        }],
    }
    response = requests.post(
        api_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=180,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", str(content or ""), re.DOTALL)
    if not match:
        raise ValueError("usability judge did not return a JSON object")
    return json.loads(match.group(0), strict=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("fastmoss", "sellersprite"), required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "chat_log_regression")
    parser.add_argument("--skip-judge", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY")
    api_url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1")
    report_model = web_app.chat_report_model()
    judge_model = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for index, (session, user_text, raw_assistant) in enumerate(
        load_samples(args.sessions, args.limit), start=1
    ):
        assistant = to_message(raw_assistant)
        route = replay_route(args.provider, user_text)
        if args.provider == "fastmoss":
            new_report = web_app.synthesize_fastmoss_report_from_packet(
                assistant, user_text, route, requests,
                api_key, api_url, report_model,
            )
        else:
            new_report = web_app.synthesize_sellersprite_report_from_packet(
                assistant, user_text, route, requests,
                api_key, api_url, report_model,
            )
        old_report = str(raw_assistant.get("content") or "")
        metrics = compare_reports(old_report, new_report, args.provider)
        judge = {"passed": True, "severity": "skipped", "reasons": []}
        if not args.skip_judge:
            judge = judge_material_deviation(
                old_report, new_report, api_key, api_url, judge_model
            )
        passed = bool(metrics["passed"] and judge.get("passed") is not False and judge.get("severity") != "major")
        sample_id = str(session.get("id") or f"sample-{index}")
        output_path = args.output_dir / f"{args.provider}-{index}-{sample_id[-12:]}.md"
        output_path.write_text(new_report, encoding="utf-8")
        results.append({
            "session_id": sample_id,
            "updated_at": session.get("updated_at"),
            "question": user_text[:160],
            "tool_calls": len(assistant.tool_results or []),
            "metrics": metrics,
            "judge": judge,
            "passed": passed,
            "output": str(output_path),
        })

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
