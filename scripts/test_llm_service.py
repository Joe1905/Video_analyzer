"""HTTP contract tests; no cloud calls or Whisper inference."""
import io
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests
from llm_service import APIError, Handler, Service


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"LLM_DATA_DIR": self.directory.name,
                                     "XDG_CACHE_HOME": self.directory.name,
                                     "DEEPSEEK_API_KEY": "upstream-test-key"}):
            self.service = Service()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.headers = {"Authorization": "Bearer " + self.service.token}
        self.body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "Hola"}], "stream": False}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def test_health_and_auth_boundary(self):
        self.assertEqual(requests.get(self.url + "/healthz").status_code, 200)
        self.assertEqual(requests.get(self.url + "/v1/models").status_code, 401)
        self.assertEqual(requests.post(self.url + "/v1/chat/completions", json=self.body).status_code, 401)
        self.assertNotIn(self.service.token, requests.get(self.url + "/healthz").text)

    def test_raw_request_uses_upstream_key_and_preserves_errors(self):
        payload = {"error": {"message": "quota exhausted"}}
        with patch("llm_service.requests.post", return_value=Mock(status_code=402, json=lambda: payload)) as call:
            status, response = self.service.text_request(self.body)
        self.assertEqual((status, response), (402, payload))
        self.assertEqual(call.call_args.kwargs["json"], self.body)
        self.assertEqual(call.call_args.kwargs["headers"]["Authorization"], "Bearer upstream-test-key")
        self.assertFalse(call.call_args.kwargs["allow_redirects"])

    def test_streams_tools_and_nontext_rejected(self):
        for body in ({**self.body, "stream": True}, {**self.body, "tools": []},
                     {**self.body, "messages": [{"role": "user", "content": [{"type": "input_audio"}]}]}):
            response = requests.post(self.url + "/v1/chat/completions", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 400)

    def test_multipart_transcription_does_not_call_deepseek(self):
        asr = {"text": "Hola", "language": "es", "elapsed_ms": 25}
        def transcribe(audio, language, model):
            self.assertEqual(audio.read(), b"test-audio")
            self.assertEqual((language, model), ("es", self.service.model_name))
            return asr
        with patch.object(self.service, "transcribe", side_effect=transcribe), patch.object(self.service, "text_request") as text:
            response = requests.post(self.url + "/v1/audio/transcriptions", headers=self.headers,
                files={"file": ("test.wav", b"test-audio")}, data={"language": "es", "model": self.service.model_name})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), asr)
        text.assert_not_called()

    def test_translation_uses_actual_transcript_and_rejects_truncation(self):
        asr = {"text": "Actual recognized words", "language": "es", "elapsed_ms": 25}
        raw = {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {"content": "English"}}]}
        with patch.object(self.service, "text_request", return_value=(200, raw)) as call:
            result = self.service.translate_audio(asr, "deepseek-flash", "en")
        self.assertEqual(call.call_args.args[0]["messages"][1]["content"], asr["text"])
        self.assertEqual(result["transcript"], asr["text"])
        raw["choices"][0]["finish_reason"] = "length"
        with patch.object(self.service, "text_request", return_value=(200, raw)), self.assertRaises(APIError):
            self.service.translate_audio(asr, "deepseek-flash", "en")

    def test_asr_busy_and_missing_cache(self):
        with self.assertRaises(APIError) as error:
            self.service.transcribe(io.BytesIO(b"test"), "es", self.service.model_name)
        self.assertEqual(error.exception.status, 503)
        self.service.cache.mkdir()
        (self.service.cache / f"{self.service.model_name}.pt").write_bytes(b"test-cache")
        with self.service.asr_lock, self.assertRaises(APIError) as error:
            self.service.transcribe(io.BytesIO(b"test"), "es", self.service.model_name)
        self.assertEqual(error.exception.status, 429)

    def test_malformed_json_and_missing_file(self):
        response = requests.post(self.url + "/v1/chat/completions", headers={**self.headers, "Content-Type": "application/json"}, data=b"{")
        self.assertEqual(response.status_code, 400)
        response = requests.post(self.url + "/v1/audio/transcriptions", headers=self.headers, files={"other": ("test.wav", b"test")})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
