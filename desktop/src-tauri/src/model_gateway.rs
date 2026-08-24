use crate::credentials::platform_vault;
use crate::model_center::{
    resolve_model_center_db_path, ConfiguredModelRuntime, ConnectionRuntime, ConnectionSummary,
    ModelCenter, ModelCenterState,
};
use crate::provider_registry;
use base64::Engine as _;
use reqwest::blocking::{Client, Response};
use reqwest::redirect::Policy;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, State};
use zeroize::{Zeroize, Zeroizing};

const MAX_GATEWAY_REQUEST_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_RESPONSE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_ERROR_CHARS: usize = 800;

#[derive(Clone)]
pub(crate) struct ModelGateway {
    center: Arc<ModelCenter>,
}

impl ModelGateway {
    pub(crate) fn new(center: Arc<ModelCenter>) -> Self {
        Self { center }
    }

    pub(crate) fn discover_models(
        &self,
        connection_id: &str,
    ) -> Result<Vec<DiscoveredModel>, GatewayError> {
        self.discover_models_with_secret(connection_id, None)
    }

    fn discover_models_with_secret(
        &self,
        connection_id: &str,
        secret_override: Option<&str>,
    ) -> Result<Vec<DiscoveredModel>, GatewayError> {
        let connection = self
            .center
            .connection_runtime(connection_id)
            .map_err(GatewayError::configuration)?;
        if !connection.enabled {
            return Err(GatewayError::new(
                "MODEL_CONNECTION_DISABLED",
                "The model connection is disabled.",
                false,
            ));
        }
        let provider = provider_registry::find(&connection.provider_id)
            .ok_or_else(|| GatewayError::configuration("Unknown provider."))?;
        if !provider.supports_discovery || provider.models_path.is_none() {
            return Err(GatewayError::new(
                "MODEL_DISCOVERY_UNSUPPORTED",
                "This provider does not expose model discovery; enter the model ID manually.",
                false,
            ));
        }
        let stored_secret = if secret_override.is_none() {
            self.center
                .credential_for_connection(&connection)
                .map_err(GatewayError::configuration)?
        } else {
            None
        };
        let secret_ref =
            secret_override.or_else(|| stored_secret.as_ref().map(|value| value.as_str()));
        let client = build_client(&connection, 30)?;
        let url = endpoint_url(&connection.endpoint, provider.models_path.unwrap_or("/models"))?;
        let request = authorize(client.get(url), secret_ref)?;
        let response = request
            .send()
            .map_err(|_| GatewayError::network("Model discovery could not reach the provider."))?;
        let payload = read_json_response(response, secret_ref)?;
        let ids = if connection.provider_id == "ollama" {
            payload
                .get("models")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|item| {
                    item.get("name")
                        .or_else(|| item.get("model"))
                        .and_then(Value::as_str)
                })
                .map(str::to_string)
                .collect::<Vec<_>>()
        } else {
            payload
                .get("data")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|item| item.get("id").and_then(Value::as_str))
                .map(str::to_string)
                .collect::<Vec<_>>()
        };
        let mut ids = ids;
        ids.sort();
        ids.dedup();
        ids.truncate(ids.len().min(1000));
        Ok(ids
            .into_iter()
            .filter(|model_id| provider_registry::validate_model_id(model_id).is_ok())
            .map(|model_id| DiscoveredModel {
                alias: model_id.clone(),
                billing_class: provider_registry::billing_class_for(
                    &connection.provider_id,
                    &model_id,
                )
                .to_string(),
                model_id,
                capabilities: vec!["text_chat".to_string(), "structured_json".to_string()],
            })
            .collect())
    }

    pub(crate) fn generate(
        &self,
        configured_model_id: &str,
        system_prompt: &str,
        user_prompt: &str,
        json_mode: bool,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        self.generate_with_secret(
            configured_model_id,
            system_prompt,
            user_prompt,
            json_mode,
            None,
        )
    }

    fn generate_with_secret(
        &self,
        configured_model_id: &str,
        system_prompt: &str,
        user_prompt: &str,
        json_mode: bool,
        secret_override: Option<&str>,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        if system_prompt.len() + user_prompt.len() > 7 * 1024 * 1024 {
            return Err(GatewayError::new(
                "MODEL_PROMPT_TOO_LARGE",
                "The model prompt exceeds the gateway limit.",
                false,
            ));
        }
        let model = self
            .center
            .configured_model_runtime(configured_model_id)
            .map_err(GatewayError::configuration)?;
        if !model.connection_enabled || !model.model_enabled {
            return Err(GatewayError::new(
                "MODEL_DISABLED",
                "The configured model or its connection is disabled.",
                false,
            ));
        }
        if !model.capabilities.is_empty()
            && !model
                .capabilities
                .iter()
                .any(|capability| capability == "text_chat")
        {
            return Err(GatewayError::new(
                "MODEL_CAPABILITY_MISMATCH",
                "The configured model is not enabled for text chat.",
                false,
            ));
        }
        let connection = self
            .center
            .connection_runtime(&model.connection_id)
            .map_err(GatewayError::configuration)?;
        let stored_secret = if secret_override.is_none() {
            self.center
                .credential_for_connection(&connection)
                .map_err(GatewayError::configuration)?
        } else {
            None
        };
        let secret_ref =
            secret_override.or_else(|| stored_secret.as_ref().map(|value| value.as_str()));
        let timeout = u64::try_from(model.timeout_seconds)
            .unwrap_or(180)
            .clamp(5, 600);
        let client = build_client(&connection, timeout)?;
        if model.provider_id == "ollama" {
            self.generate_ollama(
                &client,
                &model,
                secret_ref,
                system_prompt,
                user_prompt,
                json_mode,
            )
        } else {
            self.generate_openai_compatible(
                &client,
                &model,
                secret_ref,
                system_prompt,
                user_prompt,
                json_mode,
            )
        }
    }

    fn generate_vision(
        &self,
        configured_model_id: &str,
        system_prompt: &str,
        user_prompt: &str,
        image_media_type: &str,
        image_base64: &str,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        if system_prompt.len() + user_prompt.len() > 512 * 1024
            || image_base64.len() > 11 * 1024 * 1024
            || image_media_type != "image/png"
        {
            return Err(GatewayError::new(
                "MODEL_VISION_PAYLOAD_INVALID",
                "The vision request exceeds the gateway limits or uses an unsupported image type.",
                false,
            ));
        }
        let mut decoded = base64::engine::general_purpose::STANDARD
            .decode(image_base64)
            .map_err(|_| {
                GatewayError::new(
                    "MODEL_VISION_PAYLOAD_INVALID",
                    "The vision image is not valid base64.",
                    false,
                )
            })?;
        let image_valid = decoded.len() <= 8 * 1024 * 1024
            && decoded.starts_with(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        decoded.zeroize();
        if !image_valid {
            return Err(GatewayError::new(
                "MODEL_VISION_PAYLOAD_INVALID",
                "The vision image is not a bounded PNG.",
                false,
            ));
        }
        let model = self
            .center
            .configured_model_runtime(configured_model_id)
            .map_err(GatewayError::configuration)?;
        if !model.connection_enabled || !model.model_enabled {
            return Err(GatewayError::new(
                "MODEL_DISABLED",
                "The configured model or its connection is disabled.",
                false,
            ));
        }
        if !model
            .capabilities
            .iter()
            .any(|capability| capability == "vision")
        {
            return Err(GatewayError::new(
                "MODEL_CAPABILITY_MISMATCH",
                "The configured model is not enabled for vision.",
                false,
            ));
        }
        let connection = self
            .center
            .connection_runtime(&model.connection_id)
            .map_err(GatewayError::configuration)?;
        let secret = self
            .center
            .credential_for_connection(&connection)
            .map_err(GatewayError::configuration)?;
        let secret_ref = secret.as_ref().map(|value| value.as_str());
        let timeout = u64::try_from(model.timeout_seconds)
            .unwrap_or(180)
            .clamp(5, 600);
        let client = build_client(&connection, timeout)?;
        let response = if model.provider_id == "ollama" {
            let url = endpoint_url(&model.endpoint, "api/chat")?;
            let payload = json!({
                "model": model.model_id,
                "stream": false,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt, "images": [image_base64]}
                ]
            });
            post_json(&client, &url, &payload, secret_ref)?
        } else {
            let url = endpoint_url(&model.endpoint, "chat/completions")?;
            let mut payload = json!({
                "model": model.model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {
                            "url": format!("data:{image_media_type};base64,{image_base64}")
                        }}
                    ]}
                ],
                "response_format": {"type": "json_object"}
            });
            if let Some(temperature) = model.temperature {
                payload["temperature"] = json!(temperature);
            }
            match post_json(&client, &url, &payload, secret_ref) {
                Err(error) if response_format_is_unsupported(&error) => {
                    payload
                        .as_object_mut()
                        .expect("gateway payload is an object")
                        .remove("response_format");
                    post_json(&client, &url, &payload, secret_ref)?
                }
                result => result?,
            }
        };
        let (content, finish_reason) = if model.provider_id == "ollama" {
            (
                response
                    .pointer("/message/content")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        GatewayError::new(
                            "MODEL_RESPONSE_INVALID",
                            "The vision response did not contain message.content.",
                            false,
                        )
                    })?
                    .to_string(),
                response
                    .get("done_reason")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            )
        } else {
            (
                response
                    .pointer("/choices/0/message/content")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        GatewayError::new(
                            "MODEL_RESPONSE_INVALID",
                            "The vision response did not contain message.content.",
                            false,
                        )
                    })?
                    .to_string(),
                response
                    .pointer("/choices/0/finish_reason")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            )
        };
        Ok(self.result(&model, content, finish_reason))
    }

    fn generate_openai_compatible(
        &self,
        client: &Client,
        model: &ConfiguredModelRuntime,
        secret: Option<&str>,
        system_prompt: &str,
        user_prompt: &str,
        json_mode: bool,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        let url = endpoint_url(&model.endpoint, "chat/completions")?;
        let mut payload = json!({
            "model": model.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        });
        if let Some(temperature) = model.temperature {
            payload["temperature"] = json!(temperature);
        }
        if json_mode {
            payload["response_format"] = json!({"type": "json_object"});
        }
        let response = match post_json(client, &url, &payload, secret) {
            Err(error) if json_mode && response_format_is_unsupported(&error) => {
                payload
                    .as_object_mut()
                    .expect("gateway payload is an object")
                    .remove("response_format");
                post_json(client, &url, &payload, secret)?
            }
            result => result?,
        };
        let content = response
            .pointer("/choices/0/message/content")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                GatewayError::new(
                    "MODEL_RESPONSE_INVALID",
                    "The provider response did not contain message.content.",
                    false,
                )
            })?
            .to_string();
        let finish_reason = response
            .pointer("/choices/0/finish_reason")
            .and_then(Value::as_str)
            .map(str::to_string);
        Ok(self.result(model, content, finish_reason))
    }

    fn generate_ollama(
        &self,
        client: &Client,
        model: &ConfiguredModelRuntime,
        secret: Option<&str>,
        system_prompt: &str,
        user_prompt: &str,
        json_mode: bool,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        let url = endpoint_url(&model.endpoint, "api/chat")?;
        let mut payload = json!({
            "model": model.model_id,
            "stream": false,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        });
        if json_mode {
            payload["format"] = json!("json");
        }
        if let Some(temperature) = model.temperature {
            payload["options"] = json!({"temperature": temperature});
        }
        let response = post_json(client, &url, &payload, secret)?;
        let content = response
            .pointer("/message/content")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                GatewayError::new(
                    "MODEL_RESPONSE_INVALID",
                    "The Ollama response did not contain message.content.",
                    false,
                )
            })?
            .to_string();
        let finish_reason = response
            .get("done_reason")
            .and_then(Value::as_str)
            .map(str::to_string);
        Ok(self.result(model, content, finish_reason))
    }

    fn result(
        &self,
        model: &ConfiguredModelRuntime,
        content: String,
        finish_reason: Option<String>,
    ) -> GatewayGenerateResult {
        let endpoint_host = reqwest::Url::parse(&model.endpoint)
            .ok()
            .and_then(|url| url.host_str().map(str::to_string))
            .unwrap_or_default();
        GatewayGenerateResult {
            meta: GatewayResponseMeta {
                configured_model_id: model.configured_model_id.clone(),
                connection_id: model.connection_id.clone(),
                provider_id: model.provider_id.clone(),
                model_id: model.model_id.clone(),
                alias: model.alias.clone(),
                endpoint_host,
                finish_reason,
                content_length: content.len(),
                credential_version: model.credential_version,
                connection_configuration_version: model.connection_configuration_version,
                model_configuration_version: model.configuration_version,
                billing_class: model.billing_class.clone(),
                free_source_url: model.free_source_url.clone(),
                free_verified_at: model.free_verified_at,
                temperature: model.temperature,
                timeout_seconds: model.timeout_seconds,
            },
            content,
        }
    }

    pub(crate) fn rotate_connection_secret(
        &self,
        connection_id: &str,
        secret: &str,
    ) -> Result<ConnectionSummary, GatewayError> {
        let rotation = self
            .center
            .stage_secret(connection_id, secret)
            .map_err(GatewayError::configuration)?;
        let staged_secret = match self.center.staged_secret(&rotation) {
            Ok(secret) => secret,
            Err(error) => {
                let _ = self.center.discard_staged_secret(&rotation);
                return Err(GatewayError::configuration(error));
            }
        };
        let connection = self
            .center
            .connection_runtime(connection_id)
            .map_err(GatewayError::configuration)?;
        let provider = provider_registry::find(&connection.provider_id)
            .ok_or_else(|| GatewayError::configuration("Unknown provider."))?;
        let tested = if provider.supports_discovery {
            self.discover_models_with_secret(connection_id, Some(staged_secret.as_str()))
                .map(|_| ())
        } else {
            let configured_model_id = self
                .center
                .first_enabled_model_id(connection_id)
                .map_err(GatewayError::configuration)?
                .ok_or_else(|| {
                    GatewayError::new(
                        "MODEL_ROTATION_TEST_UNAVAILABLE",
                        "Add and test a model before replacing this connection key.",
                        false,
                    )
                })?;
            self.generate_with_secret(
                &configured_model_id,
                "You are a connection test. Return JSON only.",
                r#"{"task":"Return {\"ok\":true}."}"#,
                true,
                Some(staged_secret.as_str()),
            )
            .map(|_| ())
        };
        if let Err(error) = tested {
            let _ = self.center.discard_staged_secret(&rotation);
            return Err(error);
        }
        drop(staged_secret);
        let summary = self
            .center
            .activate_staged_secret(&rotation)
            .map_err(GatewayError::configuration)?;
        let _ = self
            .center
            .mark_connection_status(connection_id, "ready", None);
        Ok(summary)
    }
    pub(crate) fn test_connection(
        &self,
        connection_id: &str,
        requested_model_id: Option<&str>,
    ) -> Result<GatewayTestResult, GatewayError> {
        let connection = self
            .center
            .connection_runtime(connection_id)
            .map_err(GatewayError::configuration)?;
        if !connection.enabled {
            let error = GatewayError::new(
                "MODEL_CONNECTION_DISABLED",
                "The model connection is disabled.",
                false,
            );
            let _ = self
                .center
                .mark_connection_status(connection_id, "disabled", Some(&error.code));
            return Err(error);
        }
        let provider = provider_registry::find(&connection.provider_id)
            .ok_or_else(|| GatewayError::configuration("Unknown provider."))?;
        let requested_model_id = requested_model_id.map(str::trim).filter(|value| !value.is_empty());
        if let Some(model_id) = requested_model_id {
            provider_registry::validate_model_id(model_id)
                .map_err(GatewayError::configuration)?;
        }
        let discovered_ollama_model = if requested_model_id.is_none() && connection.provider_id == "ollama" {
            let models = self.discover_models_with_secret(connection_id, None)?;
            Some(models.into_iter().next().ok_or_else(|| GatewayError::new(
                "MODEL_NO_INSTALLED_MODEL",
                "No Ollama models are installed. Install a model in your existing Ollama service, then retry.",
                true,
            ))?.model_id)
        } else {
            None
        };
        let configured_model = self
            .center
            .first_enabled_model_id(connection_id)
            .map_err(GatewayError::configuration)?
            .map(|configured_model_id| {
                self.center
                    .configured_model_runtime(&configured_model_id)
                    .map(|model| model.model_id)
                    .map_err(GatewayError::configuration)
            })
            .transpose()?;
        let model_id = requested_model_id
            .map(str::to_string)
            .or(discovered_ollama_model)
            .or_else(|| provider.default_test_model_id.map(str::to_string))
            .or(configured_model)
            .ok_or_else(|| GatewayError::new(
                "MODEL_TEST_MODEL_REQUIRED",
                "Select or add a model before testing this custom connection.",
                false,
            ))?;
        provider_registry::validate_model_id(&model_id)
            .map_err(GatewayError::configuration)?;
        let secret = self
            .center
            .credential_for_connection(&connection)
            .map_err(GatewayError::configuration)?;
        let secret_ref = secret.as_ref().map(|value| value.as_str());
        let model = ConfiguredModelRuntime {
            configured_model_id: format!("connection-test.{connection_id}"),
            connection_id: connection_id.to_string(),
            provider_id: connection.provider_id.clone(),
            endpoint: connection.endpoint.clone(),
            credential_version: 0,
            connection_enabled: true,
            connection_configuration_version: 0,
            model_id: model_id.clone(),
            alias: format!("{} connection test", provider.display_name),
            model_enabled: true,
            capabilities: vec!["text_chat".to_string(), "structured_json".to_string()],
            billing_class: provider_registry::billing_class_for(&connection.provider_id, &model_id).to_string(),
            free_source_url: provider_registry::free_source_url(&connection.provider_id, &model_id).map(str::to_string),
            free_verified_at: None,
            temperature: None,
            timeout_seconds: 30,
            configuration_version: 0,
        };
        let client = build_client(&connection, 30)?;
        let result = if connection.provider_id == "ollama" {
            self.generate_ollama(
                &client,
                &model,
                secret_ref,
                "You are a connection test. Return JSON only.",
                r#"{"task":"Return {\"ok\":true}."}"#,
                true,
            )
        } else {
            self.generate_openai_compatible(
                &client,
                &model,
                secret_ref,
                "You are a connection test. Return JSON only.",
                r#"{"task":"Return {\"ok\":true}."}"#,
                true,
            )
        };
        match result {
            Ok(result) => {
                let _ = self
                    .center
                    .mark_connection_status(connection_id, "ready", None);
                Ok(GatewayTestResult {
                    ok: true,
                    message: format!("Connection test succeeded with {}.", model_id),
                    meta: result.meta,
                })
            }
            Err(error) => {
                let _ = self.center.mark_connection_status(
                    connection_id,
                    health_status_for_error(&error.code),
                    Some(&error.code),
                );
                Err(error)
            }
        }
    }
    pub(crate) fn test_model(
        &self,
        configured_model_id: &str,
    ) -> Result<GatewayTestResult, GatewayError> {
        let result: Result<GatewayGenerateResult, GatewayError> = (|| {
            let text_result = self.generate(
                configured_model_id,
                "You are a connection test. Return JSON only.",
                r#"{"task":"Return {\"ok\":true}."}"#,
                true,
            )?;
            let model = self
                .center
                .configured_model_runtime(configured_model_id)
                .map_err(GatewayError::configuration)?;
            if model
                .capabilities
                .iter()
                .any(|capability| capability == "vision")
            {
                const TEST_PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
                self.generate_vision(
                    configured_model_id,
                    "You are a vision connection test. Return JSON only.",
                    r#"{"task":"Confirm that an image was received and return {\"ok\":true}."}"#,
                    "image/png",
                    TEST_PNG,
                )?;
            }
            Ok(text_result)
        })();
        match result {
            Ok(result) => {
                let _ =
                    self.center
                        .mark_connection_status(&result.meta.connection_id, "ready", None);
                let _ = self.center.mark_model_health(configured_model_id, "ready");
                Ok(GatewayTestResult {
                    ok: true,
                    message: "Connection and declared capabilities succeeded.".to_string(),
                    meta: result.meta,
                })
            }
            Err(error) => {
                if let Ok(model) = self.center.configured_model_runtime(configured_model_id) {
                    let status = health_status_for_error(&error.code);
                    let _ = self.center.mark_connection_status(
                        &model.connection_id,
                        status,
                        Some(&error.code),
                    );
                    let _ = self.center.mark_model_health(configured_model_id, status);
                }
                Err(error)
            }
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct DiscoveredModel {
    pub model_id: String,
    pub alias: String,
    pub billing_class: String,
    pub capabilities: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayResponseMeta {
    pub configured_model_id: String,
    pub connection_id: String,
    pub provider_id: String,
    pub model_id: String,
    pub alias: String,
    pub endpoint_host: String,
    pub finish_reason: Option<String>,
    pub content_length: usize,
    pub credential_version: i64,
    pub connection_configuration_version: i64,
    pub model_configuration_version: i64,
    pub billing_class: String,
    pub free_source_url: Option<String>,
    pub free_verified_at: Option<i64>,
    pub temperature: Option<f64>,
    pub timeout_seconds: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayGenerateResult {
    pub content: String,
    pub meta: GatewayResponseMeta,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayTestResult {
    pub ok: bool,
    pub message: String,
    pub meta: GatewayResponseMeta,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

impl GatewayError {
    fn new(code: &str, message: impl Into<String>, retryable: bool) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
            retryable,
        }
    }

    fn configuration(message: impl Into<String>) -> Self {
        let message = message.into();
        let code = if message == "MODEL_CREDENTIAL_MISSING"
            || message == "stored credential was not found"
        {
            "MODEL_CREDENTIAL_MISSING"
        } else {
            "MODEL_CONFIGURATION_ERROR"
        };
        let safe_message = if code == "MODEL_CREDENTIAL_MISSING" {
            "The saved credential is unavailable. Reconfigure this connection in Model Center."
                .to_string()
        } else {
            message
        };
        Self::new(code, safe_message, false)
    }

    fn network(message: impl Into<String>) -> Self {
        Self::new("MODEL_NETWORK_ERROR", message, true)
    }
}

fn health_status_for_error(code: &str) -> &'static str {
    match code {
        "MODEL_UNAUTHORIZED" | "MODEL_CREDENTIAL_MISSING" => "unauthorized",
        "MODEL_RATE_LIMITED" => "rate_limited",
        "MODEL_CONNECTION_DISABLED" | "MODEL_DISABLED" => "disabled",
        _ => "unavailable",
    }
}

fn endpoint_url(base: &str, path: &str) -> Result<reqwest::Url, GatewayError> {
    let mut normalized = base.trim_end_matches('/').to_string();
    normalized.push('/');
    normalized.push_str(path.trim_start_matches('/'));
    reqwest::Url::parse(&normalized).map_err(|_| {
        GatewayError::new(
            "MODEL_ENDPOINT_INVALID",
            "The configured endpoint is invalid.",
            false,
        )
    })
}

fn build_client(
    connection: &ConnectionRuntime,
    timeout_seconds: u64,
) -> Result<Client, GatewayError> {
    provider_registry::validate_endpoint(&connection.provider_id, &connection.endpoint)
        .map_err(GatewayError::configuration)?;
    let url = reqwest::Url::parse(&connection.endpoint).map_err(|_| {
        GatewayError::new(
            "MODEL_ENDPOINT_INVALID",
            "The configured endpoint is invalid.",
            false,
        )
    })?;
    let mut builder = Client::builder()
        .redirect(Policy::none())
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(timeout_seconds.clamp(5, 600)));

    if connection.provider_id == "custom" && !is_loopback_host(url.host_str().unwrap_or_default()) {
        let host = url.host_str().ok_or_else(|| {
            GatewayError::new(
                "MODEL_ENDPOINT_INVALID",
                "The configured endpoint has no host.",
                false,
            )
        })?;
        let port = url.port_or_known_default().ok_or_else(|| {
            GatewayError::new(
                "MODEL_ENDPOINT_INVALID",
                "The configured endpoint has no usable port.",
                false,
            )
        })?;
        let addresses = (host, port)
            .to_socket_addrs()
            .map_err(|_| GatewayError::network("The custom endpoint could not be resolved."))?
            .collect::<Vec<_>>();
        if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(address.ip())) {
            return Err(GatewayError::new(
                "MODEL_ENDPOINT_PRIVATE",
                "A remote custom endpoint must not resolve to a private or reserved address.",
                false,
            ));
        }
        builder = builder.resolve_to_addrs(host, &addresses);
    }

    builder.build().map_err(|_| {
        GatewayError::new(
            "MODEL_CLIENT_ERROR",
            "The secure model client could not be created.",
            false,
        )
    })
}

fn is_loopback_host(host: &str) -> bool {
    matches!(host, "localhost" | "127.0.0.1" | "::1")
}

fn is_public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(value) => is_public_ipv4(value),
        IpAddr::V6(value) => is_public_ipv6(value),
    }
}

