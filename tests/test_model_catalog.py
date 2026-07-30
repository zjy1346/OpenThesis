from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openthesis.model_catalog import (
    CLOUD_PRESET_IDS,
    MODEL_PRESETS,
    PRESETS_BY_ID,
    ModelDiscoveryError,
    discover_models,
    infer_model_preset,
    merge_model_ids,
)


class CatalogHandler(BaseHTTPRequestHandler):
    authorization = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).authorization = self.headers.get("Authorization", "")
        status = 200
        if self.path == "/v1/models":
            payload = {"data": [{"id": "remote-b"}, {"id": "remote-a"}]}
        elif self.path == "/api/tags":
            payload = {"models": [{"name": "llama3:8b"}, {"model": "qwen3:8b"}]}
        elif self.path == "/unauthorized/models":
            status, payload = 401, {"error": "secret details must not escape"}
        elif self.path == "/missing/models":
            status, payload = 404, {"error": "missing"}
        elif self.path == "/malformed/models":
            encoded = b"{not-json"
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        elif self.path == "/empty/models":
            payload = {"data": []}
        elif self.path == "/slow/models":
            time.sleep(0.2)
            payload = {"data": [{"id": "late"}]}
        else:
            status, payload = 404, {"error": "missing"}
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError):
            pass


class ModelCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_eight_named_presets_are_unique_and_complete(self) -> None:
        expected = {
            "deepseek",
            "qwen",
            "kimi",
            "glm",
            "openai",
            "gemini",
            "openrouter",
            "ollama",
        }
        self.assertEqual(CLOUD_PRESET_IDS | {"ollama"}, expected)
        ids = [preset.preset_id for preset in MODEL_PRESETS]
        self.assertEqual(len(ids), len(set(ids)))
        for preset_id in expected:
            preset = PRESETS_BY_ID[preset_id]
            self.assertIn(preset.protocol, {"openai-compatible", "ollama"})
            self.assertTrue(preset.base_url.startswith(("https://", "http://localhost")))
            self.assertTrue(preset.recommended_models)

    def test_expected_addresses_and_recommended_models(self) -> None:
        self.assertEqual(
            PRESETS_BY_ID["deepseek"].base_url, "https://api.deepseek.com"
        )
        self.assertEqual(
            PRESETS_BY_ID["openai"].recommended_models[0], "gpt-5.6-terra"
        )
        self.assertEqual(PRESETS_BY_ID["glm"].models_path, None)
        self.assertEqual(PRESETS_BY_ID["ollama"].models_path, "/api/tags")

    def test_catalog_contains_no_credential_material(self) -> None:
        serialized = repr(MODEL_PRESETS).lower()
        for marker in ("ghp_", "github_pat_", "-----begin private key", "sk-proj-"):
            self.assertNotIn(marker, serialized)

    def test_legacy_settings_inference_preserves_unknowns(self) -> None:
        self.assertEqual(
            infer_model_preset("openai-compatible", "https://api.openai.com/v1").preset_id,
            "openai",
        )
        self.assertEqual(
            infer_model_preset("ollama", "http://example.invalid").preset_id,
            "ollama",
        )
        self.assertEqual(
            infer_model_preset("openai-compatible", "https://private.example/v1").preset_id,
            "custom",
        )
        self.assertEqual(infer_model_preset("none", "").preset_id, "none")

    def test_merge_deduplicates_and_keeps_recommendations_first(self) -> None:
        self.assertEqual(
            merge_model_ids(("recommended", "same"), ("same", "remote")),
            ("recommended", "same", "remote"),
        )

    def test_openai_discovery_sends_bearer_and_parses_models(self) -> None:
        preset = PRESETS_BY_ID["openai"]
        models = discover_models(
            preset, f"{self.base_url}/v1", "session-only-secret"
        )
        self.assertEqual(models, ("remote-b", "remote-a"))
        self.assertEqual(CatalogHandler.authorization, "Bearer session-only-secret")

    def test_ollama_discovery_does_not_require_key(self) -> None:
        models = discover_models(PRESETS_BY_ID["ollama"], self.base_url)
        self.assertEqual(models, ("llama3:8b", "qwen3:8b"))

    def test_discovery_failures_are_safe_and_keep_secret_out(self) -> None:
        cases = (
            ("unauthorized", 1),
            ("missing", 1),
            ("malformed", 1),
            ("empty", 1),
            ("slow", 0.02),
        )
        for path, timeout in cases:
            with self.subTest(path=path), self.assertRaises(
                ModelDiscoveryError
            ) as caught:
                discover_models(
                    PRESETS_BY_ID["openai"],
                    f"{self.base_url}/{path}",
                    "never-print-this-secret",
                    timeout_seconds=timeout,
                )
            self.assertNotIn("never-print-this-secret", str(caught.exception))

    def test_provider_without_standard_catalog_uses_builtin_message(self) -> None:
        with self.assertRaises(ModelDiscoveryError):
            discover_models(PRESETS_BY_ID["glm"], self.base_url, "key")


if __name__ == "__main__":
    unittest.main()
