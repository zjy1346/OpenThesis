from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.2
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
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"模型接口返回 HTTP {exc.code}：{detail[:1000]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProviderError(f"模型接口请求失败：{exc}") from exc


def _parse_model_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
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
        return {"result": result, "structured_output_valid": True}
    result.setdefault("structured_output_valid", True)
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
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = _post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers,
            self.config.timeout_seconds,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"无法解析模型响应：{response}") from exc
        return _parse_model_json(str(content))


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
        payload = {
            "model": self.config.model,
            "stream": False,
            "format": "json" if json_mode else "",
            "options": {"temperature": self.config.temperature},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = _post_json(
            f"{self.base_url}/api/chat",
            payload,
            {},
            self.config.timeout_seconds,
        )
        try:
            content = response["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"无法解析 Ollama 响应：{response}") from exc
        return _parse_model_json(str(content))


def create_provider(config: ModelConfig) -> ModelProvider | None:
    if not config.enabled:
        return None
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider == "openai-compatible":
        return OpenAICompatibleProvider(config)
    raise ProviderError(f"暂不支持模型提供方：{config.provider}")
