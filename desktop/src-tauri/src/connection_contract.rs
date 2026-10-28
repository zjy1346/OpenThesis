//! Connection-scoped wire declarations. A catalog entry is never a transport certification.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct CapabilityProfile {
    pub source: String,
    pub json_output: String,
    pub stream: String,
    pub include_usage: bool,
    pub usage: String,
    pub output_parameter: String,
    pub output_semantics: String,
    pub output_limit: Option<u32>,
    pub allow_unbounded_output: bool,
    pub upstream: String,
}

impl Default for CapabilityProfile {
    fn default() -> Self {
        Self { source: "legacy".into(), json_output: "prompt_json".into(), stream: "none".into(), include_usage: false,
            usage: "unknown".into(), output_parameter: "unknown".into(),
            output_semantics: "unknown".into(), output_limit: None,
            allow_unbounded_output: false, upstream: "unknown".into() }
    }
}

impl CapabilityProfile {
    pub fn compatible_default(protocol: &str) -> Self {
        Self { source:"compatible_default".into(), stream:if protocol == "ollama" {"ndjson"} else {"sse"}.into(),
            output_parameter:if protocol == "ollama" {"num_predict"} else {"max_tokens"}.into(),
            output_semantics:"visible_output_tokens_only".into(), ..Default::default() }
    }

    pub fn validate(&self, protocol: &str) -> Result<(), String> {
        if !["legacy", "compatible_default", "official", "user"].contains(&self.source.as_str())
            || !["chat_completions", "ollama"].contains(&protocol)
            || !["prompt_json", "json_object"].contains(&self.json_output.as_str())
            || !["none", "sse", "ndjson"].contains(&self.stream.as_str())
            || !["unknown", "openai", "deepseek", "ollama"].contains(&self.usage.as_str())
            || !["unknown", "max_tokens", "max_completion_tokens", "num_predict"].contains(&self.output_parameter.as_str())
            || !["unknown", "total_generated_tokens_including_reasoning", "visible_output_tokens_only"].contains(&self.output_semantics.as_str())
            || !["unknown", "direct", "proxy"].contains(&self.upstream.as_str())
            || self.output_limit.is_some_and(|n| n == 0 || n > 2_147_483_647)
            || (protocol == "ollama" && (self.stream == "sse" || !["unknown", "num_predict"].contains(&self.output_parameter.as_str())))
            || (protocol == "chat_completions" && (self.stream == "ndjson" || self.output_parameter == "num_predict"))
            || (self.include_usage && self.stream != "sse")
        { return Err("Unsupported connection protocol or capability declaration".into()); }
        Ok(())
    }

    pub fn apply_json(&self, payload: &mut Value, protocol: &str, requested: bool) {
        if requested && self.json_output == "json_object" {
            if protocol == "ollama" { payload["format"] = json!("json"); }
            else { payload["response_format"] = json!({"type":"json_object"}); }
        }
    }

    pub fn apply_budget(&self, payload: &mut Value, requested: u32) -> Result<Option<(u32, String, String)>, String> {
        if self.output_parameter == "unknown" {
            return if self.allow_unbounded_output { Ok(None) }
                else { Err("MODEL_OUTPUT_BUDGET_POLICY_REQUIRED".into()) };
        }
        let tokens = self.output_limit.map_or(requested, |limit| requested.min(limit));
        if tokens == 0 { return Err("MODEL_OUTPUT_BUDGET_POLICY_REQUIRED".into()); }
        if self.output_parameter == "num_predict" { payload["options"]["num_predict"] = json!(tokens); }
        else { payload[&self.output_parameter] = json!(tokens); }
        Ok(Some((tokens, self.output_parameter.clone(), self.output_semantics.clone())))
    }
}

