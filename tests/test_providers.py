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
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
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

    def test_ollama_protocol(self) -> None:
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

    def test_http_error_is_reported(self) -> None:
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

