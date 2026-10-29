"""OpenAI Responses transport for the existing, permission-scoped chat loop."""
import json as json_module
import os

import requests


def chat_model_options():
    return [
        {"id": "deepseek", "label": "DeepSeek", "model": os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash-vision-exp"),
         "configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip())},
        {"id": "openai", "label": "GPT", "model": os.getenv("OPENAI_CHAT_MODEL", "gpt-6.1-sol"),
         "configured": bool(os.getenv("OPENAI_API_KEY", "").strip())},
    ]


def validate_chat_model(value):
    provider = value or "deepseek"
    if provider not in ("deepseek", "openai"):
        raise ValueError("不支持的对话模型")
    option = next(item for item in chat_model_options() if item["id"] == provider)
    if not option["configured"]:
        key = "OPENAI_API_KEY" if provider == "openai" else "DEEPSEEK_API_KEY"
        raise ValueError(f"{option['label']} 尚未配置，请在服务器配置 {key}")
    return provider


class ChatResponse:
    """The small response interface consumed by the existing chat helpers."""
    status_code = 200
    text = ""

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class OpenAIChatTransport:
    provider_name = "openai"
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self):
        # One instance per user request: never share reasoning state across chats.
        self.reasoning_by_call = {}

    def input_items(self, messages):
        items = []
        seen_reasoning = set()
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                              "output": str(message.get("content") or "")})
                continue
            content = message.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"type": "input_text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        image = part["image_url"]
                        parts.append({"type": "input_image", "image_url": image["url"],
                                      "detail": image.get("detail", "auto")})
                content = parts
            if content:
                items.append({"role": role, "content": content})
            for call in message.get("tool_calls") or []:
                for reasoning in self.reasoning_by_call.get(call["id"], []):
                    if reasoning["id"] not in seen_reasoning:
                        items.append(reasoning)
                        seen_reasoning.add(reasoning["id"])
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": call["function"]["name"],
                              "arguments": call["function"]["arguments"]})
        return items

    def post(self, url, *, headers=None, data=None, json=None, timeout=120):
        # Ignore the legacy URL and credentials supplied by helpers. OpenAI keys
        # are used only with the official endpoint, and no DeepSeek fallback runs.
        payload = json if json is not None else json_module.loads(data)
        request = {
            "model": os.getenv("OPENAI_CHAT_MODEL", "gpt-6.1-sol"),
            "input": self.input_items(payload["messages"]),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": max(4096, int(payload.get("max_tokens") or 16384)),
            "reasoning": {"effort": os.getenv("OPENAI_CHAT_REASONING_EFFORT", "low")},
        }
        if payload.get("tools"):
            request["tools"] = [dict(tool["function"], type="function", strict=False)
                                for tool in payload["tools"]]
        if payload.get("response_format"):
            request["text"] = {"format": payload["response_format"]}
        proxy = os.getenv("OPENAI_PROXY_URL", "").strip()
        try:
            response = requests.post(self.endpoint, json=request,
                headers={"Authorization": "Bearer " + os.getenv("OPENAI_API_KEY", ""),
                         "Content-Type": "application/json"},
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=(10, timeout), allow_redirects=False)
        except requests.RequestException as exc:
            raise RuntimeError(f"OpenAI 连接失败（{type(exc).__name__}），请检查网络和 OPENAI_PROXY_URL") from None
        if response.status_code != 200:
            hints = {401: "API Key 无效", 403: "账户或模型无访问权限", 429: "额度不足或请求受限"}
            raise RuntimeError(f"OpenAI HTTP {response.status_code}：{hints.get(response.status_code, '请求失败，请检查模型配置或稍后重试')}")
        result = response.json()
        if result.get("status") != "completed":
            raise RuntimeError("OpenAI 未完成回答，可能已达到输出上限；请缩小问题范围后重试")
        output = result.get("output") or []
        reasoning = [item for item in output if item.get("type") == "reasoning"]
        calls, texts = [], []
        for item in output:
            if item.get("type") == "function_call":
                call_id = item["call_id"]
                self.reasoning_by_call[call_id] = reasoning
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": item["name"], "arguments": item["arguments"]}})
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
                    elif part.get("type") == "refusal":
                        texts.append(part["refusal"])
        if not texts and not calls:
            raise RuntimeError("OpenAI 返回了空回答，请重试")
        usage = result.get("usage") or {}
        return ChatResponse({"id": result.get("id"), "model": result.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "\n".join(texts), "tool_calls": calls}}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)}})
