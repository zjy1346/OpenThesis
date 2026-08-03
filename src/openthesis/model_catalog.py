from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from .i18n import model_preset_id_from_label, model_preset_label

class ModelDiscoveryError(RuntimeError):
    """A user-safe model catalog error which never contains credentials."""


@dataclass(frozen=True, slots=True)
class ModelPreset:
    preset_id: str
    label: str
    region: str
    protocol: str
    base_url: str
    recommended_models: tuple[str, ...]
    models_path: str | None
    help_url: str
    requires_api_key: bool = True
    temperature: float | None = None


MODEL_PRESETS: tuple[ModelPreset, ...] = (
    ModelPreset(
        "none",
        "不调用 AI（本地确定性分析）",
        "离线",
        "none",
        "",
        (),
        None,
        "",
        requires_api_key=False,
    ),
    ModelPreset(
        "deepseek",
        "国内 · DeepSeek",
        "国内",
        "openai-compatible",
        "https://api.deepseek.com",
        ("deepseek-v4-pro", "deepseek-v4-flash"),
        "/models",
        "https://api-docs.deepseek.com/",
        temperature=0.2,
    ),
    ModelPreset(
        "qwen",
        "国内 · Qwen（通义千问）",
        "国内",
        "openai-compatible",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ("qwen3.7-plus", "qwen-plus"),
        "/models",
        "https://help.aliyun.com/zh/model-studio/get-api-key",
        temperature=0.2,
    ),
    ModelPreset(
        "kimi",
        "国内 · Kimi",
        "国内",
        "openai-compatible",
        "https://api.moonshot.cn/v1",
        ("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"),
        "/models",
        "https://platform.moonshot.cn/console/api-keys",
    ),
    ModelPreset(
        "kimi-global",
        "国外 · Kimi International",
        "国外",
        "openai-compatible",
        "https://api.moonshot.ai/v1",
        ("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5"),
        "/models",
        "https://platform.kimi.ai/console/api-keys",
    ),
    ModelPreset(
        "glm",
        "国内 · GLM（智谱）",
        "国内",
        "openai-compatible",
        "https://open.bigmodel.cn/api/paas/v4",
        ("glm-5.2", "glm-5.1", "glm-4.7"),
        None,
        "https://open.bigmodel.cn/usercenter/apikeys",
    ),
    ModelPreset(
        "openai",
        "国外 · OpenAI",
        "国外",
        "openai-compatible",
        "https://api.openai.com/v1",
        ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"),
        "/models",
        "https://platform.openai.com/api-keys",
    ),
    ModelPreset(
        "gemini",
        "国外 · Gemini",
        "国外",
        "openai-compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        ("gemini-3.6-flash", "gemini-3.5-flash-lite"),
        "/models",
        "https://aistudio.google.com/app/apikey",
        temperature=0.2,
    ),
    ModelPreset(
        "openrouter",
        "国外 · OpenRouter",
        "国外",
        "openai-compatible",
        "https://openrouter.ai/api/v1",
        (
            "openai/gpt-5.6-terra",
            "google/gemini-3.6-flash",
            "deepseek/deepseek-v4-pro",
        ),
        "/models",
        "https://openrouter.ai/settings/keys",
    ),
    ModelPreset(
        "ollama",
        "本地 · Ollama",
        "本地",
        "ollama",
        "http://localhost:11434",
        ("qwen3:8b", "deepseek-r1:8b", "gpt-oss:20b"),
        "/api/tags",
        "https://ollama.com/download",
        requires_api_key=False,
        temperature=0.2,
    ),
    ModelPreset(
        "custom",
        "自定义 · OpenAI-compatible",
        "自定义",
        "openai-compatible",
        "",
        (),
        "/models",
        "",
    ),
)

