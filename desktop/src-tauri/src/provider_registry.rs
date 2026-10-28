use serde::Serialize;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct ModelPreset {
    pub model_id: &'static str,
    pub alias: &'static str,
    pub capabilities: &'static [&'static str],
    pub billing_class: &'static str,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub struct ProviderDefinition {
    pub provider_id: &'static str,
    pub display_name: &'static str,
    pub category: &'static str,
    pub region: &'static str,
    pub base_url: &'static str,
    pub help_url: &'static str,
    pub requires_api_key: bool,
    pub supports_discovery: bool,
    pub free_hint: bool,
    pub allowed_hosts: &'static [&'static str],
    pub recommended_models: &'static [ModelPreset],
    pub models_path: Option<&'static str>,
    pub default_test_model_id: Option<&'static str>,
}

const EMPTY_HOSTS: &[&str] = &[];
const TEXT_JSON: &[&'static str] = &["text_chat", "structured_json"];
const DEEPSEEK_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "deepseek-v4-pro", alias: "DeepSeek V4 Pro", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "deepseek-v4-flash", alias: "DeepSeek V4 Flash", capabilities: TEXT_JSON, billing_class: "paid" },
];
const QWEN_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "qwen3.8-max", alias: "Qwen 3.8 Max", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "qwen3.7-plus", alias: "Qwen 3.7 Plus", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "qwen3.7-flash", alias: "Qwen 3.7 Flash", capabilities: TEXT_JSON, billing_class: "paid" },
];
const KIMI_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "kimi-k2.5", alias: "Kimi K2.5", capabilities: TEXT_JSON, billing_class: "paid" },
];
const GLM_MODELS: &[ModelPreset] = &[ModelPreset { model_id: "glm-5.2", alias: "GLM 5.2", capabilities: TEXT_JSON, billing_class: "paid" }];
const OPENAI_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "gpt-5.6-terra", alias: "GPT-5.6 Terra", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "gpt-5.6-sol", alias: "GPT-5.6 Sol", capabilities: TEXT_JSON, billing_class: "paid" },
];
const GEMINI_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "gemini-3.7-flash", alias: "Gemini 3.7 Flash", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "gemini-3.6-flash", alias: "Gemini 3.6 Flash", capabilities: TEXT_JSON, billing_class: "paid" },
    ModelPreset { model_id: "gemini-3.5-flash-lite", alias: "Gemini 3.5 Flash Lite", capabilities: TEXT_JSON, billing_class: "paid" },
];
const OPENROUTER_MODELS: &[ModelPreset] = &[ModelPreset { model_id: "openrouter/free", alias: "OpenRouter Free", capabilities: TEXT_JSON, billing_class: "free_tier" }];
const OLLAMA_MODELS: &[ModelPreset] = &[
    ModelPreset { model_id: "qwen3:8b", alias: "Qwen 3 8B", capabilities: TEXT_JSON, billing_class: "unknown" },
    ModelPreset { model_id: "deepseek-r1:8b", alias: "DeepSeek R1 8B", capabilities: TEXT_JSON, billing_class: "unknown" },
];
static PROVIDERS: &[ProviderDefinition] = &[
    ProviderDefinition {
        provider_id: "deepseek",
        display_name: "DeepSeek",
        category: "cloud",
        region: "global",
        base_url: "https://api.deepseek.com",
        help_url: "https://platform.deepseek.com/api_keys",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: &["api.deepseek.com"],
        recommended_models: DEEPSEEK_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("deepseek-v4-flash"),
    },
    ProviderDefinition {
        provider_id: "qwen",
        display_name: "Qwen / DashScope",
        category: "cloud",
        region: "global",
        base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
        help_url: "https://dashscope.console.aliyun.com/apiKey",
        requires_api_key: true,
        supports_discovery: false,
        free_hint: false,
        allowed_hosts: &["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"],
        recommended_models: QWEN_MODELS,
        models_path: None,
        default_test_model_id: Some("qwen3.7-flash"),
    },
    ProviderDefinition {
        provider_id: "kimi_cn",
        display_name: "Kimi",
        category: "cloud",
        region: "cn",
        base_url: "https://api.moonshot.cn/v1",
        help_url: "https://platform.moonshot.cn/console/api-keys",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: &["api.moonshot.cn"],
        recommended_models: KIMI_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("kimi-k2.5"),
    },
    ProviderDefinition {
        provider_id: "kimi_global",
        display_name: "Kimi (Global)",
        category: "cloud",
        region: "global",
        base_url: "https://api.moonshot.ai/v1",
        help_url: "https://platform.moonshot.ai/console/api-keys",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: &["api.moonshot.ai"],
        recommended_models: KIMI_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("kimi-k2.5"),
    },
    ProviderDefinition {
        provider_id: "glm",
        display_name: "GLM",
        category: "cloud",
        region: "global",
        base_url: "https://open.bigmodel.cn/api/paas/v4",
        help_url: "https://open.bigmodel.cn/usercenter/apikeys",
        requires_api_key: true,
        supports_discovery: false,
        free_hint: false,
        allowed_hosts: &["open.bigmodel.cn"],
        recommended_models: GLM_MODELS,
        models_path: None,
        default_test_model_id: Some("glm-5.2"),
    },
    ProviderDefinition {
        provider_id: "openai",
        display_name: "OpenAI",
        category: "cloud",
        region: "global",
        base_url: "https://api.openai.com/v1",
        help_url: "https://platform.openai.com/api-keys",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: &["api.openai.com"],
        recommended_models: OPENAI_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("gpt-5.6-terra"),
    },
    ProviderDefinition {
        provider_id: "gemini",
        display_name: "Gemini",
        category: "cloud",
        region: "global",
        base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
        help_url: "https://aistudio.google.com/app/apikey",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: true,
        allowed_hosts: &["generativelanguage.googleapis.com"],
        recommended_models: GEMINI_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("gemini-3.7-flash"),
    },
    ProviderDefinition {
        provider_id: "openrouter",
        display_name: "OpenRouter",
        category: "cloud",
        region: "global",
        base_url: "https://openrouter.ai/api/v1",
        help_url: "https://openrouter.ai/settings/keys",
        requires_api_key: true,
        supports_discovery: true,
        free_hint: true,
        allowed_hosts: &["openrouter.ai"],
        recommended_models: OPENROUTER_MODELS,
        models_path: Some("/models"),
        default_test_model_id: Some("openrouter/free"),
    },
    ProviderDefinition {
        provider_id: "ollama",
        display_name: "Ollama",
        category: "local",
        region: "local",
        base_url: "http://127.0.0.1:11434",
        help_url: "https://ollama.com/download",
        requires_api_key: false,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: &["127.0.0.1", "localhost", "::1"],
        recommended_models: OLLAMA_MODELS,
        models_path: Some("/api/tags"),
        default_test_model_id: None,
    },
    ProviderDefinition {
        provider_id: "custom",
        display_name: "Custom OpenAI-compatible",
        category: "custom",
        region: "custom",
        base_url: "",
        help_url: "",
        requires_api_key: false,
        supports_discovery: true,
        free_hint: false,
        allowed_hosts: EMPTY_HOSTS,
        recommended_models: &[],
        models_path: Some("/models"),
        default_test_model_id: None,
    },
];

