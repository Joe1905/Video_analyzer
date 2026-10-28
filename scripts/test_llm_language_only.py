"""No model inference or network: ensure language-only bypasses transcription."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from llm_service import APIError, Service


class LanguageOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = Service.__new__(Service)
        self.service.directory = self.service.cache = Path(self.tmp.name)
        (self.service.cache / "small.pt").touch()
        self.service.model_name = "small"
        self.service.asr_lock = threading.Lock()
        self.service.model = Mock()
        self.service.model.dims.n_mels = 80
        self.service.model.device = "cpu"
        self.service.model.detect_language.return_value = (0, {"es": .95, "fr": .05})
        self.service.model.transcribe.return_value = {"text": "Hola", "language": "es", "segments": []}
        self.whisper = Mock()
        self.whisper.tokenizer.LANGUAGES = {"es": "Spanish"}

    def call(self, language_only):
        with patch.dict("sys.modules", {"torch": Mock(), "whisper": self.whisper}), patch(
            "llm_service.subprocess.run", return_value=SimpleNamespace(stdout='{"format":{"duration":"2"}}')
        ):
            return self.service.transcribe(io.BytesIO(b"fixture"), "", "small", language_only=language_only)

    def test_detection_never_transcribes(self):
        result = self.call(True)
        self.service.model.transcribe.assert_not_called()
        self.service.model.detect_language.assert_called_once()
        self.assertEqual(result["task"], "detect_language")
        self.assertEqual(result["language"], "es")
        self.assertEqual(result["language_probability"], .95)
        self.assertNotIn("text", result)
        self.assertFalse(self.service.asr_lock.locked())

    def test_normal_transcription_is_unchanged(self):
        result = self.call(False)
        self.service.model.detect_language.assert_not_called()
        self.service.model.transcribe.assert_called_once()
        self.assertEqual(result["text"], "Hola")
        self.assertNotIn("task", result)

    def test_no_language_hint_or_silent_backend_fallback(self):
        for language, model in [("es", "small"), ("", "small-ct2-int8-v1")]:
            with self.assertRaises(APIError):
                self.service.transcribe(io.BytesIO(), language, model, language_only=True)


if __name__ == "__main__":
    unittest.main()