fn is_public_ipv4(ip: Ipv4Addr) -> bool {
    let octets = ip.octets();
    !(ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_unspecified()
        || ip.is_broadcast()
        || ip.is_multicast()
        || octets[0] == 0
        || octets[0] >= 224
        || (octets[0] == 100 && (64..=127).contains(&octets[1]))
        || (octets[0] == 192 && octets[1] == 0 && octets[2] == 2)
        || (octets[0] == 198 && (octets[1] == 18 || octets[1] == 19))
        || (octets[0] == 198 && octets[1] == 51 && octets[2] == 100)
        || (octets[0] == 203 && octets[1] == 0 && octets[2] == 113))
}

fn is_public_ipv6(ip: Ipv6Addr) -> bool {
    let segments = ip.segments();
    if let Some(mapped) = ip.to_ipv4_mapped() {
        return is_public_ipv4(mapped);
    }
    !(ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || (segments[0] & 0xfe00) == 0xfc00
        || (segments[0] & 0xffc0) == 0xfe80
        || segments[0] == 0x2001 && segments[1] == 0x0db8)
}

fn authorize(
    request: reqwest::blocking::RequestBuilder,
    secret: Option<&str>,
) -> Result<reqwest::blocking::RequestBuilder, GatewayError> {
    if let Some(secret) = secret {
        let mut bearer = Zeroizing::new(String::with_capacity(secret.len() + 7));
        bearer.push_str("Bearer ");
        bearer.push_str(secret);
        let value = reqwest::header::HeaderValue::from_bytes(bearer.as_bytes()).map_err(|_| {
            GatewayError::new(
                "MODEL_CREDENTIAL_INVALID",
                "The saved credential is invalid.",
                false,
            )
        })?;
        Ok(request.header(reqwest::header::AUTHORIZATION, value))
    } else {
        Ok(request)
    }
}