/// Accept a base URL or an unambiguous complete Chat Completions URL. Preserve prefixes.
pub fn normalize_endpoint(endpoint: &str, protocol: &str) -> Result<String, String> {
    let mut url = reqwest::Url::parse(endpoint.trim()).map_err(|_| "endpoint is invalid")?;
    if !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some() {
        return Err("endpoint must not contain credentials, query, or fragment".into());
    }
    let path = url.path().trim_end_matches('/');
    let suffix = if protocol == "ollama" { "/api/chat" } else { "/chat/completions" };
    let mut base = path.strip_suffix(suffix).unwrap_or(path).to_string();
    if base.is_empty() && protocol != "ollama" { base = "/v1".into(); }
    url.set_path(&base);
    Ok(url.to_string().trim_end_matches('/').to_string())
}

/// User-saved connection transport only; external material cannot supply this.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct ConnectionTransportSettings {
    pub endpoint_mode: String,
    pub models_endpoint: Option<String>,
    pub network_mode: String,
    pub proxy_url: Option<String>,
}

impl Default for ConnectionTransportSettings {
    fn default() -> Self {
        Self {endpoint_mode:"base".into(),models_endpoint:None,network_mode:"system".into(),proxy_url:None}
    }
}

impl ConnectionTransportSettings {
    pub fn normalize_endpoint(&self, endpoint: &str, protocol: &str) -> Result<String,String> {
        if self.endpoint_mode == "base" { return normalize_endpoint(endpoint,protocol); }
        if self.endpoint_mode != "full_request" { return Err("MODEL_ENDPOINT_MODE_INVALID".into()); }
        let value = endpoint.trim().trim_end_matches('/');
        crate::provider_registry::validate_endpoint("custom",value)?;
        Ok(value.into())
    }

    pub fn validate(&self, endpoint: &str) -> Result<(),String> {
        if !["base","full_request"].contains(&self.endpoint_mode.as_str())
            || !["system","direct","proxy"].contains(&self.network_mode.as_str()) {
            return Err("MODEL_TRANSPORT_INVALID".into());
        }
        if self.network_mode == "proxy" {
            let proxy = reqwest::Url::parse(self.proxy_url.as_deref().unwrap_or_default()).map_err(|_| "MODEL_PROXY_INVALID")?;
            if !["http","https"].contains(&proxy.scheme()) || proxy.host_str().is_none()
                || !proxy.username().is_empty() || proxy.password().is_some() || proxy.query().is_some() || proxy.fragment().is_some()
                || !matches!(proxy.path(), ""|"/") { return Err("MODEL_PROXY_INVALID".into()); }
            crate::provider_registry::validate_model_destination(&proxy)?;
        }
        if let Some(path) = self.models_endpoint.as_deref().filter(|v| !v.is_empty()) {
            let base = reqwest::Url::parse(endpoint).map_err(|_| "MODEL_ENDPOINT_INVALID")?;
            let target = base.join(path).map_err(|_| "MODEL_ENDPOINT_INVALID")?;
            crate::provider_registry::validate_endpoint("custom", target.as_str())?;
            if target.origin() != base.origin() { return Err("MODEL_MODELS_ENDPOINT_ORIGIN".into()); }
        }
        Ok(())
    }

    pub fn url(&self, endpoint: &str, path: &str, discovery: bool) -> Result<reqwest::Url,String> {
        self.validate(endpoint)?;
        if discovery {
            if let Some(override_url) = self.models_endpoint.as_deref().filter(|v| !v.is_empty()) {
                return reqwest::Url::parse(endpoint).map_err(|_| String::from("MODEL_ENDPOINT_INVALID"))?.join(override_url).map_err(|_| "MODEL_ENDPOINT_INVALID".into());
            }
        } else if self.endpoint_mode == "full_request" {
            return reqwest::Url::parse(endpoint).map_err(|_| "MODEL_ENDPOINT_INVALID".into());
        }
        request_url(endpoint,path)
    }
}

