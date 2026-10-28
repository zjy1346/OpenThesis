//! Non-executable models.dev metadata. Never mutates connections or performs inference.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

const BUNDLED: &str = include_str!("../resources/models-dev-20261022.json");
pub const SOURCE: &str = "https://models.dev/api.json";

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DirectoryModel {
    pub provider_id: String,
    pub model_id: String,
    pub name: String,
    pub context: Option<u64>,
    pub output: Option<u64>,
    pub input_price: Option<f64>,
    pub output_price: Option<f64>,
    pub cache_read_price: Option<f64>,
    pub image_input: Option<bool>,
    pub structured_output: Option<bool>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DirectorySnapshot {
    pub license: String,
    pub source: String,
    pub retrieved_at: String,
    pub models: Vec<DirectoryModel>,
}

/// Import only inert, bounded fields. Provider route metadata wins; never merge by model name.
pub fn sanitize(value: &Value, retrieved_at: String) -> Result<DirectorySnapshot, String> {
    let providers = value.as_object().ok_or("Invalid model directory")?;
    let mut models = Vec::new();
    for (provider_id, provider) in providers.iter().take(1000) {
        if provider_id.len() > 128 { continue; }
        let Some(entries) = provider.get("models").and_then(Value::as_object) else { continue; };
        for (id, model) in entries.iter().take(5000) {
            if crate::provider_registry::validate_model_id(id).is_err() { continue; }
            let number = |path: &str| model.pointer(path).and_then(Value::as_f64).filter(|n| n.is_finite() && *n >= 0.0);
            let limit = |path: &str| model.pointer(path).and_then(Value::as_u64).filter(|n| *n > 0 && *n <= 2_147_483_647);
            models.push(DirectoryModel {
                provider_id: provider_id.clone(), model_id:id.clone(),
                name:model.get("name").and_then(Value::as_str).unwrap_or(id).chars().filter(|c| !c.is_control()).take(256).collect(),
                context:limit("/limit/context"), output:limit("/limit/output"),
                input_price:number("/cost/input"), output_price:number("/cost/output"), cache_read_price:number("/cost/cache_read"),
                image_input:model.pointer("/modalities/input").and_then(Value::as_array).map(|items| items.iter().any(|item| item.as_str() == Some("image"))),
                structured_output:model.get("structured_output").and_then(Value::as_bool),
            });
            if models.len() >= 50_000 { break; }
        }
        if models.len() >= 50_000 { break; }
    }
    if models.is_empty() { return Err("Model directory contains no usable metadata; local snapshot retained".into()); }
    Ok(DirectorySnapshot { license:include_str!("../resources/models-dev-LICENSE.txt").into(), source:SOURCE.into(), retrieved_at, models })
}

pub fn bundled() -> Result<DirectorySnapshot, String> {
    sanitize(&serde_json::from_str(BUNDLED).map_err(|_| "Bundled model directory is invalid")?, "2026-10-22; sha256:F7250ADD5249892A3C33E743CDE047B2BA728119526F4BB59962DD4EE93AAC9E".into())
}

pub fn refresh() -> Result<DirectorySnapshot, String> {
    let client = reqwest::blocking::Client::builder().redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(25)).build().map_err(|_| "Directory client unavailable")?;
    let response = client.get(SOURCE).send().and_then(|r| r.error_for_status()).map_err(|_| "Directory refresh failed; local snapshot retained")?;
    let mut bytes = Vec::new();
    use std::io::Read;
    response.take(16 * 1024 * 1024 + 1).read_to_end(&mut bytes).map_err(|_| "Directory refresh failed; local snapshot retained")?;
    if bytes.len() > 16 * 1024 * 1024 { return Err("Directory response exceeds limit; local snapshot retained".into()); }
    let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_| "Clock unavailable")?.as_secs().to_string();
    sanitize(&serde_json::from_slice(&bytes).map_err(|_| "Directory returned invalid JSON; local snapshot retained")?, timestamp)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn catalog_is_inert_route_specific_declaration_not_certification_320() {
        let input = serde_json::json!({"proxy":{"npm":"DO NOT EXECUTE","models":{"Exact/ID":{"name":"Private","limit":{"context":123,"output":20},"cost":{"input":0.0},"structured_output":true}}},"official":{"models":{"Exact/ID":{"limit":{"context":456}}}}});
        let result = sanitize(&input,"test".into()).unwrap();
        let serialized = serde_json::to_string(&result).unwrap();
        assert!(!serialized.contains("npm")); assert!(!serialized.contains("EXECUTE"));
        assert_eq!(result.models.len(),2);
        assert_eq!(result.models.iter().find(|m| m.provider_id=="proxy").unwrap().context,Some(123));
        assert!(sanitize(&serde_json::json!({}),"test".into()).is_err());
        assert!(!bundled().unwrap().models.is_empty());
    }
}