pub fn all() -> Vec<ProviderDefinition> {
    PROVIDERS.to_vec()
}
pub fn find(provider_id: &str) -> Option<&'static ProviderDefinition> {
    PROVIDERS
        .iter()
        .find(|provider| provider.provider_id == provider_id)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct AppliedOutputTokenBudget {
    pub(crate) wire_field: &'static str,
    pub(crate) applied_tokens: u32,
    pub(crate) semantics: &'static str,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum OutputTokenBudgetSupport {
    Applied(AppliedOutputTokenBudget),
    Unsupported,
}

/// Add an output budget only where the provider's current chat contract is
/// known. Returning `Unsupported` is intentional: callers must not infer a
/// vendor field from the OpenAI-compatible label alone.
pub(crate) fn apply_output_token_budget(
    provider_id: &str,
    model_id: &str,
    payload: &mut serde_json::Value,
    max_output_tokens: u32,
) -> OutputTokenBudgetSupport {
    let model_id = model_id.to_ascii_lowercase();
    // These are exact model IDs with official model-specific maximum output
    // limits. Unknown aliases and dynamically routed/local models intentionally
    // do not receive a guessed cap.
    let (field, model_max_output_tokens) = match provider_id {
        "openai" => match model_id.as_str() {
            "gpt-5.6-sol" | "gpt-5.6-terra" => ("max_completion_tokens", 128_000),
            _ => return OutputTokenBudgetSupport::Unsupported,
        },
        "deepseek" => match model_id.as_str() {
            // DeepSeek lists the currently routed Flash and V4-Pro maximum as
            // 393,216 output tokens. The legacy V4 Flash ID remains accepted.
            "deepseek-v4-flash" | "deepseek-v4-pro" | "deepseek-flash" => {
                ("max_tokens", 393_216)
            }
            _ => return OutputTokenBudgetSupport::Unsupported,
        },
        "qwen" => match model_id.as_str() {
            "qwen3.5-plus" => ("max_completion_tokens", 65_536),
            "qwen3.8-max" | "qwen3.7-plus" | "qwen3.7-flash" => {
                ("max_completion_tokens", 131_072)
            }
            // Older Qwen's max_tokens may only bound visible answer tokens,
            // not the full generation including thinking.
            _ => return OutputTokenBudgetSupport::Unsupported,
        },
        _ => return OutputTokenBudgetSupport::Unsupported,
    };
    if max_output_tokens == 0 {
        return OutputTokenBudgetSupport::Unsupported;
    }
    let applied_tokens = max_output_tokens.min(model_max_output_tokens);
    let Some(object) = payload.as_object_mut() else {
        return OutputTokenBudgetSupport::Unsupported;
    };
    object.insert(field.to_string(), serde_json::json!(applied_tokens));
    OutputTokenBudgetSupport::Applied(AppliedOutputTokenBudget {
        wire_field: field,
        applied_tokens,
        semantics: "total_generated_tokens_including_reasoning",
    })
}

