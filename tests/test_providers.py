from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openthesis.providers import (
    ModelConfig,
    ProviderError,
    RustModelGatewayProvider,
    _parse_model_json,
    create_provider,
)


class ProviderTests(unittest.TestCase):
    def test_model_config_is_a_non_secret_versioned_reference(self) -> None:
        config = ModelConfig(
            configured_model_id="deepseek.personal.chat",
            configuration_version=7,
            role="primary",
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.provider, "gateway")
        self.assertEqual(config.public_id, "gateway:deepseek.personal.chat@7")
        self.assertFalse(hasattr(config, "api_key"))
        self.assertFalse(hasattr(config, "base_url"))

    def test_create_provider_only_constructs_the_rust_gateway(self) -> None:
        self.assertIsNone(create_provider(ModelConfig()))
        provider = create_provider(ModelConfig(configured_model_id="local.qwen"))
        self.assertIsInstance(provider, RustModelGatewayProvider)

    def test_gateway_request_contains_only_non_secret_model_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": True,
                "result": {
                    "content": '{"ok":true}',
                    "meta": {
                        "configured_model_id": "model.ready",
                        "configuration_version": 3,
                        "provider_id": "openai",
                    },
                },
            }
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(response).encode(), stderr=b""
            )
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                provider = RustModelGatewayProvider(
                    ModelConfig(
                        configured_model_id="model.ready",
                        configuration_version=3,
                    ),
                    gateway_path=gateway,
                )
                result = provider.generate("system", "user")

            self.assertTrue(result["ok"])
            self.assertEqual(result["_response_meta"]["configuration_version"], 3)
            kwargs = invoked.call_args.kwargs
            self.assertEqual(kwargs["args"], [str(gateway), "--model-gateway"])
            request = json.loads(kwargs["input"])
            self.assertEqual(request["configured_model_id"], "model.ready")
            self.assertEqual(request["operation"], "generate")
            serialized = json.dumps(request)
            self.assertNotIn("api_key", serialized)
            self.assertNotIn("base_url", serialized)
            self.assertNotIn("secret", serialized)

    def test_vision_payload_is_bounded_png_and_never_uses_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": True,
                "result": {"content": '{"facts":[]}', "meta": {}},
            }
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(response).encode(), stderr=b""
            )
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="vision.ready", role="vision"),
                    gateway_path=gateway,
                )
                provider.generate_vision("system", "user", b"\x89PNG\r\n\x1a\ncontent")

            kwargs = invoked.call_args.kwargs
            request = json.loads(kwargs["input"])
            self.assertEqual(request["operation"], "vision")
            self.assertEqual(request["image_media_type"], "image/png")
            self.assertNotIn(request["image_base64"], " ".join(kwargs["args"]))
            with self.assertRaises(ProviderError) as caught:
                provider.generate_vision("system", "user", b"not-png")
            self.assertEqual(caught.exception.code, "MODEL_VISION_PAYLOAD_INVALID")

    def test_gateway_errors_are_typed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": False,
                "error": {
                    "code": "MODEL_RATE_LIMITED",
                    "message": "quota exhausted",
                    "retryable": True,
                },
            }
            completed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=json.dumps(response).encode(), stderr=b"ignored"
            )
            with patch("openthesis.providers.subprocess.run", return_value=completed):
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="model.ready"),
                    gateway_path=gateway,
                )
                with self.assertRaises(ProviderError) as caught:
                    provider.generate("system", "user")
            self.assertEqual(caught.exception.code, "MODEL_RATE_LIMITED")
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("ignored", str(caught.exception))

    def test_gateway_path_must_be_absolute_existing_file(self) -> None:
        provider = RustModelGatewayProvider(
            ModelConfig(configured_model_id="model.ready"),
            gateway_path=Path("relative.exe"),
        )
        with self.assertRaises(ProviderError) as caught:
            provider.generate("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_GATEWAY_UNAVAILABLE")

    def test_malformed_model_json_keeps_a_safe_parse_error_class(self) -> None:
        result = _parse_model_json("not-json")
        self.assertFalse(result["structured_output_valid"])
        self.assertEqual(result["_response_error"], "invalid_json")

    def test_non_object_model_json_is_rejected_without_raw_payload_diagnostics(self) -> None:
        result = _parse_model_json("[1, 2, 3]")
        self.assertFalse(result["structured_output_valid"])
        self.assertEqual(result["_response_error"], "invalid_shape")
        self.assertNotIn("prompt", result)


if __name__ == "__main__":
    unittest.main()
