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
                "wire_contract": "text-single-request-v1",
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

    def test_text_single_wire_request_preserves_prompts_and_json_mode_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": True,
                "result": {"content": '{"ok":true}', "meta": {}},
                "wire_contract": "text-single-request-v1",
            }
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(response).encode(), stderr=b""
            )
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="text.ready"), gateway_path=gateway
                )
                result = provider.generate(
                    "system text with punctuation", "user text with full context", json_mode=False
                )

            self.assertTrue(result["ok"])
            self.assertEqual(invoked.call_count, 1)
            request = json.loads(invoked.call_args.kwargs["input"])
            self.assertEqual(request["operation"], "generate")
            self.assertEqual(request["system_prompt"], "system text with punctuation")
            self.assertEqual(request["user_prompt"], "user text with full context")
            self.assertFalse(request["json_mode"])
            self.assertEqual(request["wire_contract"], "text-single-request-v1")

    def test_text_response_without_contract_is_protocol_error_with_one_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {"ok": True, "result": {"content": '{"ok":true}', "meta": {}}}
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(response).encode(), stderr=b""
            )
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="text.ready"), gateway_path=gateway
                )
                with self.assertRaises(ProviderError) as caught:
                    provider.generate("system", "user")
            self.assertEqual(caught.exception.code, "MODEL_GATEWAY_PROTOCOL_ERROR")
            self.assertEqual(invoked.call_count, 1)

    def test_text_format_rejection_preserves_http_status_without_provider_retry(self) -> None:
        for status in (400, 422):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                gateway = Path(directory) / "OpenThesis.exe"
                gateway.write_bytes(b"MZ")
                response = {
                    "ok": False,
                    "wire_contract": "text-single-request-v1",
                    "error": {
                        "code": "MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                        "message": "unsupported format",
                        "http_status": status,
                        "retryable": False,
                    },
                }
                completed = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout=json.dumps(response).encode(), stderr=b""
                )
                with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                    provider = RustModelGatewayProvider(
                        ModelConfig(configured_model_id="text.ready"), gateway_path=gateway
                    )
                    with self.assertRaises(ProviderError) as caught:
                        provider.generate("system", "user")
                self.assertEqual(caught.exception.code, "MODEL_RESPONSE_FORMAT_UNSUPPORTED")
                self.assertEqual(caught.exception.http_status, status)
                self.assertEqual(invoked.call_count, 1)

    def test_vision_payload_is_bounded_png_and_never_uses_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": True,
                "result": {"content": '{"facts":[]}', "meta": {}},
                "wire_contract": "vision-single-request-v1",
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
            self.assertEqual(request["wire_contract"], "vision-single-request-v1")
            self.assertNotIn(request["image_base64"], " ".join(kwargs["args"]))
            with self.assertRaises(ProviderError) as caught:
                provider.generate_vision("system", "user", b"not-png")
            self.assertEqual(caught.exception.code, "MODEL_VISION_PAYLOAD_INVALID")

    def test_single_vision_request_can_disable_format_without_hidden_python_retry(self):
        provider = RustModelGatewayProvider(ModelConfig(configured_model_id="vision.ready"))
        with patch.object(provider, "_invoke_gateway", return_value={"facts": []}) as invoked:
            provider.generate_vision_once("system", "user", b"\x89PNG\r\n\x1a\ncontent", json_mode=False)
        self.assertEqual(invoked.call_count, 1)
        self.assertFalse(invoked.call_args.args[0]["json_mode"])
        self.assertEqual(invoked.call_args.args[0]["wire_contract"], "vision-single-request-v1")

    def test_vision_rejects_a_legacy_response_without_single_wire_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            old_response = {"ok": True, "result": {"content": '{"facts":[]}', "meta": {}}}
            completed = subprocess.CompletedProcess([], 0, json.dumps(old_response).encode(), b"")
            provider = RustModelGatewayProvider(ModelConfig(configured_model_id="vision.ready"), gateway_path=gateway)
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                with self.assertRaises(ProviderError) as caught:
                    provider.generate_vision("system", "user", b"\x89PNG\r\n\x1a\ncontent")
            self.assertEqual(caught.exception.code, "MODEL_GATEWAY_PROTOCOL_ERROR")
            self.assertEqual(invoked.call_count, 1)

    def test_explicit_format_rejection_preserves_http_status_and_has_no_provider_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {"ok": False, "wire_contract": "vision-single-request-v1", "error": {
                "code": "MODEL_RESPONSE_FORMAT_UNSUPPORTED", "message": "unsupported format",
                "http_status": 400, "retryable": False,
            }}
            completed = subprocess.CompletedProcess([], 1, json.dumps(response).encode(), b"")
            provider = RustModelGatewayProvider(ModelConfig(configured_model_id="vision.ready"), gateway_path=gateway)
            with patch("openthesis.providers.subprocess.run", return_value=completed) as invoked:
                with self.assertRaises(ProviderError) as caught:
                    provider.generate_vision("system", "user", b"\x89PNG\r\n\x1a\ncontent")
            self.assertEqual(caught.exception.http_status, 400)
            self.assertEqual(caught.exception.code, "MODEL_RESPONSE_FORMAT_UNSUPPORTED")
            self.assertEqual(invoked.call_count, 1)

    def test_gateway_errors_are_typed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": False,
                "wire_contract": "text-single-request-v1",
                "error": {
                    "code": "MODEL_RATE_LIMITED",
                    "message": "quota exhausted",
                    "retryable": True,
                    "retry_after_seconds": 19,
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
            self.assertEqual(caught.exception.retry_after_seconds, 19)
            self.assertNotIn("ignored", str(caught.exception))

    def test_gateway_oserror_remains_unavailable_and_does_not_echo_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            with patch(
                "openthesis.providers.subprocess.run",
                side_effect=OSError("api_key=sk-private-value"),
            ):
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="model.ready"),
                    gateway_path=gateway,
                )
                with self.assertRaises(ProviderError) as caught:
                    provider.generate("system", "user")
            self.assertEqual(caught.exception.code, "MODEL_GATEWAY_UNAVAILABLE")
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("sk-private-value", str(caught.exception))

    def test_gateway_path_must_be_absolute_existing_file(self) -> None:
        provider = RustModelGatewayProvider(
            ModelConfig(configured_model_id="model.ready"),
            gateway_path=Path("relative.exe"),
        )
        with self.assertRaises(ProviderError) as caught:
            provider.generate("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_GATEWAY_UNAVAILABLE")

    def test_research_cancellation_terminates_the_gateway_process(self) -> None:
        class Process:
            returncode = None

            def __init__(self) -> None:
                self.killed = False

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def communicate(self, input=None, timeout=None):
                return b"", b""

        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            process = Process()
            with patch("openthesis.providers.subprocess.Popen", return_value=process):
                provider = RustModelGatewayProvider(
                    ModelConfig(configured_model_id="model.ready"),
                    gateway_path=gateway,
                )
                provider.set_cancel_check(lambda: True)
                with self.assertRaises(ProviderError) as caught:
                    provider.generate("system", "user")
            self.assertEqual(caught.exception.code, "MODEL_CANCELLED")
            self.assertTrue(process.killed)

    def test_gateway_error_diagnostics_are_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "OpenThesis.exe"
            gateway.write_bytes(b"MZ")
            response = {
                "ok": False,
                "wire_contract": "text-single-request-v1",
                "error": {
                    "code": "MODEL_PROVIDER_ERROR",
                    "message": "prompt=private prompt api_key=sk-private-value " * 100,
                    "retryable": False,
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

            message = str(caught.exception)
            self.assertNotIn("private prompt", message)
            self.assertNotIn("sk-private-value", message)
            self.assertLessEqual(len(message), 300)

    def test_malformed_model_json_keeps_a_safe_parse_error_class(self) -> None:
        result = _parse_model_json("not-json")
        self.assertFalse(result["structured_output_valid"])
        self.assertEqual(result["_response_error"], "invalid_json")

    def test_non_object_model_json_is_rejected_without_raw_payload_diagnostics(self) -> None:
        result = _parse_model_json("[1, 2, 3]")
        self.assertFalse(result["structured_output_valid"])
        self.assertEqual(result["_response_error"], "invalid_shape")
        self.assertNotIn("prompt", result)

    def test_duplicate_json_keys_are_not_silently_last_write_wins(self) -> None:
        for response in (
            '{"revenue":1,"revenue":2}',
            '{"financial_quality":{"profit":1,"profit":1}}',
            'Here is JSON: {"revenue":1,"revenue":2}',
            '```json\n{"revenue":1,"revenue":2}\n```',
        ):
            with self.subTest(response=response):
                result = _parse_model_json(response)
                self.assertFalse(result["structured_output_valid"])
                self.assertEqual(result["_response_error"], "duplicate_json_key")
                self.assertNotIn("revenue", result)

    def test_non_finite_numbers_are_not_valid_model_financials(self) -> None:
        for number in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(number=number):
                result = _parse_model_json('{"growth":' + number + '}')
                self.assertFalse(result["structured_output_valid"])
                self.assertEqual(result["_response_error"], "non_finite_json_number")
                self.assertNotIn("growth", result)

    def test_valid_nested_json_preserves_every_field(self) -> None:
        response = {"financial_quality": {"cash_conversion": 0.82, "notes": ["source checked"]},
                    "implied_expectations": {"growth": 0.12}}
        result = _parse_model_json(json.dumps(response))
        for key, value in response.items():
            self.assertEqual(result[key], value)


if __name__ == "__main__":
    unittest.main()