fn post_json(
    client: &Client,
    url: &reqwest::Url,
    payload: &Value,
    secret: Option<&str>,
) -> Result<Value, GatewayError> {
    let request = authorize(client.post(url.clone()).json(payload), secret)?;
    let response = request
        .send()
        .map_err(|_| GatewayError::network("The model provider could not be reached."))?;
    read_json_response(response, secret)
}

fn read_json_response(response: Response, secret: Option<&str>) -> Result<Value, GatewayError> {
    let status = response.status();
    if status.is_redirection() {
        return Err(GatewayError::new(
            "MODEL_REDIRECT_BLOCKED",
            "The provider attempted an HTTP redirect, which the credential policy blocks.",
            false,
        ));
    }
    if response
        .content_length()
        .is_some_and(|length| length > MAX_PROVIDER_RESPONSE_BYTES)
    {
        return Err(GatewayError::new(
            "MODEL_RESPONSE_TOO_LARGE",
            "The provider response exceeds the gateway limit.",
            false,
        ));
    }
    let mut body = Vec::new();
    response
        .take(MAX_PROVIDER_RESPONSE_BYTES + 1)
        .read_to_end(&mut body)
        .map_err(|_| GatewayError::network("The provider response could not be read."))?;
    if body.len() as u64 > MAX_PROVIDER_RESPONSE_BYTES {
        body.zeroize();
        return Err(GatewayError::new(
            "MODEL_RESPONSE_TOO_LARGE",
            "The provider response exceeds the gateway limit.",
            false,
        ));
    }
    if !status.is_success() {
        let mut detail = String::from_utf8_lossy(&body).into_owned();
        body.zeroize();
        if let Some(secret) = secret {
            if !secret.is_empty() {
                detail = detail.replace(secret, "[REDACTED]");
            }
        }
        detail = detail
            .chars()
            .take(MAX_PROVIDER_ERROR_CHARS)
            .collect::<String>();
        let code = match status.as_u16() {
            401 | 403 => "MODEL_UNAUTHORIZED",
            408 => "MODEL_TIMEOUT",
            429 => "MODEL_RATE_LIMITED",
            value if value >= 500 => "MODEL_PROVIDER_UNAVAILABLE",
            _ => "MODEL_HTTP_ERROR",
        };
        let retryable =
            matches!(status.as_u16(), 408 | 409 | 425 | 429) || status.is_server_error();
        let message = if detail.trim().is_empty() {
            format!("The provider returned HTTP {}.", status.as_u16())
        } else {
            format!("The provider returned HTTP {}: {}", status.as_u16(), detail)
        };
        return Err(GatewayError::new(code, message, retryable));
    }
    let result = serde_json::from_slice::<Value>(&body).map_err(|_| {
        GatewayError::new(
            "MODEL_RESPONSE_INVALID",
            "The provider returned invalid JSON.",
            false,
        )
    });
    body.zeroize();
    result
}

