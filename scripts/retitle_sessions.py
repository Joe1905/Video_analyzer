"""Script to update existing chat sessions with the new title extraction and LLM intent recognition logic."""
import os
import sys
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from chat_session import ChatStore, Session, Message

DATA_DIR = ROOT / "data"
DATA_DEV_DIR = ROOT / "data-dev"

SESSION_FILES = [
    DATA_DIR / "sessions.json",
    DATA_DIR / "sellersprite_mcp" / "chat_sessions.json",
    DATA_DIR / "sellersprite_mcp" / "sessions.json",
    DATA_DIR / "fastmoss_mcp" / "chat_sessions.json",
    DATA_DIR / "fastmoss_mcp" / "sessions.json",
    DATA_DEV_DIR / "sessions.json",
    DATA_DEV_DIR / "sellersprite_mcp" / "chat_sessions.json",
    DATA_DEV_DIR / "sellersprite_mcp" / "sessions.json",
    DATA_DEV_DIR / "fastmoss_mcp" / "chat_sessions.json",
    DATA_DEV_DIR / "fastmoss_mcp" / "sessions.json",
    ROOT / "sellersprite_mcp_chat" / "data" / "sessions.json",
]


def generate_llm_title(user_text: str) -> str | None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    try:
        import requests
        api_url = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1").rstrip("/")
        model = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-v4-flash")

        prompt = (
            "根据用户发起的首条提问内容，归纳其核心业务意图，生成一个简短的会话标题。\n"
            "要求：\n"
            "1. 标题长度在 4 到 12 个字之间（包含汉字或数字）。\n"
            "2. 突出核心意图与关键词，例如：'3C电子蓝海挖掘'、'ABA高增长词分析'、'潜质竞品ASIN拆解'。\n"
            "3. 严禁包含'请使用卖家精灵官方Skill'、'开始分析'、'目标：'、标点符号、引号或多余修饰语。\n"
            "4. 只直接输出标题文本，不要包含任何解释说明。\n\n"
            f"用户提问：\n{user_text[:600]}"
        )
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 30,
            "temperature": 0.3,
        }
        resp = requests.post(f"{api_url}/chat/completions", json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            raw_title = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            cleaned_title = re.sub(r'["\'`“”‘’《》【】\n\r]', '', raw_title).strip()
            if cleaned_title and len(cleaned_title) <= 30:
                return cleaned_title
    except Exception as e:
        print(f"[ERROR] LLM title call failed: {e}")
    return None


def main():
    modified_count = 0
    found_files = 0
    for session_file in SESSION_FILES:
        if not session_file.is_file():
            continue
        found_files += 1
        print(f"Processing session file: {session_file}")
        try:
            with open(session_file, encoding="utf-8") as f:
                raw_data = json.load(f)
            if not isinstance(raw_data, list):
                continue
            for item in raw_data:
                sid = item.get("id", "")
                old_title = item.get("title", "")
                is_custom = item.get("title_is_custom", False)
                messages = item.get("messages", [])

                first_user_text = ""
                for m in messages:
                    if m.get("role") == "user" and m.get("content"):
                        first_user_text = m.get("content", "")
                        break

                if not first_user_text:
                    continue

                llm_title = generate_llm_title(first_user_text) if not is_custom else None
                new_title = llm_title or ChatStore._auto_title(Session(id=sid, messages=[Message(id="1", role="user", content=first_user_text)]))

                if new_title and new_title != old_title:
                    print(f"  [RENAME] Session {sid}: '{old_title}' -> '{new_title}'")
                    item["title"] = new_title
                    modified_count += 1

            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print(f"Error processing {session_file}: {e}")

    print(f"\nScan complete. Examined {found_files} session files, updated {modified_count} session titles.")


if __name__ == "__main__":
    main()
