#!/usr/bin/env python3
"""Offline checks for GPT text, images, tool continuation and chat boundaries."""
import io
import base64
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import web_app
from chat_session import Message, Session, ChatStore, save_sessions_to_disk, load_sessions_from_disk
from openai_chat import OpenAIChatTransport, chat_model_options, validate_chat_model


def response(output, status="completed", http_status=200):
    return SimpleNamespace(status_code=http_status, json=lambda: {
        "id": "resp_test", "model": "gpt-6.1-sol", "status": status, "output": output,
        "usage": {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}})


def answer(text="已完成"):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def test_text_images_and_reasoning_tool_continuation():
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "encrypted"}
    call = {"type": "function_call", "call_id": "call_1", "name": "system__current_time", "arguments": "{}"}
    messages = [{"role": "system", "content": "回答用户", "_context_scope": "system"},
                {"role": "user", "content": "上一轮问题"},
                {"role": "assistant", "content": "上一轮回答"},
                {"role": "user", "content": [{"type": "text", "text": "看这张图"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    tool = {"type": "function", "function": {"name": "system__current_time", "parameters": {"type": "object", "properties": {}}}}
    transport = OpenAIChatTransport()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "OPENAI_PROXY_URL": "http://127.0.0.1:7890"}), \
         patch("openai_chat.requests.post", side_effect=[response([reasoning, call]), response([answer()])]) as post:
        first = transport.post("https://api.deepseek.com/v1/chat/completions", json={"messages": messages, "tools": [tool]})
        sent = post.call_args.kwargs["json"]
        assert post.call_args.args[0] == "https://api.openai.com/v1/responses"
        assert sent["input"][-1]["content"][1]["type"] == "input_image"
        assert sent["input"][2]["content"] == "上一轮回答"
        assert "_context_scope" not in sent["input"][0]
        assert sent["tools"][0]["strict"] is False
        assert "temperature" not in sent and "thinking" not in sent
        assert sent["store"] is False
        assert sent["reasoning"] == {"effort": "medium"}
        assert post.call_args.kwargs["proxies"]["https"] == "http://127.0.0.1:7890"
        assert post.call_args.kwargs["allow_redirects"] is False
        msg = first.json()["choices"][0]["message"]
        messages.append(web_app.build_deepseek_tool_assistant_message(msg, msg["tool_calls"], True))
        messages.append({"role": "tool", "tool_call_id": "call_1", "content": "2026-10-29"})
        second = transport.post("ignored", data=json.dumps({"messages": messages}).encode())
        sent = post.call_args.kwargs["json"]
        assert sent["input"][-3:] == [reasoning, call, {"type": "function_call_output", "call_id": "call_1", "output": "2026-10-29"}]
        assert second.json()["choices"][0]["message"]["content"] == "已完成"
        assert second.json()["usage"]["total_tokens"] == 20
    assert OpenAIChatTransport().reasoning_by_call == {}
    with patch.dict(os.environ, {"OPENAI_CHAT_REASONING_EFFORT": "low"}), patch("openai_chat.requests.post", return_value=response([answer()])) as post:
        OpenAIChatTransport("gpt-6.1-terra").post("ignored", json={"messages": [{"role": "user", "content": "hi"}]})
        assert post.call_args.kwargs["json"]["model"] == "gpt-6.1-terra"
        assert post.call_args.kwargs["json"]["reasoning"]["effort"] == "medium"


def test_errors_and_key_privacy():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}):
        assert "test-secret" not in json.dumps(chat_model_options())
        assert validate_chat_model("openai") == "openai"
        for fake in [response([], http_status=401), response([], status="incomplete"), response([])]:
            with patch("openai_chat.requests.post", return_value=fake) as post:
                try:
                    OpenAIChatTransport().post("ignored", json={"messages": []})
                    raise AssertionError("expected error")
                except RuntimeError as exc:
                    assert "test-secret" not in str(exc)
                assert post.call_count == 1
        with patch("openai_chat.requests.post", side_effect=requests.ConnectionError("test-secret")):
            try:
                OpenAIChatTransport().post("ignored", json={"messages": []})
                raise AssertionError("expected connection error")
            except RuntimeError as exc:
                assert "test-secret" not in str(exc)
    with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
        try:
            validate_chat_model("openai")
            raise AssertionError("expected missing key")
        except ValueError as exc:
            assert "OPENAI_API_KEY" in str(exc)


def test_existing_chat_loop_and_tool_boundary():
    for name, permitted in [("system__current_time", True), ("sellersprite__not_exposed", False)]:
        user = Message(id="u", role="user", content="今天的日期")
        assistant = Message(id="a", role="assistant", content="", status="pending")
        session = Session(id="test", messages=[user, assistant])
        store = Mock()
        def update(session, message, content, status="done"):
            message.content, message.status = content, status
        store.update_message.side_effect = update
        tool = {"type": "function", "function": {"name": "system__current_time", "parameters": {"type": "object", "properties": {}}}}
        output = {"type": "function_call", "call_id": "call_1", "name": name, "arguments": "{}"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), \
             patch("web_app.resolve_chat_intent", return_value={"intent": "general", "task_depth": "direct", "tools": None, "max_rounds": 3}), \
             patch("web_app.chat_request_needs_tools", return_value=True), \
             patch("web_app.build_prefixed_model_tools", return_value=[tool]), \
             patch("web_app.execute_prefixed_tool", return_value={"ok": True, "date": "2026-10-29"}) as execute, \
             patch("web_app.record_api_call") as record, \
             patch("openai_chat.requests.post", side_effect=[response([output]), response([answer("日期已核对")])]) as post:
            web_app.run_chat_deepseek(store, session, assistant, user.content, "home", {"system__current_time"}, "openai")
            assert execute.call_count == int(permitted)
            assert assistant.status == "done", assistant.content
            assert assistant.content == "日期已核对"
            assert post.call_count == 2
            assert all(call.args[0] == "openai" for call in record.call_args_list)
            assert all(call.args[0] == OpenAIChatTransport.endpoint for call in post.call_args_list)


