from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        self.retryable = retryable
        super().__init__(message)


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail[:1000]
        super().__init__(
            f"模型接口返回 HTTP {status_code}：{self.detail}",
            retryable=status_code in {408, 409, 425, 429} or status_code >= 500,
        )


@dataclass(slots=True)
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = ""
    temperature: float | None = 0.2
    timeout_seconds: int = 180

    @property
    def enabled(self) -> bool:
        return (
            self.provider not in {"", "none"}
            and bool(self.model)
            and bool(self.base_url)
        )

    @property
    def public_id(self) -> str:
        return f"{self.provider}:{self.model}" if self.enabled else "deterministic"


class ModelProvider(Protocol):
    def test_connection(self) -> str: ...

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]: ...


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        authorization = headers.get("Authorization", "")
        if authorization:
            secret = authorization.removeprefix("Bearer ").strip()
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        raise ProviderHTTPError(exc.code, detail) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"模型接口请求失败：{exc}", retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"模型接口返回了无效 JSON：{exc}") from exc


def _parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {
            "narrative": "",
            "structured_output_valid": False,
            "_response_error": "empty_content",
        }
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
        if text.endswith("```"):
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


class OpenAICompatibleProvider:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    def test_connection(self) -> str:
        result = self.generate(
            "You are a connection test. Return JSON only.",
            '{"task":"Return {\\\"ok\\\":true}."}',
        )
        return "连接成功" if result else "接口响应为空"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        headers = {}
        api_key = self.config.api_key.strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = _post_json(
                f"{self.base_url}/chat/completions",
                payload,
                headers,
                self.config.timeout_seconds,
            )
        except ProviderHTTPError as exc:
            detail = exc.detail.lower()
            if (
                json_mode
                and exc.status_code in {400, 422}
                and "response_format" in detail
                and any(
                    marker in detail
                    for marker in ("unsupported", "not support", "unknown", "invalid")
                )
            ):
                retry_payload = dict(payload)
                retry_payload.pop("response_format", None)
                response = _post_json(
                    f"{self.base_url}/chat/completions",
                    retry_payload,
                    headers,
                    self.config.timeout_seconds,
                )
            else:
                raise
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型响应缺少可用的 message.content") from exc
        parsed = _parse_model_json(str(content or ""))
        choice = response.get("choices", [{}])[0]
        parsed["_response_meta"] = {
            "provider": self.config.provider,
            "model": self.config.model,
            "endpoint_host": urllib.parse.urlparse(self.base_url).hostname or "",
            "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
            "content_length": len(str(content or "")),
        }
        return parsed


class OllamaProvider:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    def test_connection(self) -> str:
        result = self.generate(
            "Return JSON only.",
            '{"task":"Return {\\\"ok\\\":true}."}',
        )
        return "连接成功" if result else "接口响应为空"

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "stream": False,
            "format": "json" if json_mode else "",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.config.temperature is not None:
            payload["options"] = {"temperature": self.config.temperature}
        response = _post_json(
            f"{self.base_url}/api/chat",
            payload,
            {},
            self.config.timeout_seconds,
        )
        try:
            content = response["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("Ollama 响应缺少可用的 message.content") from exc
        parsed = _parse_model_json(str(content or ""))
        parsed["_response_meta"] = {
            "provider": self.config.provider,
            "model": self.config.model,
            "endpoint_host": urllib.parse.urlparse(self.base_url).hostname or "",
            "finish_reason": response.get("done_reason"),
            "content_length": len(str(content or "")),
        }
        return parsed


def create_provider(config: ModelConfig) -> ModelProvider | None:
    if not config.enabled:
        return None
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider == "openai-compatible":
        return OpenAICompatibleProvider(config)
    raise ProviderError(f"暂不支持模型提供方：{config.provider}")