fn response_format_is_unsupported(error: &GatewayError) -> bool {
    if error.code != "MODEL_HTTP_ERROR" {
        return false;
    }
    let detail = error.message.to_ascii_lowercase();
    detail.contains("response_format")
        && ["unsupported", "not support", "unknown", "invalid"]
            .iter()
            .any(|marker| detail.contains(marker))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
struct GatewayCliRequest {
    operation: String,
    configured_model_id: String,
    system_prompt: String,
    user_prompt: String,
    #[serde(default = "default_json_mode")]
    json_mode: bool,
    #[serde(default)]
    image_media_type: String,
    #[serde(default)]
    image_base64: String,
}

fn default_json_mode() -> bool {
    true
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
struct GatewayCliResponse {
    ok: bool,
    result: Option<GatewayGenerateResult>,
    error: Option<GatewayError>,
}

pub fn run_model_gateway_stdio() -> i32 {
    let response = match read_cli_request().and_then(run_cli_request) {
        Ok(result) => GatewayCliResponse {
            ok: true,
            result: Some(result),
            error: None,
        },
        Err(error) => GatewayCliResponse {
            ok: false,
            result: None,
            error: Some(error),
        },
    };
    let mut stdout = std::io::stdout().lock();
    if serde_json::to_writer(&mut stdout, &response).is_err()
        || stdout.write_all(b"\n").is_err()
        || stdout.flush().is_err()
    {
        return 3;
    }
    0
}

fn read_cli_request() -> Result<GatewayCliRequest, GatewayError> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .lock()
        .take(MAX_GATEWAY_REQUEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            GatewayError::new(
                "MODEL_GATEWAY_PROTOCOL_ERROR",
                "The gateway request could not be read.",
                false,
            )
        })?;
    if bytes.len() as u64 > MAX_GATEWAY_REQUEST_BYTES {
        bytes.zeroize();
        return Err(GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The gateway request is too large.",
            false,
        ));
    }
    let parsed = serde_json::from_slice(&bytes).map_err(|_| {
        GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The gateway request is invalid.",
            false,
        )
    });
    bytes.zeroize();
    parsed
}

