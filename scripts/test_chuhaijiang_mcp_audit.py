"""Focused privacy and lifecycle checks for the Chuhaijiang MCP audit trail."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from scripts import web_app
except ModuleNotFoundError:
    import web_app


class TestChuhaijiangMcpAudit(unittest.TestCase):
    def setUp(self):
        self._context = getattr(web_app.CHAT_EXECUTION_CONTEXT, "chuhaijiang", None)

    def tearDown(self):
        if self._context is None:
            try:
                delattr(web_app.CHAT_EXECUTION_CONTEXT, "chuhaijiang")
            except AttributeError:
                pass
        else:
            web_app.CHAT_EXECUTION_CONTEXT.chuhaijiang = self._context

    def test_audit_is_owner_scoped_and_never_persists_raw_arguments_or_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_db = Path(temp_dir) / "mcp_audit.sqlite"
            with patch.object(web_app, "CHUHAIJIANG_MCP_AUDIT_DB", audit_db):
                web_app.record_chuhaijiang_mcp_audit(
                    trace_id="trace-1",
                    owner_id="owner-a",
                    session_id="session-a",
                    tool_id="chuhaijiang__account_info",
                    args_digest="1234567890abcdef",
                    stage="received",
                    elapsed_ms=3,
                    token="sk_live_should_never_persist",
                    arguments={"token": "sk_live_should_never_persist"},
                )
                own_events = web_app.list_chuhaijiang_mcp_audit("owner-a")
                self.assertEqual(len(own_events), 1)
                self.assertEqual(web_app.list_chuhaijiang_mcp_audit("owner-b"), [])
                serialized = json.dumps(own_events, ensure_ascii=False)
                self.assertNotIn("sk_live_should_never_persist", serialized)
                self.assertNotIn("owner-a", serialized)
                self.assertEqual(own_events[0]["tool_id"], "chuhaijiang__account_info")
                self.assertEqual(own_events[0]["stage"], "received")

    def test_preflight_confirmation_and_bridge_errors_are_redacted_and_audited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_db = Path(temp_dir) / "mcp_audit.sqlite"
            context = {"owner_id": "owner-a", "session_id": "session-a"}
            web_app.CHAT_EXECUTION_CONTEXT.chuhaijiang = context
            key = ("owner-a", "session-a")
            with patch.object(web_app, "CHUHAIJIANG_MCP_AUDIT_DB", audit_db):
                try:
                    confirmation = web_app.execute_prefixed_tool(
                        "chuhaijiang__ai_generate",
                        {"prompt": "sk_live_should_never_persist"},
                        allowed_tool_ids={"chuhaijiang__ai_generate"},
                    )
                    self.assertEqual(confirmation["error"], "confirmation_required")
                    with patch.object(
                        web_app,
                        "mcp_bridge_request",
                        side_effect=RuntimeError("authorization sk_live_should_never_persist"),
                    ):
                        failure = web_app.execute_prefixed_tool(
                            "chuhaijiang__account_info",
                            {},
                            allowed_tool_ids={"chuhaijiang__account_info"},
                        )
                    self.assertEqual(failure["error"], "Chuhaijiang MCP authentication error")
                finally:
                    with web_app.CHUHAIJIANG_CONFIRMATIONS_LOCK:
                        web_app.CHUHAIJIANG_CONFIRMATIONS.pop(key, None)
                events = web_app.list_chuhaijiang_mcp_audit("owner-a")
                stages = {event["stage"] for event in events}
                self.assertTrue({"received", "preflight_ok", "confirmation_required", "bridge_error"}.issubset(stages))
                self.assertNotIn("sk_live_should_never_persist", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