PRESETS_BY_ID = {preset.preset_id: preset for preset in MODEL_PRESETS}
PRESET_LABELS = tuple(preset.label for preset in MODEL_PRESETS)
PRESETS_BY_LABEL = {preset.label: preset for preset in MODEL_PRESETS}
CLOUD_PRESET_IDS = frozenset(
    preset.preset_id
    for preset in MODEL_PRESETS
    if preset.preset_id not in {"none", "ollama", "custom"}
)


def get_model_preset(value: str) -> ModelPreset:
    localized_id = model_preset_id_from_label(value)
    return (
        PRESETS_BY_ID.get(value)
        or PRESETS_BY_LABEL.get(value)
        or (PRESETS_BY_ID.get(localized_id) if localized_id else None)
        or PRESETS_BY_ID["custom"]
    )


def preset_labels(language: str) -> tuple[str, ...]:
    return tuple(model_preset_label(preset.preset_id, language) for preset in MODEL_PRESETS)


def merge_model_ids(
    recommended: Iterable[str], discovered: Iterable[str]
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in (*tuple(recommended), *tuple(discovered)):
        model_id = str(raw).strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    return tuple(result)


def infer_model_preset(provider: str, base_url: str) -> ModelPreset:
    provider = provider.strip().lower()
    if provider in {"", "none"}:
        return PRESETS_BY_ID["none"]
    if provider == "ollama":
        return PRESETS_BY_ID["ollama"]
    normalized = base_url.strip().lower().rstrip("/")
    for preset in MODEL_PRESETS:
        preset_url = preset.base_url.lower().rstrip("/")
        if preset_url and (
            normalized == preset_url
            or normalized.startswith(f"{preset_url}/")
            or preset_url.startswith(f"{normalized}/")
        ):
            return preset
    return PRESETS_BY_ID["custom"]


def discover_models(
    preset: ModelPreset,
    base_url: str,
    api_key: str = "",
    *,
    timeout_seconds: float = 12,
) -> tuple[str, ...]:
    if not preset.models_path:
        raise ModelDiscoveryError("此提供方暂不提供在线模型列表，请使用内置列表或手动填写。")
    if preset.requires_api_key and not api_key.strip():
        raise ModelDiscoveryError("请先填写本次会话的 API Key，再刷新在线模型。")
    url = f"{base_url.rstrip('/')}{preset.models_path}"
    headers = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            message = (
                f"认证失败（HTTP {exc.code}，地址 {_safe_endpoint_label(url)}），"
                "请检查 API Key、区域预设和账号权限。"
            )
        elif exc.code == 404:
            message = (
                f"在线模型目录不存在（HTTP 404，地址 {_safe_endpoint_label(url)}），"
                "请检查区域预设或使用内置列表。"
            )
        else:
            message = (
                f"在线模型目录返回 HTTP {exc.code}（地址 {_safe_endpoint_label(url)}），"
                "已保留内置列表。"
            )
        raise ModelDiscoveryError(message) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ModelDiscoveryError(
            f"在线模型目录连接超时或不可达（地址 {_safe_endpoint_label(url)}），已保留内置列表。"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelDiscoveryError(
            f"在线模型目录返回了无法识别的数据（地址 {_safe_endpoint_label(url)}），已保留内置列表。"
        ) from exc

    if preset.protocol == "ollama":
        raw_models = payload.get("models", []) if isinstance(payload, dict) else []
        discovered = [
            item.get("name") or item.get("model")
            for item in raw_models
            if isinstance(item, dict)
        ]
    else:
        raw_models = payload.get("data", []) if isinstance(payload, dict) else []
        discovered = [
            item.get("id") for item in raw_models if isinstance(item, dict)
        ]
    models = merge_model_ids((), (item for item in discovered if item))
    if not models:
        raise ModelDiscoveryError(
            f"在线模型目录为空（地址 {_safe_endpoint_label(url)}），已保留内置列表。"
        )
    return models


def _safe_endpoint_label(url: str) -> str:
    """Return only a host[:port] for diagnostics; never expose URL credentials."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "unknown host"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return host
    except ValueError:
        return "unknown host"