/// First-party contracts are recorded in docs/research/provider-contracts-3.1.0.md.
/// Gemini's compatible path has no bound terminal/usage contract in those
/// sources; custom endpoints have no provider contract. Neither is guessed.
pub(crate) fn text_stream_protocol(provider_id: &str) -> Option<&'static str> {
    match provider_id {
        "openai" | "deepseek" | "qwen" | "kimi_cn" | "kimi_global" | "glm" | "openrouter" => Some("sse"),
        "ollama" => Some("ndjson"),
        _ => None,
    }
}

pub(crate) fn stream_requests_usage(provider_id: &str) -> bool {
    matches!(provider_id, "openai" | "deepseek" | "qwen" | "kimi_cn" | "kimi_global")
}

pub fn validate_provider_id(value: &str) -> Result<(), String> {
    if value.len() > 64
        || value.is_empty()
        || !value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
    {
        return Err("provider id is invalid".to_string());
    }
    Ok(())
}
pub fn validate_connection_id(value: &str) -> Result<(), String> {
    if value.len() > 128
        || value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    {
        return Err("connection id is invalid".to_string());
    }
    Ok(())
}
pub fn validate_model_id(value: &str) -> Result<(), String> {
    if value.len() > 256 || value.is_empty() || value.chars().any(char::is_control) {
        return Err("model id is invalid".to_string());
    }
    Ok(())
}
pub fn validate_endpoint(provider_id: &str, endpoint: &str) -> Result<(), String> {
    if endpoint.len() > 2048 || endpoint.is_empty() {
        return Err("endpoint is invalid".to_string());
    }
    find(provider_id).ok_or_else(|| "unknown provider".to_string())?;
    let url = tauri::Url::parse(endpoint).map_err(|_| "endpoint is invalid".to_string())?;
    if !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some() {
        return Err("endpoint must not contain credentials or a query".to_string());
    }
    let host = url
        .host_str()
        .ok_or_else(|| "endpoint host is missing".to_string())?;
    validate_model_destination(&url)?;
    let local = matches!(host, "localhost" | "127.0.0.1" | "::1" | "[::1]")
        || host.ends_with(".localhost")
        || host.trim_matches(['[',']']).parse::<std::net::IpAddr>().is_ok_and(|ip| match ip {
            std::net::IpAddr::V4(ip) => ip.is_private() || ip.is_loopback(),
            std::net::IpAddr::V6(ip) => ip.is_loopback() || (ip.segments()[0] & 0xfe00) == 0xfc00,
        });
    if url.scheme() != "https" && !(url.scheme() == "http" && local) {
        return Err("cloud endpoint must use https".to_string());
    }
    Ok(())
}

