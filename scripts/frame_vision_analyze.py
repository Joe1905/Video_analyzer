"""Run the existing analyzer with a status-aware OpenAI-compatible client."""
import base64
import sys
from pathlib import Path

import requests
from vision_provider import completion_url, frame_config, mark_unavailable, observe_response


def generate(config, prompt, image_path=None, stream=False, model=None, temperature=0.2, num_predict=256):
    content = prompt
    if image_path:
        image = base64.b64encode(Path(image_path).read_bytes()).decode()
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]
    payload = {"model": config["model"], "messages": [{"role": "user", "content": content}],
               "temperature": temperature, "max_tokens": num_predict, "stream": False}
    if config["provider"] == "deepseek":
        payload["thinking"] = {"type": "disabled"}
    try:
        response = requests.post(completion_url(config["api_url"]),
            headers={"Authorization": "Bearer " + config["api_key"]}, json=payload, timeout=(10, 300))
    except requests.RequestException:
        if config["provider"] == "qwen":
            status = mark_unavailable("ConnectionFailed", "Qwen 连接失败或请求超时")
            raise SystemExit(status["message"] + "；请重新提交帧分析")
        raise SystemExit("DeepSeek Vision 连接失败或请求超时")
    if not response.ok:
        status = observe_response(response) if config["provider"] == "qwen" else None
        # Upstream catches Exception and may produce a success artifact from failed frames.
        # Exit the CLI instead, preserving failure for the queue/report caller.
        raise SystemExit((status["message"] if status else
                          config["provider"] + " 帧分析请求失败，HTTP " + str(response.status_code)) + "；请重新提交帧分析")
    try:
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
    except (ValueError, KeyError, IndexError, TypeError):
        raise SystemExit(config["provider"] + " 帧分析未返回有效内容")
    return {"response": content}


def main():
    config = frame_config()
    print("[vision] 帧分析线路：" + config["provider"] + " / " + config["model"], flush=True)
    from video_analyzer import cli
    from video_analyzer.clients.generic_openai_api import GenericOpenAIAPIClient

    class RoutedClient(GenericOpenAIAPIClient):
        def generate(self, *args, **kwargs):
            return generate(config, *args, **kwargs)

    # CLI's factory is the single construction point; no installed package edits.
    cli.GenericOpenAIAPIClient = RoutedClient
    sys.argv.extend(["--api-key", config["api_key"], "--api-url", config["api_url"].removesuffix("/chat/completions"),
                     "--model", config["model"]])
    return cli.main()


if __name__ == "__main__":
    main()
