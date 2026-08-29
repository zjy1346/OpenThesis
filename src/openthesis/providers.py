from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, code: str = "MODEL_ERROR"):
        self.retryable = retryable
        self.code = code
        super().__init__(message)


class ProviderHTTPError(ProviderError):
    """Compatibility error type for callers that classify provider HTTP failures."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail[:1000]
        super().__init__(
            f"模型接口返回 HTTP {status_code}：{self.detail}",
            retryable=status_code in {408, 409, 425, 429} or status_code >= 500,
            code=f"MODEL_HTTP_{status_code}",
        )


_SENSITIVE_GATEWAY_MESSAGE_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "bearer ",
    "credential",
    "password",
    "prompt",
    "secret",
    "system_message",
    "user_message",
    "token",
)


def _safe_gateway_error_message(value: Any) -> str:
    """Keep provider diagnostics useful without echoing request data or secrets."""

    message = str(value).strip()
    if not message:
        return "模型网关调用失败。"
    lowered = message.casefold()
    if any(marker in lowered for marker in _SENSITIVE_GATEWAY_MESSAGE_MARKERS):
        return "模型网关调用失败（错误详情已隐藏）。"
    return message[:160]


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """A non-secret reference to a model configured in the Rust Model Center."""

    configured_model_id: str = ""
    configuration_version: int = 1
    role: str = "primary"
    timeout_seconds: int = 180

    @property
    def enabled(self) -> bool:
        return bool(self.configured_model_id.strip())

    @property
    def provider(self) -> str:
        return "gateway" if self.enabled else "none"

    @property
    def model(self) -> str:
        return self.configured_model_id.strip()

    @property
    def public_id(self) -> str:
        if not self.enabled:
            return "deterministic"
        return (
            f"gateway:{self.configured_model_id.strip()}"
            f"@{max(1, int(self.configuration_version))}"
        )


class ModelProvider(Protocol):
    def test_connection(self) -> str: ...

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]: ...


def _parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {
            "narrative": "",
            "structured_output_valid": False,
            "_response_error": "empty_content",
        }
    fence = chr(96) * 3
    if text.startswith(fence):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
        if text.endswith(fence):
            text = text[:-3]
        text = text.strip()
    parse_error_class: str | None = None
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        parse_error_class = "invalid_json"
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                result = {"narrative": text, "structured_output_valid": False}
        else:
            result = {"narrative": text, "structured_output_valid": False}
    if not isinstance(result, dict):
        return {
            "result": result,
            "structured_output_valid": False,
            "_response_error": "invalid_shape",
        }
    result.setdefault("structured_output_valid", True)
    if parse_error_class and not result.get("structured_output_valid", True):
        result.setdefault("_response_error", parse_error_class)
    return result


class RustModelGatewayProvider:
    """Use the Tauri executable's bounded gateway mode; Python never receives API keys."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        gateway_path: str | os.PathLike[str] | None = None,
    ):
        self.config = config
        configured_path = gateway_path or os.environ.get("OPENTHESIS_MODEL_GATEWAY_PATH", "")
        self.gateway_path = Path(configured_path) if configured_path else None

    def test_connection(self) -> str:
        result = self.generate(
            "You are a connection test. Return JSON only.",
            '{"task":"Return {\\"ok\\":true}."}',
        )
        return "连接成功" if result else "接口响应为空"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        request = {
            "operation": "generate",
            "configured_model_id": self.config.configured_model_id,
            "system_prompt": str(system_prompt),
            "user_prompt": str(user_prompt),
            "json_mode": bool(json_mode),
        }
        return self._invoke_gateway(request)

    def generate_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_png: bytes,
    ) -> dict[str, Any]:
        import base64

        if not image_png or len(image_png) > 8 * 1024 * 1024 or not image_png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ProviderError(
                "视觉页面不是受支持的有界 PNG。",
                code="MODEL_VISION_PAYLOAD_INVALID",
            )
        request = {
            "operation": "vision",
            "configured_model_id": self.config.configured_model_id,
            "system_prompt": str(system_prompt),
            "user_prompt": str(user_prompt),
            "json_mode": True,
            "image_media_type": "image/png",
            "image_base64": base64.b64encode(image_png).decode("ascii"),
        }
        return self._invoke_gateway(request)

    def _invoke_gateway(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enabled:
            raise ProviderError("未选择已配置模型。", code="MODEL_CONFIGURATION_ERROR")
        path = self._validated_gateway_path()
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            raise ProviderError(
                "模型请求超过网关大小限制。",
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
            )
        options: dict[str, Any] = {
            "args": [str(path), "--model-gateway"],
            "input": encoded,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "check": False,
            "timeout": max(5, min(600, int(self.config.timeout_seconds))) + 20,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run(**options)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                "模型网关调用超时。",
                retryable=True,
                code="MODEL_TIMEOUT",
            ) from exc
        except OSError as exc:
            raise ProviderError(
                "模型网关无法启动。",
                retryable=True,
                code="MODEL_GATEWAY_UNAVAILABLE",
            ) from exc
        if len(completed.stdout) > 16 * 1024 * 1024:
            raise ProviderError(
                "模型网关响应超过大小限制。",
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
            )
        try:
            response = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "模型网关返回了无效响应。",
                retryable=completed.returncode != 0,
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
            ) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else None
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code", "MODEL_GATEWAY_ERROR"))[:80]
            message = _safe_gateway_error_message(error.get("message", "模型网关调用失败。"))
            raise ProviderError(
                f"{code}: {message}",
                retryable=bool(error.get("retryable", False)),
                code=code,
            )
        payload = response.get("result")
        if not isinstance(payload, dict):
            raise ProviderError(
                "模型网关响应缺少 result。",
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
            )
        content = str(payload.get("content", ""))
        result = _parse_model_json(content)
        meta = payload.get("meta")
        result["_response_meta"] = dict(meta) if isinstance(meta, dict) else {}
        return result

    def _validated_gateway_path(self) -> Path:
        path = self.gateway_path
        if path is None or not path.is_absolute() or not path.is_file():
            raise ProviderError(
                "模型网关路径不可用，请从 OpenThesis 桌面应用发起研究。",
                code="MODEL_GATEWAY_UNAVAILABLE",
            )
        return path


def create_provider(config: ModelConfig) -> ModelProvider | None:
    if not config.enabled:
        return None
    return RustModelGatewayProvider(config)