/// Saved user targets may be private/Fake-IP, but never metadata or non-unicast.
pub(crate) fn validate_model_destination(url: &reqwest::Url) -> Result<(),String> {
    let host=url.host_str().ok_or("endpoint host is missing")?.trim_matches(['[',']']).trim_end_matches('.').to_ascii_lowercase();
    if ["metadata.google.internal","metadata.goog","instance-data"].contains(&host.as_str())
        || host.parse::<std::net::IpAddr>().is_ok_and(|ip| !allowed_model_ip(ip)) {
        return Err("MODEL_ENDPOINT_FORBIDDEN_TARGET".into());
    }
    Ok(())
}

pub(crate) fn allowed_model_ip(ip: std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(ip) => {
            let a=ip.octets();
            !ip.is_unspecified() && !ip.is_multicast() && !ip.is_broadcast() && a[0]!=0 && a[0]<224
                && !["169.254.169.254","169.254.170.2","100.100.100.200"].contains(&ip.to_string().as_str())
        }
        std::net::IpAddr::V6(ip) => ip.to_ipv4_mapped().map_or_else(||
            !ip.is_unspecified() && !ip.is_multicast() && ip != "fd00:ec2::254".parse::<std::net::Ipv6Addr>().unwrap(),
            |v4| allowed_model_ip(std::net::IpAddr::V4(v4))),
    }
}

pub fn billing_class_for(provider_id: &str, model_id: &str) -> &'static str {
    // A loopback connection can forward cloud models; model names do not prove hosting cost.
    if provider_id == "ollama" { return "unknown"; }
    if let Some(provider) = find(provider_id) {
        if let Some(preset) = provider.recommended_models.iter().find(|preset| preset.model_id == model_id) {
            return preset.billing_class;
        }
    }
    if provider_id == "openrouter"
        && (model_id == "openrouter/free" || model_id.ends_with(":free"))
    {
        "free_tier"
    } else {
        "unknown"
    }
}