fn run_cli_request(request: GatewayCliRequest) -> Result<GatewayGenerateResult, GatewayError> {
    let data_dir = std::env::var("OPENTHESIS_DATA_DIR").map_err(|_| {
        GatewayError::new(
            "MODEL_GATEWAY_CONFIGURATION_ERROR",
            "The model gateway data directory is unavailable.",
            false,
        )
    })?;
    let db_path = resolve_model_center_db_path(Some(&data_dir), Path::new(""))
        .map_err(GatewayError::configuration)?;
    let center =
        Arc::new(ModelCenter::new(db_path, platform_vault()).map_err(GatewayError::configuration)?);
    let gateway = ModelGateway::new(center);
    match request.operation.as_str() {
        "generate" => gateway.generate(
            &request.configured_model_id,
            &request.system_prompt,
            &request.user_prompt,
            request.json_mode,
        ),
        "vision" => gateway.generate_vision(
            &request.configured_model_id,
            &request.system_prompt,
            &request.user_prompt,
            &request.image_media_type,
            &request.image_base64,
        ),
        _ => Err(GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The gateway operation is not supported.",
            false,
        )),
    }
}
#[tauri::command]
pub fn model_gateway_discover_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
) -> Result<Vec<DiscoveredModel>, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    let gateway = ModelGateway::new(center.clone());
    match gateway.discover_models(&connection_id) {
        Ok(models) => {
            let _ = center.mark_connection_status(&connection_id, "ready", None);
            Ok(models)
        }
        Err(error) => {
            let _ = center.mark_connection_status(
                &connection_id,
                health_status_for_error(&error.code),
                Some(&error.code),
            );
            Err(error)
        }
    }
}

