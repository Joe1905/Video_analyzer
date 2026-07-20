#!/usr/bin/env python3
"""Replay one persisted FastMoss session with identical calls in each evidence format.

Generated reports are written under ``output/fastmoss_evidence_replay``.  The
script never re-executes MCP tools.  Model calls require ``--call-model`` so a
local format audit cannot accidentally consume API balance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import web_app  # noqa: E402


FORMATS = ("semantic", "generic", "json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay persisted FastMoss evidence without re-running tools"
    )
    parser.add_argument(
        "--sessions",
        default=str(ROOT / "data" / "fastmoss_mcp" / "chat_sessions.json"),
        help="persisted FastMoss chat_sessions.json",
    )
    parser.add_argument("--session-index", type=int, default=-1)
    parser.add_argument("--modes", nargs="+", choices=FORMATS, default=list(FORMATS))
    parser.add_argument(
        "--call-model",
        action="store_true",
        help="send each evidence format to the configured report model",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "output" / "fastmoss_evidence_replay"),
    )
    return parser


def _combined_assistant(session: dict[str, Any]) -> SimpleNamespace:
    assistant_messages = [
        message for message in session.get("messages") or []
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    return SimpleNamespace(
        tool_calls=[
            call for message in assistant_messages
            for call in (message.get("tool_calls") or [])
        ],
        tool_results=[
            result for message in assistant_messages
            for result in (message.get("tool_results") or [])
        ],
    )


def _user_text(session: dict[str, Any]) -> str:
    messages = [
        str(message.get("content") or "").strip()
        for message in session.get("messages") or []
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    messages = [message for message in messages if message]
    return messages[-1] if messages else "请根据已有证据生成完整调研报告"


def _route(session: dict[str, Any]) -> dict[str, Any]:
    first_user = next((
        str(message.get("content") or "").strip()
        for message in session.get("messages") or []
        if isinstance(message, dict) and message.get("role") == "user"
    ), "")
    return {"playbook": "product", "task_depth": "workflow", "entity": first_user}


def replay(args: argparse.Namespace) -> dict[str, Any]:
    sessions = json.loads(Path(args.sessions).read_text(encoding="utf-8"))
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("no persisted FastMoss sessions found")
    session = sessions[args.session_index]
    assistant = _combined_assistant(session)
    user_text = _user_text(session)
    route = _route(session)
    manifest = web_app.fastmoss_evidence_manifest(assistant, user_text, route)
    dossier = web_app.fastmoss_report_evidence_dossier(assistant, manifest, route)
    session_id = str(session.get("id") or f"session-{args.session_index}")
    output_dir = Path(args.output_dir) / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "session_id": session_id,
        "tool_call_count": len(assistant.tool_calls),
        "tool_result_count": len(assistant.tool_results),
        "evidence_envelope_count": manifest.get("evidence_envelope_count") or 0,
        "evidence_fact_count": manifest.get("evidence_fact_count") or 0,
        "modes": {},
    }
    old_mode = os.environ.get("FASTMOSS_REPORT_EVIDENCE_FORMAT")
    try:
        for mode in args.modes:
            os.environ["FASTMOSS_REPORT_EVIDENCE_FORMAT"] = mode
            evidence, stats = web_app.fastmoss_render_report_evidence(dossier)
            mode_summary: dict[str, Any] = {
                "evidence_chars": len(evidence),
                "render_stats": stats,
            }
            if args.call_model:
                api_key = str(os.getenv("DEEPSEEK_API_KEY") or "").strip()
                if not api_key:
                    raise RuntimeError("Missing DEEPSEEK_API_KEY")
                original_finalize = web_app.finalize_fastmoss_answer
                web_app.finalize_fastmoss_answer = lambda draft, *_args, **_kwargs: draft
                try:
                    report = web_app.synthesize_fastmoss_report_from_packet(
                        assistant,
                        user_text,
                        route,
                        requests,
                        api_key,
                        os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1"),
                        os.getenv("DEEPSEEK_REPORT_MODEL", "deepseek-v4-pro"),
                    )
                finally:
                    web_app.finalize_fastmoss_answer = original_finalize
                report_path = output_dir / f"{mode}.md"
                report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
                mode_summary.update({
                    "report_chars": len(report),
                    "report_headings": sum(
                        1 for line in report.splitlines() if line.lstrip().startswith("#")
                    ),
                    "report_table_rows": sum(
                        1 for line in report.splitlines() if line.startswith("|")
                    ),
                    "report_path": str(report_path),
                })
            summary["modes"][mode] = mode_summary
    finally:
        if old_mode is None:
            os.environ.pop("FASTMOSS_REPORT_EVIDENCE_FORMAT", None)
        else:
            os.environ["FASTMOSS_REPORT_EVIDENCE_FORMAT"] = old_mode

    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary["summary_path"] = str(summary_path)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = replay(args)
    except Exception as exc:
        print(f"FastMoss evidence replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
