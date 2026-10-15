from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "MODEL_ERROR",
        retry_after_seconds: int | float | None = None,
        http_status: int | None = None,
        response_received: bool = False,
        processing_phase: str = "",
        content_path: str = "",
        safe_receipt: dict | None = None,
        diagnostic_trace_id: str = "",
    ):
        self.retryable = retryable
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status
        self.response_received = response_received
        self.processing_phase = processing_phase
        self.content_path = content_path
        self.safe_receipt = safe_receipt
        self.diagnostic_trace_id = diagnostic_trace_id
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
            http_status=status_code,
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
    connection_id: str = ""

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
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non_finite_json_number")

    def parse(value):
        return json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)

    try:
        result = parse(text)
    except ValueError as exc:
        if not isinstance(exc, json.JSONDecodeError):
            return {"narrative": text, "structured_output_valid": False, "_response_error": str(exc)}
        parse_error_class = "invalid_json"
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                result = parse(text[start : end + 1])
            except ValueError as inner:
                from .research_execution.model_output import recover_closed_json
                partial, issues = recover_closed_json(text)
                result = ({**partial, "structured_output_valid": True, "_output_incomplete": True,
                           "_output_normalization_issues": list(issues)} if partial else
                          {"narrative": text, "structured_output_valid": False})
                if not isinstance(inner, json.JSONDecodeError):
                    result["_response_error"] = str(inner)
        else:
            from .research_execution.model_output import recover_closed_json
            partial, issues = recover_closed_json(text)
            result = ({**partial, "structured_output_valid": True, "_output_incomplete": True,
                       "_output_normalization_issues": list(issues)} if partial else
                      {"narrative": text, "structured_output_valid": False})
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
        self._cancel_check: Callable[[], bool] | None = None
        self._request_deadline: float | None = None

    def for_request_budget(self, deadline: float, cancel_check=None):
        """Isolated transport budget, without mutating shared model settings."""
        scoped = RustModelGatewayProvider(self.config, gateway_path=self.gateway_path)
        scoped._request_deadline = deadline
        original_cancel = self._cancel_check
        scoped._cancel_check = lambda: bool((original_cancel and original_cancel())
                                            or (cancel_check and cancel_check()))
        return scoped

    def set_cancel_check(self, cancel_check: Callable[[], bool] | None) -> None:
        """Attach the run-scoped cancellation signal without changing prompts."""

        self._cancel_check = cancel_check

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
        return self.generate_once(system_prompt, user_prompt, json_mode=json_mode)

    def generate_once(
        self, system_prompt: str, user_prompt: str, *, json_mode: bool = True,
    ) -> dict[str, Any]:
        """Exactly one text wire attempt; its owner journals any compensation."""
        request = {
            "operation": "generate",
            "configured_model_id": self.config.configured_model_id,
            "system_prompt": str(system_prompt),
            "user_prompt": str(user_prompt),
            "json_mode": bool(json_mode),
            "wire_contract": "text-single-request-v1",
        }
        return self._invoke_gateway(request)

    def generate_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_png: bytes,
    ) -> dict[str, Any]:
        return self.generate_vision_once(system_prompt, user_prompt, image_png)

    def generate_vision_once(
        self,
        system_prompt: str,
        user_prompt: str,
        image_png: bytes,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        """Exactly one Rust wire attempt; format fallback belongs to its owner."""
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
            "json_mode": bool(json_mode),
            "wire_contract": "vision-single-request-v1",
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
        timeout_seconds = max(5, min(600, int(self.config.timeout_seconds))) + 20
        if self._request_deadline is not None:
            timeout_seconds = min(timeout_seconds, self._request_deadline - time.monotonic())
            if timeout_seconds <= 0:
                raise ProviderError("模型请求预算已耗尽。", retryable=True, code="MODEL_TIMEOUT")
        options: dict[str, Any] = {
            "args": [str(path), "--model-gateway"],
            "input": encoded,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "check": False,
            "timeout": timeout_seconds,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = (
                self._run_cancellable_gateway(
                    options["args"], encoded, timeout_seconds,
                    int(options.get("creationflags", 0)),
                )
                if self._cancel_check is not None
                else subprocess.run(**options)
            )
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
                response_received=True, processing_phase="gateway_decode", content_path="$.wire",
            )
        try:
            response = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "模型网关返回了无效响应。",
                retryable=completed.returncode != 0,
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
                response_received=True, processing_phase="gateway_decode", content_path="$.wire",
            ) from exc
        if request.get("wire_contract") and (
            not isinstance(response, dict)
            or response.get("wire_contract") != request["wire_contract"]
        ):
            raise ProviderError(
                "模型网关协议不匹配，请使用与当前核心配套的桌面版本。",
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
                response_received=True, processing_phase="gateway_identity", content_path="$.wire.wire_contract",
            )
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else None
            error = error if isinstance(error, dict) else {}
            code = str(error.get("code", "MODEL_GATEWAY_ERROR"))[:80]
            message = _safe_gateway_error_message(error.get("message", "模型网关调用失败。"))
            retry_after = error.get("retry_after_seconds")
            try:
                retry_after_seconds = (
                    max(1, int(retry_after))
                    if retry_after is not None and code == "MODEL_RATE_LIMITED"
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                retry_after_seconds = None
            raise ProviderError(
                f"{code}: {message}",
                retryable=bool(error.get("retryable", False)),
                code=code,
                retry_after_seconds=retry_after_seconds,
                http_status=error.get("http_status") if type(error.get("http_status")) is int else None,
            )
        payload = response.get("result")
        if not isinstance(payload, dict):
            raise ProviderError(
                "模型网关响应缺少 result。",
                code="MODEL_GATEWAY_PROTOCOL_ERROR",
            )
        content = str(payload.get("content", ""))
        from .research_execution.responses import gateway_received_response
        result = gateway_received_response(content, payload.get("meta"), _parse_model_json,
            expected_model_id=self.config.configured_model_id,
            expected_connection_id=self.config.connection_id)
        finish_reason = result["_response_meta"].get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason.casefold() in {"refusal", "content_filter"}:
            # A safety refusal may be empty or even syntactically valid JSON.
            # It is neither accepted research nor permission to retry the task.
            result["structured_output_valid"] = False
            result["_response_error"] = "provider_refusal"
        return result

    def _run_cancellable_gateway(
        self,
        args: list[str],
        encoded: bytes,
        timeout_seconds: float,
        creationflags: int,
    ) -> subprocess.CompletedProcess[bytes]:
        """Supervise the gateway so cancel and hard-timeout terminate it."""

        started = time.monotonic()
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        first = True
        while True:
            if self._cancel_check and self._cancel_check():
                process.kill()
                process.communicate()
                raise ProviderError(
                    "模型调用已取消。", retryable=False, code="MODEL_CANCELLED"
                )
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise ProviderError(
                    "模型网关调用超时。", retryable=True, code="MODEL_TIMEOUT"
                )
            try:
                stdout, _ = process.communicate(
                    input=encoded if first else None,
                    timeout=min(0.1, remaining),
                )
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=int(process.returncode or 0),
                    stdout=stdout,
                    stderr=b"",
                )
            except subprocess.TimeoutExpired:
                first = False
                continue

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
