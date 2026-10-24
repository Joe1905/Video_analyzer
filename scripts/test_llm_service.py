"""HTTP contract tests; no cloud calls or Whisper inference."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError
from llm_service import APIError, Handler, Service


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"LLM_DATA_DIR": self.directory.name,
                                     "XDG_CACHE_HOME": self.directory.name,
                                     "LLM_READ_TIMEOUT_SECONDS": "150",
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
        self.assertEqual(call.call_args.kwargs["timeout"], (10, 150))

    def test_read_timeout_configuration(self):
        with patch.dict(os.environ, {"LLM_DATA_DIR": self.directory.name}):
            with patch.dict(os.environ):
                os.environ.pop("LLM_READ_TIMEOUT_SECONDS", None)
                self.assertEqual(Service().read_timeout, 150)
            with patch.dict(os.environ, {"LLM_READ_TIMEOUT_SECONDS": "120"}):
                self.assertEqual(Service().read_timeout, 120)
            for value in ("0", "-1", "invalid"):
                with patch.dict(os.environ, {"LLM_READ_TIMEOUT_SECONDS": value}), self.assertRaises(ValueError):
                    Service()

    def test_timeout_classification_and_redacted_logs(self):
        secret = "must-not-appear-in-logs"
        errors = [(requests.ReadTimeout(secret), 504),
                  (requests.ConnectTimeout(secret), 504),
                  (requests.ConnectionError(ReadTimeoutError(None, secret, secret)), 504),
                  (requests.ConnectionError(secret), 502), (ValueError(secret), 502)]
        self.body.update(thinking={"type": "enabled"}, reasoning_effort="high",
                         response_format={"type": "json_object"}, max_tokens=16384)
        for exception, expected in errors:
            output = io.StringIO()
            with redirect_stdout(output), patch("llm_service.requests.post", side_effect=exception) as call:
                with self.assertRaises(APIError) as result:
                    self.service.text_request(self.body)
            self.assertEqual(result.exception.status, expected)
            call.assert_called_once()
            self.assertEqual(call.call_args.kwargs["json"], self.body)
            log = json.loads(output.getvalue())
            self.assertEqual(log["status"], expected)
            self.assertEqual(log["exception_type"], type(exception).__name__)
            self.assertGreaterEqual(log["elapsed_ms"], 0)
            for private in (secret, "upstream-test-key", self.service.token, "Hola"):
                self.assertNotIn(private, output.getvalue())

    def test_body_read_timeout_returns_http_504(self):
        with patch("llm_service.requests.post", side_effect=requests.ConnectionError(
                ReadTimeoutError(None, "private-url", "private-message"))):
            # Use a Session so only the service's upstream requests.post is mocked.
            with requests.Session() as client:
                response = client.post(self.url + "/v1/chat/completions", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"error": {"message": "DeepSeek request timed out; not retried"}})

    @unittest.skipUnless(os.getenv("LLM_TEST_LONG_TIMEOUTS") == "1", "opt-in 150-second socket test")
    def test_real_read_timeout_beyond_old_55_second_boundary(self):
        release = threading.Event()
        class SlowUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.flush()
                release.wait(56 if self.path.startswith("/success/") else 160)
                try:
                    self.wfile.write(b"{}")
                except OSError:
                    pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        def probe(path):
            service = copy(self.service)
            service.upstream = f"http://127.0.0.1:{upstream.server_port}/{path}"
            started = time.monotonic()
            try:
                status, _ = service.text_request(self.body)
            except APIError as exc:
                status = exc.status
            return status, time.monotonic() - started

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(probe, ("success", "timeout")))
            self.assertEqual(results[0][0], 200)
            self.assertGreaterEqual(results[0][1], 55)
            self.assertLess(results[0][1], 75)
            self.assertEqual(results[1][0], 504)
            self.assertGreaterEqual(results[1][1], 149)
            self.assertLess(results[1][1], 165)
        finally:
            release.set()
            upstream.shutdown()
            upstream.server_close()
            thread.join()

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

    def test_https_requires_and_accepts_service_certificate(self):
        self.server.socket = self.service.tls_context().wrap_socket(self.server.socket, server_side=True)
        url = self.url.replace("http://", "https://") + "/healthz"
        with self.assertRaises(requests.exceptions.SSLError):
            requests.get(url, timeout=5)
        response = requests.get(url, verify=str(self.service.directory / "server.crt"), timeout=5)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