pub fn request_url(base: &str, path: &str) -> Result<reqwest::Url, String> {
    let protocol = if path.trim_matches('/').starts_with("api/") { "ollama" } else { "chat_completions" };
    let base = normalize_endpoint(base, protocol)?;
    reqwest::Url::parse(&format!("{}/{}", base, path.trim_start_matches('/'))).map_err(|_| "endpoint is invalid".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_request_and_list_override_are_explicit_and_same_origin_321() {
        let transport=ConnectionTransportSettings {endpoint_mode:"full_request".into(),models_endpoint:Some("/tenant/models".into()),..Default::default()};
        let endpoint="https://gateway.example/tenant/custom/generate";
        assert_eq!(transport.normalize_endpoint(endpoint,"chat_completions").unwrap(),endpoint);
        assert_eq!(transport.url(endpoint,"chat/completions",false).unwrap().as_str(),endpoint);
        assert_eq!(transport.url(endpoint,"models",true).unwrap().as_str(),"https://gateway.example/tenant/models");
        assert!(ConnectionTransportSettings {models_endpoint:Some("https://other.example/models".into()),..transport.clone()}.validate(endpoint).is_err());
        assert!(ConnectionTransportSettings {network_mode:"proxy".into(),proxy_url:Some("http://user:secret@localhost:7890".into()),..transport}.validate(endpoint).is_err());
        let profile=CapabilityProfile::compatible_default("chat_completions");
        let mut payload=json!({}); profile.apply_budget(&mut payload,4000).unwrap();
        assert_eq!(profile.stream,"sse"); assert_eq!(payload["max_tokens"],4000);
        assert_eq!(profile.source,"compatible_default"); assert_eq!(profile.usage,"unknown");
        assert!(!profile.allow_unbounded_output);
    }
    #[test]
    fn bare_host_uses_compatible_v1_without_rewriting_existing_paths_321() {
        for input in ["https://maas-api.antdigital.com", "https://maas-api.antdigital.com/"] {
            assert_eq!(request_url(input, "chat/completions").unwrap().as_str(),
                "https://maas-api.antdigital.com/v1/chat/completions");
        }
        assert_eq!(request_url("https://maas-api.antdigital.com/v1/chat/completions", "chat/completions").unwrap().as_str(),
            "https://maas-api.antdigital.com/v1/chat/completions");
        assert_eq!(request_url("https://gateway.example/team", "chat/completions").unwrap().as_str(),
            "https://gateway.example/team/chat/completions");
    }
    #[test]
    fn base_or_full_url_keeps_case_and_proxy_prefix() {
        for (base, expected) in [
            ("https://host.example/proxy/v1/", "https://host.example/proxy/v1/chat/completions"),
            ("https://host.example/proxy/v1/chat/completions/", "https://host.example/proxy/v1/chat/completions"),
        ] { assert_eq!(request_url(base,"chat/completions").unwrap().as_str(), expected); }
        assert_eq!(request_url("https://host.example/prefix/v1/chat/completions", "/models").unwrap().as_str(), "https://host.example/prefix/v1/models");
        assert!(normalize_endpoint("https://user:secret@host.example/v1", "chat_completions").is_err());
        assert!(normalize_endpoint("https://host.example/v1#key", "chat_completions").is_err());
    }
    #[test]
    fn unknown_is_not_schema_stream_or_budget_certification() {
        let mut payload = json!({"model":"Private/Exact-ID","messages":[]});
        let profile = CapabilityProfile::default();
        profile.apply_json(&mut payload,"chat_completions",true);
        assert!(payload.get("response_format").is_none());
        assert!(profile.apply_budget(&mut payload,1024).is_err());
        let declared = CapabilityProfile { output_parameter:"max_tokens".into(), output_semantics:"visible_output_tokens_only".into(), output_limit:Some(500), ..profile };
        assert_eq!(declared.apply_budget(&mut payload,1024).unwrap().unwrap().0,500);
        assert_eq!(payload["model"],"Private/Exact-ID");
        assert_eq!(payload["max_tokens"],500);
        assert!(CapabilityProfile{ json_output:"strict_schema".into(), ..Default::default() }.validate("chat_completions").is_err());
    }
}