def test_missing_key_rejected_before_creating_messages():
    body = json.dumps({"llmProvider": "openai", "message": "你好"}).encode()
    handler = SimpleNamespace(headers={"Content-Length": str(len(body))}, rfile=io.BytesIO(body))
    with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), \
         patch("web_app.json_response") as reply, patch("web_app.chat_store_for_provider") as store:
        web_app.Handler.handle_chat_ask(handler)
        assert reply.call_args.args[1] == 400
        assert "OPENAI_API_KEY" in reply.call_args.args[2]["error"]
        store.assert_not_called()


def test_full_sociavault_catalog_and_provider_isolation():
    tools = [{"name": f"social_tool_{i}", "inputSchema": {"type": "object", "properties": {}}} for i in range(107)]
    with patch("web_app.list_mcp_bridge_tools", side_effect=lambda domain: tools if domain == "sociavault" else []):
        selected = web_app.provider_default_enabled_tool_ids("home")
        expected = {"sociavault__" + tool["name"] for tool in tools}
        assert expected <= selected
        exposed = {tool["function"]["name"] for tool in web_app.build_prefixed_model_tools(selected)}
        assert expected <= exposed
        assert not expected & web_app.filter_locked_provider_tool_ids("amazon", selected)
        assert not expected & web_app.filter_locked_provider_tool_ids("fastmoss", selected)
        catalog = web_app.build_tool_catalog("home")
        assert set(catalog["toolRegistries"]["sociavault"]) == expected
    failure = web_app.normalize_prefixed_tool_result("sociavault__test", {
        "ok": False, "data": {"isError": True, "content": [{"type": "text", "text": "API failed"}]}})
    assert failure["data_state"] == "error" and not failure["evidence_observed"]


def test_image_generation_storage_and_followup():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nimage-test").decode()
    with tempfile.TemporaryDirectory() as directory, patch("web_app.CHAT_ATTACHMENT_DIR", Path(directory)):
        attachments = []
        def save(encoded):
            attachments.append(web_app.save_generated_chat_image(encoded))
            return "图片已生成"
        transport = OpenAIChatTransport(on_image=save)
        image_call = {"type": "image_generation_call", "status": "completed", "result": png}
        with patch("openai_chat.requests.post", return_value=response([image_call])) as post:
            result = transport.post("ignored", json={"messages": [], "enable_image_generation": True}).json()
            assert result["choices"][0]["message"]["content"] == "图片已生成"
            assert post.call_args.kwargs["json"]["tools"][-1]["type"] == "image_generation"
            assert post.call_args.kwargs["timeout"][1] >= 300
            assert png not in json.dumps(result)
        message = Message(id="image", role="assistant", content="已生成", attachments=attachments,
            tool_calls=[{"function": {"name": "system__current_time"}}])
        store = ChatStore(Path(directory) / "sessions.json")
        store.sessions["images"] = Session(id="images", messages=[message, Message(id="u", role="user", content="改为蓝色")])
        save_sessions_to_disk(store)
        restored = ChatStore(store.sessions_file)
        load_sessions_from_disk(restored)
        history, _ = web_app.build_chat_history_context(restored.sessions["images"].messages, "pending")
        items = transport.input_items(history)
        images = [part for item in items if isinstance(item.get("content"), list)
                  for part in item["content"] if part["type"] == "input_image"]
        assert len(images) == 1 and images[0]["image_url"].endswith(png)
        assert web_app.chat_attachment_path(attachments[0]["id"]).read_bytes().startswith(b"\x89PNG")
        for bad in ["invalid base64", base64.b64encode(b"not png").decode()]:
            try:
                web_app.save_generated_chat_image(bad)
                raise AssertionError("invalid image accepted")
            except ValueError:
                pass
        with patch("openai_chat.requests.post", return_value=response([answer()])) as post:
            transport.post("ignored", json={"messages": []})
            assert "tools" not in post.call_args.kwargs["json"]  # helper requests cannot generate images
            OpenAIChatTransport().post("ignored", json={"messages": [], "enable_image_generation": True})
            assert "tools" not in post.call_args.kwargs["json"]  # other provider pages have no callback


if __name__ == "__main__":
    test_image_generation_storage_and_followup()
    test_text_images_and_reasoning_tool_continuation()
    test_errors_and_key_privacy()
    test_existing_chat_loop_and_tool_boundary()
    test_missing_key_rejected_before_creating_messages()
    test_full_sociavault_catalog_and_provider_isolation()
    print("OpenAI chat tests passed")
