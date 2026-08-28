#!/usr/bin/env python3
"""Deterministic backend contracts for the AI chat scroll regression flow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat_session import ChatStore, Message, Session
import web_app


class ParameterOnlyHandler:
    def __init__(self, value: str = "", port: int = 4004) -> None:
        self.path = f"/api/chat/ask?{web_app.UI_CHAT_SCROLL_TEST_QUERY}={value}"
        self.server = SimpleNamespace(server_port=port)


class RecordingStore(ChatStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.events: list[tuple[str, dict]] = []

    def broadcast(self, session_id: str, event: str, data: dict) -> None:
        del session_id
        snapshot = {
            "messageId": data.get("messageId"),
            "content": data.get("content"),
            "tool_calls": list(data.get("tool_calls") or []),
            "tool_results": list(data.get("tool_results") or []),
        }
        self.events.append((event, snapshot))


class ChatScrollRegressionTest(unittest.TestCase):
    def test_parameter_requires_exact_scenario_and_port_4004(self) -> None:
        handler = ParameterOnlyHandler(web_app.UI_CHAT_SCROLL_TEST_SCENARIO)
        self.assertTrue(web_app.has_ui_chat_scroll_test_parameter(handler))
        self.assertTrue(web_app.is_ui_chat_scroll_test_request(handler))
        self.assertFalse(
            web_app.is_ui_chat_scroll_test_request(
                ParameterOnlyHandler(web_app.UI_CHAT_SCROLL_TEST_SCENARIO, port=4002)
            )
        )
        self.assertFalse(
            web_app.is_ui_chat_scroll_test_request(ParameterOnlyHandler("wrong-scenario"))
        )

    def test_setup_clones_only_one_source_session_and_cleans_stale_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ChatStore(Path(temp_dir) / "chat_sessions.json")
            source = Session(
                id=web_app.chat_session_key(
                    "amazon", web_app.UI_CHAT_SCROLL_TEST_SOURCE_SESSION
                ),
                title="长会话",
                messages=[
                    Message(id="u1", role="user", content="测试问题"),
                    Message(
                        id="a1",
                        role="assistant",
                        content="测试回答",
                        tool_calls=[{
                            "id": "source-tool",
                            "type": "function",
                            "function": {"name": "system__source", "arguments": "{}"},
                        }],
                    ),
                ],
            )
            first_id, first, cleaned = web_app.clone_ui_chat_scroll_test_session(
                store, source
            )
            self.assertEqual(cleaned, 0)
            self.assertTrue(first_id.startswith(web_app.UI_CHAT_SCROLL_TEST_SESSION_PREFIX))
            self.assertEqual(len(first.messages), 2)
            self.assertIsNot(first.messages[0], source.messages[0])

            second_id, second, cleaned = web_app.clone_ui_chat_scroll_test_session(
                store, source
            )
            self.assertEqual(cleaned, 1)
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(len(second.messages), 2)
            stored_prefix = web_app.chat_session_key(
                "amazon", web_app.UI_CHAT_SCROLL_TEST_SESSION_PREFIX
            )
            self.assertEqual(
                sum(key.startswith(stored_prefix) for key in store.sessions), 1
            )

    def test_setup_accepts_legacy_sellersprite_prefixed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ChatStore(Path(temp_dir) / "chat_sessions.json")
            legacy_source = Session(
                id=(
                    "sellersprite__"
                    + web_app.UI_CHAT_SCROLL_TEST_SOURCE_SESSION
                ),
                title="旧版长会话",
                messages=[Message(id="u1", role="user", content="测试问题")],
            )
            store.sessions[legacy_source.id] = legacy_source

            public_id, cloned, cleaned = (
                web_app.clone_ui_chat_scroll_test_session(store)
            )

            self.assertEqual(cleaned, 0)
            self.assertTrue(
                public_id.startswith(web_app.UI_CHAT_SCROLL_TEST_SESSION_PREFIX)
            )
            self.assertEqual(cloned.messages[0].content, "测试问题")
            self.assertIsNot(cloned.messages[0], legacy_source.messages[0])

    def test_setup_accepts_source_selected_by_exact_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ChatStore(Path(temp_dir) / "chat_sessions.json")
            titled_source = Session(
                id="amazon__stored-session-id",
                title=web_app.UI_CHAT_SCROLL_TEST_SOURCE_SESSION,
                messages=[Message(id="u1", role="user", content="长对话问题")],
            )
            store.sessions[titled_source.id] = titled_source

            _public_id, cloned, _cleaned = (
                web_app.clone_ui_chat_scroll_test_session(store)
            )

            self.assertEqual(cloned.messages[0].content, "长对话问题")
            self.assertIsNot(cloned.messages[0], titled_source.messages[0])

    def test_setup_uses_synthetic_history_when_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ChatStore(Path(temp_dir) / "chat_sessions.json")

            public_id, cloned, cleaned = web_app.clone_ui_chat_scroll_test_session(store)

            self.assertEqual(cleaned, 0)
            self.assertTrue(public_id.startswith(web_app.UI_CHAT_SCROLL_TEST_SESSION_PREFIX))
            self.assertEqual(cloned.title, "UI 双滚动条回归")
            self.assertGreaterEqual(len(cloned.messages), 12)
            self.assertEqual({message.role for message in cloned.messages}, {"user", "assistant"})
            self.assertGreater(
                sum(len(message.content) for message in cloned.messages),
                10_000,
            )

    def test_sequence_emits_ten_fake_calls_without_real_llm_or_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RecordingStore(Path(temp_dir) / "chat_sessions.json")
            session = Session(id="amazon__ui-scroll-regression-test")
            assistant = Message(
                id="assistant-test",
                role="assistant",
                content="",
                status="pending",
            )
            session.messages.append(assistant)

            with mock.patch.object(
                web_app, "run_chat_deepseek", side_effect=AssertionError("LLM called")
            ), mock.patch.object(
                web_app,
                "execute_prefixed_tool",
                side_effect=AssertionError("real tool called"),
            ):
                web_app.run_ui_chat_scroll_test_sequence(
                    store,
                    session,
                    assistant,
                    sleep_fn=lambda _seconds: None,
                    timing_scale=0,
                )

            self.assertEqual(assistant.status, "done")
            self.assertEqual(len(assistant.tool_calls or []), 10)
            self.assertEqual(len(assistant.tool_results or []), 10)
            self.assertEqual(
                [item["function"]["name"] for item in assistant.tool_calls or []],
                ["system__ui_scroll_probe"] * 10,
            )
            update_events = [payload for event, payload in store.events if event == "update"]
            done_events = [payload for event, payload in store.events if event == "done"]
            self.assertEqual(len(update_events), 23)
            self.assertEqual(len(done_events), 1)
            counts = [len(payload["tool_calls"]) for payload in update_events]
            self.assertGreaterEqual(
                sum(left == right == 8 for left, right in zip(counts, counts[1:])),
                3,
            )
            self.assertEqual(done_events[0]["content"], assistant.content)


if __name__ == "__main__":
    unittest.main()
