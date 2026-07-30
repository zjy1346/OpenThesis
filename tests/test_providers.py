from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openthesis.providers import (
    ModelConfig,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
)


class ProviderHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object], str]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(
            (self.path, payload, self.headers.get("Authorization", ""))
        )
        if payload.get("model") == "echo-key":
            authorization = self.headers.get("Authorization", "")
            self.send_response(401)
            self.end_headers()
            self.wfile.write(
                json.dumps({"error": f"rejected {authorization}"}).encode()
            )
            return
        if payload.get("model") == "format-unsupported" and "response_format" in payload:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"response_format is unsupported"}')
            return
        if payload.get("model") == "bad-request":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid model"}')
            return
        if payload.get("model") == "fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"intentional"}')
            return
        if self.path == "/v1/chat/completions":
            response = {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"ok":true,"provider":"openai"}\n```'
                        }
                    }
                ]
            }
        elif self.path == "/api/chat":
            response = {
                "message": {
                    "content": '{"ok":true,"provider":"ollama"}',
                }
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_openai_compatible_protocol(self) -> None:
        ProviderHandler.requests.clear()
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="test",
                base_url=f"{self.base_url}/v1",
                api_key="session-secret",
            )
        )
        result = provider.generate("system", "user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "openai")
        _, payload, authorization = ProviderHandler.requests[-1]
        self.assertEqual(authorization, "Bearer session-secret")
        self.assertEqual(payload["temperature"], 0.2)

    def test_optional_temperature_is_omitted(self) -> None:
        ProviderHandler.requests.clear()
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="test",
                base_url=f"{self.base_url}/v1",
                temperature=None,
            )
        )
        provider.generate("system", "user")
        self.assertNotIn("temperature", ProviderHandler.requests[-1][1])

    def test_ollama_protocol(self) -> None:
        ProviderHandler.requests.clear()
        provider = OllamaProvider(
            ModelConfig(
                provider="ollama",
                model="test",
                base_url=self.base_url,
            )
        )
        result = provider.generate("system", "user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "ollama")

    def test_ollama_omits_options_when_temperature_is_none(self) -> None:
        ProviderHandler.requests.clear()
        provider = OllamaProvider(
            ModelConfig(
                provider="ollama",
                model="test",
                base_url=self.base_url,
                temperature=None,
            )
        )
        provider.generate("system", "user")
        self.assertNotIn("options", ProviderHandler.requests[-1][1])

    def test_response_format_unsupported_retries_once_without_it(self) -> None:
        ProviderHandler.requests.clear()
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="format-unsupported",
                base_url=f"{self.base_url}/v1",
            )
        )
        result = provider.generate("system", "user")
        self.assertTrue(result["ok"])
        self.assertEqual(len(ProviderHandler.requests), 2)
        self.assertIn("response_format", ProviderHandler.requests[0][1])
        self.assertNotIn("response_format", ProviderHandler.requests[1][1])

    def test_other_400_is_not_retried(self) -> None:
        ProviderHandler.requests.clear()
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="bad-request",
                base_url=f"{self.base_url}/v1",
            )
        )
        with self.assertRaises(ProviderError):
            provider.generate("system", "user")
        self.assertEqual(len(ProviderHandler.requests), 1)

    def test_http_error_redacts_api_key_even_if_server_echoes_it(self) -> None:
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="echo-key",
                base_url=f"{self.base_url}/v1",
                api_key="must-never-appear",
            )
        )
        with self.assertRaises(ProviderError) as caught:
            provider.generate("system", "user")
        self.assertNotIn("must-never-appear", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_http_error_is_reported(self) -> None:
        ProviderHandler.requests.clear()
        provider = OpenAICompatibleProvider(
            ModelConfig(
                provider="openai-compatible",
                model="fail",
                base_url=f"{self.base_url}/v1",
            )
        )
        with self.assertRaises(ProviderError):
            provider.generate("system", "user")


if __name__ == "__main__":
    unittest.main()
