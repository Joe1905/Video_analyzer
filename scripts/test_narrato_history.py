from pathlib import Path
from tempfile import TemporaryDirectory
import os
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from app.services.narrato_history import history, task_files

with TemporaryDirectory() as directory:
    root = Path(directory)
    older = root / "older"
    newer = root / "newer"
    empty = root / "empty"
    for p in (older, newer, empty):
        p.mkdir()
    (older / "combined.mp4").write_bytes(b"old")
    (newer / "speech.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    (newer / "merger.mp4").write_bytes(b"intermediate")
    os.utime(older / "combined.mp4", (1, 1))
    assert history(root) == [newer, older]
    assert [p.name for p in task_files(newer)] == ["speech.srt"]
    with patch("app.services.narrato_history.history", return_value=[newer]):
        app = AppTest.from_string("from app.services.narrato_history import render_history\nrender_history()").run()
        assert not app.exception and not app.selectbox
        assert app.button[0].label == "历史记录"
        app = AppTest.from_string("from app.services.narrato_history import render_browser\nrender_browser()").run()
        assert not app.exception
        assert "Hello" in app.code[0].value
        assert app.selectbox[0].value.name == "speech.srt"
print("PASS: existing outputs indexed, intermediates excluded, history UI opens and previews subtitles")
