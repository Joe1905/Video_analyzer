"""Helper to remove duplicate block in web_app.py if present."""
from pathlib import Path

target = Path(__file__).parent / "web_app.py"
text = target.read_text(encoding="utf-8")

dup_pattern = '}\n    "asin": "ASIN", "ad":'
if dup_pattern in text:
    idx = text.find(dup_pattern)
    end_idx = text.find("}", idx + 2)
    if end_idx != -1:
        text = text[:idx + 1] + text[end_idx + 1:]
        target.write_text(text, encoding="utf-8")
        print("Successfully removed duplicate block from web_app.py!")
    else:
        print("End pattern not found.")
else:
    print("Duplicate pattern not found, file is clean.")