#[tauri::command]
pub fn model_gateway_rotate_connection_secret(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    secret: String,
) -> Result<ConnectionSummary, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    let secret = Zeroizing::new(secret);
    ModelGateway::new(center).rotate_connection_secret(&connection_id, secret.as_str())
}
#[tauri::command]
pub fn model_gateway_test_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    model_id: Option<String>,
) -> Result<GatewayTestResult, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    ModelGateway::new(center).test_connection(&connection_id, model_id.as_deref())
}

#[tauri::command]
pub fn model_gateway_test_model(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    configured_model_id: String,
) -> Result<GatewayTestResult, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    ModelGateway::new(center).test_model(&configured_model_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::InMemoryVault;
    use crate::model_center::{SaveConfiguredModelInput, SaveConnectionInput};
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;
    use tempfile::tempdir;

    fn center() -> Arc<ModelCenter> {
        let directory = tempdir().unwrap();
        Arc::new(
            ModelCenter::new(
                directory.keep().join("model-center.db"),
                Arc::new(InMemoryVault::default()),
            )
            .unwrap(),
        )
    }

    fn response_server(status: &str, body: &str, request_count: usize) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let status = status.to_string();
        let body = body.to_string();
        std::thread::spawn(move || {
            for _ in 0..request_count {
                let (mut stream, _) = listener.accept().unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                while reader.read_line(&mut line).unwrap() > 0 {
                    if line == "\r\n" {
                        break;
                    }
                    line.clear();
                }
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        format!("http://{address}")
    }

    fn one_response_server(status: &str, body: &str) -> String {
        response_server(status, body, 1)
    }

    fn configured_custom(endpoint: String, secret: Option<&str>) -> (Arc<ModelCenter>, String) {
        let center = center();
        center
            .save_connection(SaveConnectionInput {
                connection_id: "custom-local".into(),
                provider_id: "custom".into(),
                display_name: "Custom local".into(),
                region: "local".into(),
                endpoint,
                enabled: true,
            })
            .unwrap();
        if let Some(secret) = secret {
            center.set_secret("custom-local", secret).unwrap();
        }
        let configured_model_id = "custom-model".to_string();
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: configured_model_id.clone(),
                connection_id: "custom-local".into(),
                model_id: "test-model".into(),
                alias: "Test model".into(),
                enabled: true,
                capabilities: vec!["text_chat".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();
        (center, configured_model_id)
    }

    #[test]
    fn openai_compatible_generation_returns_nonsecret_metadata() {
        let endpoint = one_response_server(
            "200 OK",
            r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#,
        );
        let (center, model_id) = configured_custom(endpoint, Some("gateway-secret"));
        let result = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap();
        assert_eq!(result.content, r#"{"ok":true}"#);
        assert_eq!(result.meta.model_id, "test-model");
        assert!(!serde_json::to_string(&result)
            .unwrap()
            .contains("gateway-secret"));
    }

    #[test]
    fn provider_errors_redact_a_secret_even_if_the_server_echoes_it() {
        let endpoint = one_response_server("401 Unauthorized", "gateway-secret");
        let (center, model_id) = configured_custom(endpoint, Some("gateway-secret"));
        let error = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_UNAUTHORIZED");
        assert!(!error.message.contains("gateway-secret"));
        assert!(error.message.contains("[REDACTED]"));
    }

    #[test]
    fn redirects_are_never_followed() {
        let endpoint = one_response_server("302 Found", "{}");
        let (center, model_id) = configured_custom(endpoint, None);
        let error = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_REDIRECT_BLOCKED");
    }

    #[test]
    fn vision_rejects_invalid_media_before_model_lookup_or_network() {
        let gateway = ModelGateway::new(center());
        let error = gateway
            .generate_vision("missing", "system", "user", "image/jpeg", "not-base64")
            .unwrap_err();
        assert_eq!(error.code, "MODEL_VISION_PAYLOAD_INVALID");
    }

    #[test]
    fn vision_requires_an_explicit_vision_capability() {
        let endpoint = one_response_server("200 OK", r#"{}"#);
        let (center, model_id) = configured_custom(endpoint, None);
        let png = base64::engine::general_purpose::STANDARD
            .encode([0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let error = ModelGateway::new(center)
            .generate_vision(&model_id, "system", "user", "image/png", &png)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_CAPABILITY_MISMATCH");
    }
    #[test]
    fn model_test_verifies_declared_vision_with_a_second_request() {
        let endpoint = response_server(
            "200 OK",
            r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#,
            2,
        );
        let (center, model_id) = configured_custom(endpoint, None);
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: model_id.clone(),
                connection_id: "custom-local".into(),
                model_id: "test-model".into(),
                alias: "Vision test model".into(),
                enabled: true,
                capabilities: vec![
                    "text_chat".into(),
                    "structured_json".into(),
                    "vision".into(),
                ],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();

        let result = ModelGateway::new(center).test_model(&model_id).unwrap();
        assert!(result.ok);
        assert!(result.message.contains("declared capabilities"));
    }
    #[test]
    fn connection_test_uses_custom_model_without_persisting_probe() {
        let endpoint = one_response_server(
            "200 OK",
            r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#,
        );
        let (center, _) = configured_custom(endpoint, None);
        let result = ModelGateway::new(center.clone())
            .test_connection("custom-local", Some("manual-model"))
            .unwrap();
        assert_eq!(result.meta.model_id, "manual-model");
        let models = center.list_models().unwrap();
        assert_eq!(models.len(), 1);
        assert_eq!(models[0].model_id, "test-model");
    }

    #[test]
    fn ollama_connection_test_reports_when_no_model_is_installed() {
        let endpoint = one_response_server("200 OK", r#"{"models":[]}"#);
        let center = center();
        center
            .save_connection(SaveConnectionInput {
                connection_id: "ollama-local".into(),
                provider_id: "ollama".into(),
                display_name: "Ollama".into(),
                region: "local".into(),
                endpoint,
                enabled: true,
            })
            .unwrap();
        let error = ModelGateway::new(center)
            .test_connection("ollama-local", None)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_NO_INSTALLED_MODEL");
        assert!(error.message.contains("Install a model"));
    }

    #[test]
    fn billing_marks_only_verified_free_paths() {
        assert_eq!(
            provider_registry::billing_class_for("openrouter", "openrouter/free"),
            "free_tier"
        );
        assert_eq!(
            provider_registry::billing_class_for("openrouter", "provider/paid-model"),
            "unknown"
        );
        assert_eq!(
            provider_registry::billing_class_for("ollama", "qwen3:8b"),
            "local_no_provider_fee"
        );
    }

    #[test]
    fn private_address_policy_rejects_reserved_ranges() {
        assert!(!is_public_ip("10.0.0.1".parse().unwrap()));
        assert!(!is_public_ip("169.254.1.1".parse().unwrap()));
        assert!(!is_public_ip("::1".parse().unwrap()));
        assert!(!is_public_ip("2001:db8::1".parse().unwrap()));
        assert!(is_public_ip("1.1.1.1".parse().unwrap()));
    }
}