pub fn free_source_url(provider_id: &str, model_id: &str) -> Option<&'static str> {
    match billing_class_for(provider_id, model_id) {
        "local_no_provider_fee" => Some("https://ollama.com/"),
        "free_tier" => Some("https://openrouter.ai/models?max_price=0"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn catalog_excludes_none_and_contains_local_ollama() {
        assert!(!all().iter().any(|provider| provider.provider_id == "none"));
        let ollama = find("ollama").unwrap();
        assert!(!ollama.requires_api_key);
        assert!(!ollama.free_hint); // Localhost alone does not certify execution location or price.
    }
    #[test]
    fn providers_expose_builtin_models_and_test_defaults() {
        for provider in all() {
            if provider.provider_id == "custom" {
                assert!(provider.recommended_models.is_empty());
                assert!(provider.default_test_model_id.is_none());
            } else if provider.provider_id == "ollama" {
                assert!(!provider.recommended_models.is_empty());
                assert!(provider.default_test_model_id.is_none());
            } else {
                assert!(!provider.recommended_models.is_empty(), "{}", provider.provider_id);
                assert!(provider.default_test_model_id.is_some());
            }
        }
        assert_eq!(find("deepseek").unwrap().models_path, Some("/models"));
        assert_eq!(find("qwen").unwrap().models_path, None);
        assert_eq!(find("glm").unwrap().recommended_models[0].model_id, "glm-5.2");
        for provider_id in ["kimi_cn", "kimi_global"] {
            assert!(find(provider_id).unwrap().recommended_models.iter().all(|model| model.model_id != "kimi-k3"));
        }
    }
    #[test]
    fn endpoint_policy_accepts_official_and_local_hosts() {
        assert!(validate_endpoint("openai", "https://api.openai.com/v1").is_ok());
        assert!(validate_endpoint("ollama", "http://localhost:11434").is_ok());
        assert!(validate_endpoint("ollama", "https://localhost:11434").is_ok());
        assert!(validate_endpoint("openai", "http://localhost:11434").is_ok());
        assert!(validate_endpoint("custom", "http://localhost:11434").is_ok());
        assert!(validate_endpoint("custom", "http://[::1]:11434").is_ok());
        assert!(validate_endpoint("ollama", "https://remote.example/v1").is_ok());
        assert!(validate_endpoint("openai", "http://api.openai.com/v1").is_err());
        assert!(validate_endpoint("openai", "https://user-chosen.example/v1").is_ok());
        assert!(validate_endpoint("custom", "https://example.com/v1").is_ok());
    }

    #[test]
    fn explicitly_configured_destinations_are_brand_independent_but_not_metadata_321() {
        assert!(validate_endpoint("openai", "https://enterprise.internal/v1").is_ok());
        assert!(validate_endpoint("custom", "https://198.18.0.1/v1").is_ok());
        assert!(validate_endpoint("custom", "https://169.254.169.254/latest/meta-data").is_err());
        assert!(validate_endpoint("custom", "https://0.0.0.0/v1").is_err());
        assert!(validate_endpoint("custom", "https://224.0.0.1/v1").is_err());
    }

    #[test]
    fn output_budget_is_capped_to_exact_model_limit_and_unknown_contracts_stay_unsupported() {
        for (provider, model, requested, expected_key, expected_tokens) in [
            ("openai", "gpt-5.6-sol", 200_000, "max_completion_tokens", 128_000),
            ("openai", "gpt-5.6-terra", 131_072, "max_completion_tokens", 128_000),
            ("deepseek", "deepseek-v4-flash", 500_000, "max_tokens", 393_216),
            ("qwen", "qwen3.5-plus", 131_072, "max_completion_tokens", 65_536),
            ("qwen", "qwen3.8-max", 200_000, "max_completion_tokens", 131_072),
            ("qwen", "qwen3.7-plus", 131_072, "max_completion_tokens", 131_072),
            ("qwen", "qwen3.7-flash", 131_072, "max_completion_tokens", 131_072),
        ] {
            let mut payload = serde_json::json!({
                "temperature": 0.2,
                "options": {"temperature": 0.7},
                "sentinel": "unchanged"
            });
            assert_eq!(
                apply_output_token_budget(provider, model, &mut payload, requested),
                OutputTokenBudgetSupport::Applied(AppliedOutputTokenBudget {
                    wire_field: expected_key,
                    applied_tokens: expected_tokens,
                    semantics: "total_generated_tokens_including_reasoning",
                }),
                "{provider}/{model}"
            );
            assert_eq!(payload.get(expected_key), Some(&serde_json::json!(expected_tokens)), "{provider}/{model}");
            assert!(payload.get("max_tokens").is_none() || expected_key == "max_tokens");
            assert!(payload.get("max_completion_tokens").is_none() || expected_key == "max_completion_tokens");
            assert_eq!(payload["sentinel"], "unchanged");
            assert_eq!(payload["temperature"], 0.2);
        }

        for (provider, model) in [
            ("gemini", "gemini-3.7-flash"),
            ("glm", "glm-5.2"),
            ("custom", "private-model"),
            ("qwen", "unclassified-qwen-model"),
            ("qwen", "qwen-plus"),
            ("kimi_cn", "kimi-k2.5"),
            ("kimi_global", "kimi-k2.5"),
            ("openrouter", "openrouter/free"),
            ("ollama", "qwen3:8b"),
        ] {
            let mut payload = serde_json::json!({"model": model});
            assert_eq!(
                apply_output_token_budget(provider, model, &mut payload, 321),
                OutputTokenBudgetSupport::Unsupported,
                "{provider}/{model}"
            );
            assert!(payload.get("max_tokens").is_none());
            assert!(payload.get("max_completion_tokens").is_none());
            assert!(payload.pointer("/options/num_predict").is_none());
        }
    }
}




