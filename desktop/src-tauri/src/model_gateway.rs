use crate::credentials::platform_vault;
use crate::model_center::{
    resolve_model_center_db_path, ConfiguredModelRuntime, ConnectionRuntime, ConnectionSummary,
    ModelCenter, ModelCenterState,
};
use crate::provider_registry;
use crate::model_stream::{ProviderStreamDecoder, StreamFormat};
use base64::Engine as _;
use reqwest::{Client, Response};
use reqwest::redirect::Policy;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, State};
use zeroize::{Zeroize, Zeroizing};

const MAX_GATEWAY_REQUEST_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_RESPONSE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_ERROR_CHARS: usize = 800;

#[derive(Clone)]
pub(crate) struct ModelGateway {
    center: Arc<ModelCenter>,
    events: Option<Arc<GatewayEventSink>>,
    max_output_tokens: Option<u32>,
}

impl ModelGateway {
    pub(crate) fn new(center: Arc<ModelCenter>) -> Self {
        Self { center, events: None, max_output_tokens: None }
    }

    fn with_output_token_budget(mut self, max_output_tokens: Option<u32>) -> Self {
        self.max_output_tokens = max_output_tokens;
        self
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
        let url = endpoint_url(
            &connection.endpoint,
            provider.models_path.unwrap_or("/models"),
        )?;
        let request = authorize(client.get(url), secret_ref)?;
        let response = tauri::async_runtime::block_on(async { request.send().await })
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
            true,
        )
    }

    fn generate_once(
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
            false,
        )
    }

    fn generate_with_secret(
        &self,
        configured_model_id: &str,
        system_prompt: &str,
        user_prompt: &str,
        json_mode: bool,
        secret_override: Option<&str>,
        allow_format_fallback: bool,
    ) -> Result<GatewayGenerateResult, GatewayError> {
        if system_prompt.len() + user_prompt.len() > 7 * 1024 * 1024 {
            return Err(GatewayError::new(
                "MODEL_REQUEST_BODY_TOO_LARGE",
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
                allow_format_fallback,
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
        self.generate_vision_with_format(
            configured_model_id,
            system_prompt,
            user_prompt,
            image_media_type,
            image_base64,
            true,
            true,
        )
    }

    fn generate_vision_with_format(
        &self,
        configured_model_id: &str,
        system_prompt: &str,
        user_prompt: &str,
        image_media_type: &str,
        image_base64: &str,
        json_mode: bool,
        allow_format_fallback: bool,
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
            let mut payload = json!({
                "model": model.model_id,
                "stream": false,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt, "images": [image_base64]}
                ]
            });
            if json_mode {
                payload["format"] = json!("json");
            }
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
                ]
            });
            if json_mode {
                payload["response_format"] = json!({"type": "json_object"});
            }
            if let Some(temperature) = model.temperature {
                payload["temperature"] = json!(temperature);
            }
            match post_json(&client, &url, &payload, secret_ref) {
                Err(error)
                    if json_mode
                        && allow_format_fallback
                        && response_format_is_unsupported(&error) =>
                {
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
            parse_openai_compatible_message(
                &response,
                "The vision response did not contain message.content.",
            )?
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
        allow_format_fallback: bool,
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
        let output_budget_support = self.max_output_tokens.map(|tokens| {
            provider_registry::apply_output_token_budget(
                &model.provider_id,
                &model.model_id,
                &mut payload,
                tokens,
            )
        });
        let response = match self.post_text(client, &url, &mut payload, secret, &model.provider_id) {
            Err(error)
                if json_mode && allow_format_fallback && response_format_is_unsupported(&error) =>
            {
                payload
                    .as_object_mut()
                    .expect("gateway payload is an object")
                    .remove("response_format");
                self.post_text(client, &url, &mut payload, secret, &model.provider_id)?
            }
            result => result?,
        };
        let (content, finish_reason) = parse_openai_compatible_message(
            &response,
            "The provider response did not contain message.content.",
        )?;
        let mut result = self.result(model, content, finish_reason);
        result.meta.apply_usage(&response);
        apply_output_budget_receipt(&mut result.meta, output_budget_support);
        Ok(result)
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
        let output_budget_support = self.max_output_tokens.map(|tokens| {
            provider_registry::apply_output_token_budget(
                &model.provider_id,
                &model.model_id,
                &mut payload,
                tokens,
            )
        });
        let response = self.post_text(client, &url, &mut payload, secret, &model.provider_id)?;
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
        let mut result = self.result(model, content, finish_reason);
        result.meta.apply_usage(&response);
        apply_output_budget_receipt(&mut result.meta, output_budget_support);
        Ok(result)
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
                output_budget_tokens: None,
                output_budget_wire_field: None,
                output_budget_semantics: None,
                input_tokens: None,
                output_tokens: None,
                cache_read_input_tokens: None,
                cache_write_input_tokens: None,
                reasoning_tokens: None,
                answer_tokens: None,
            },
            content,
        }
    }

    fn post_text(
        &self, client: &Client, url: &reqwest::Url, payload: &mut Value,
        secret: Option<&str>, provider_id: &str,
    ) -> Result<Value, GatewayError> {
        let Some(events) = self.events.as_ref() else {
            return post_json(client, url, payload, secret);
        };
        let protocol = provider_registry::text_stream_protocol(provider_id);
        if protocol.is_some() {
            payload["stream"] = json!(true);
            // These adapters explicitly document include_usage. Do not send it
            // to an arbitrary endpoint merely labelled OpenAI-compatible.
            if provider_registry::stream_requests_usage(provider_id) {
                payload["stream_options"] = json!({"include_usage": true});
            }
        }
        events.activity("connecting", 0);
        let request = authorize(client.post(url.clone()).json(payload), secret)?;
        let mut response = tauri::async_runtime::block_on(async { request.send().await })
            .map_err(|_| GatewayError::network("The model provider could not be reached."))?;
        events.activity("waiting_response", 0);
        if !response.status().is_success() {
            return read_json_response(response, secret);
        }
        let content_type = response.headers().get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok()).unwrap_or("").to_ascii_lowercase();
        let format = match protocol {
            Some("sse") if content_type.contains("text/event-stream") => StreamFormat::Sse,
            Some("ndjson") if content_type.contains("ndjson") || content_type.contains("jsonl") => StreamFormat::Ndjson,
            _ => return read_json_response(response, secret),
        };
        let mut decoder = ProviderStreamDecoder::new(format)
            .with_reasoning_fields(provider_registry::stream_reasoning_fields(provider_id));
        loop {
            let previous_length = decoder.partial_content().len();
            let chunk = match tauri::async_runtime::block_on(async { response.chunk().await }) {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(error) => {
                    events.preserve_partial(decoder.partial_content());
                    return Err(GatewayError::new(
                        if error.is_timeout() { "MODEL_TIMEOUT" } else { "MODEL_STREAM_INTERRUPTED" },
                        "The remote stream did not complete within this request; submission outcome is unknown.", false));
                }
            };
            match decoder.push(&chunk) {
                Ok(activity) => {
                    let added = &decoder.partial_content()[previous_length..];
                    if !added.is_empty() { events.frame("content", json!({"text": added})); }
                    for item in activity { events.activity(item.kind, item.bytes); }
                },
                Err(code) => {
                    events.preserve_partial(decoder.partial_content());
                    return Err(GatewayError::new(code, "The remote stream could not be completed.", false));
                }
            }
            if decoder.is_complete() { break; }
        }
        let partial = decoder.partial_content().to_string();
        decoder.finish().map_err(|code| {
            events.preserve_partial(&partial);
            GatewayError::new(code, "The remote stream has no confirmed terminal record.", false)
        })
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
                true,
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
            let _ =
                self.center
                    .mark_connection_status(connection_id, "disabled", Some(&error.code));
            return Err(error);
        }
        let provider = provider_registry::find(&connection.provider_id)
            .ok_or_else(|| GatewayError::configuration("Unknown provider."))?;
        let requested_model_id = requested_model_id
            .map(str::trim)
            .filter(|value| !value.is_empty());
        if let Some(model_id) = requested_model_id {
            provider_registry::validate_model_id(model_id).map_err(GatewayError::configuration)?;
        }
        let discovered_ollama_model = if requested_model_id.is_none()
            && connection.provider_id == "ollama"
        {
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
        if requested_model_id.is_none()
            && connection.provider_id == "custom"
            && provider.default_test_model_id.is_none()
            && configured_model.is_none()
        {
            let discovered = self.discover_models_with_secret(connection_id, None)?;
            let _ = self
                .center
                .mark_connection_status(connection_id, "ready", None);
            let endpoint_host = reqwest::Url::parse(&connection.endpoint)
                .ok()
                .and_then(|url| url.host_str().map(str::to_string))
                .unwrap_or_default();
            return Ok(GatewayTestResult {
                ok: true,
                message: format!(
                    "Endpoint responded; model inference was not tested ({} model(s) discovered).",
                    discovered.len()
                ),
                meta: GatewayResponseMeta {
                    configured_model_id: format!("connection-probe.{connection_id}"),
                    connection_id: connection_id.to_string(),
                    provider_id: connection.provider_id.clone(),
                    model_id: "endpoint-probe".to_string(),
                    alias: format!("{} endpoint probe", provider.display_name),
                    endpoint_host,
                    finish_reason: None,
                    content_length: 0,
                    credential_version: 0,
                    connection_configuration_version: 0,
                    model_configuration_version: 0,
                    billing_class: "probe".to_string(),
                    free_source_url: None,
                    free_verified_at: None,
                    temperature: None,
                    timeout_seconds: 30,
                    output_budget_tokens: None,
                    output_budget_wire_field: None,
                    output_budget_semantics: None,
                    input_tokens: None,
                    output_tokens: None,
                    cache_read_input_tokens: None,
                    cache_write_input_tokens: None,
                    reasoning_tokens: None,
                    answer_tokens: None,
                },
                capabilities: Vec::new(),
                warnings: Vec::new(),
            });
        }
        let model_id = requested_model_id
            .map(str::to_string)
            .or(discovered_ollama_model)
            .or_else(|| provider.default_test_model_id.map(str::to_string))
            .or(configured_model)
            .ok_or_else(|| {
                GatewayError::new(
                    "MODEL_TEST_MODEL_REQUIRED",
                    "Select or add a model before testing this custom connection.",
                    false,
                )
            })?;
        provider_registry::validate_model_id(&model_id).map_err(GatewayError::configuration)?;
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
            billing_class: provider_registry::billing_class_for(&connection.provider_id, &model_id)
                .to_string(),
            free_source_url: provider_registry::free_source_url(&connection.provider_id, &model_id)
                .map(str::to_string),
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
                    capabilities: Vec::new(),
                    warnings: Vec::new(),
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
        let model = self
            .center
            .configured_model_runtime(configured_model_id)
            .map_err(GatewayError::configuration)?;
        let mut capabilities = Vec::new();
        let mut warnings = Vec::new();
        let requested_versions = (model.configuration_version, model.connection_configuration_version,
                                  model.credential_version);
        let text_result = match self.generate(
            configured_model_id,
            "You are a connection test. Return JSON only.",
            r#"{"task":"Return {\"ok\":true}."}"#,
            false,
        ) {
            Ok(result) => {
                self.record_capability(configured_model_id, "text_chat", "ready", None,
                                       probe_versions(&result.meta))?;
                capabilities.push(GatewayCapabilityResult::ready("text_chat"));
                result
            }
            Err(error) => {
                self.record_capability(
                    configured_model_id,
                    "text_chat",
                    "error",
                    Some(&error),
                    requested_versions,
                )?;
                return Err(self.finish_model_test_error(&model, error));
            }
        };

        if model
            .capabilities
            .iter()
            .any(|capability| capability == "structured_json")
        {
            if serde_json::from_str::<Value>(&text_result.content)
                .is_ok_and(|value| value.is_object())
            {
                self.record_capability(configured_model_id, "structured_json", "ready", None,
                                       probe_versions(&text_result.meta))?;
                capabilities.push(GatewayCapabilityResult::ready("structured_json"));
            } else {
                let error = GatewayError::new(
                    "MODEL_STRUCTURED_JSON_INVALID",
                    "The text response was valid text but not a JSON object.",
                    false,
                );
                self.record_capability(
                    configured_model_id,
                    "structured_json",
                    "error",
                    Some(&error),
                    probe_versions(&text_result.meta),
                )?;
                capabilities.push(GatewayCapabilityResult::error(
                    "structured_json",
                    &error,
                ));
                warnings.push(error.message.clone());
            }
        }

        if model
            .capabilities
            .iter()
            .any(|capability| capability == "vision")
        {
            const TEST_PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
            match self.generate_vision(
                configured_model_id,
                "You are a vision connection test. Return JSON only.",
                r#"{"task":"Confirm that an image was received and return {\"ok\":true}."}"#,
                "image/png",
                TEST_PNG,
            ) {
                Ok(result) => {
                    self.record_capability(configured_model_id, "vision", "ready", None,
                                           probe_versions(&result.meta))?;
                    capabilities.push(GatewayCapabilityResult::ready("vision"));
                }
                Err(error) => {
                    self.record_capability(
                        configured_model_id,
                        "vision",
                        "error",
                        Some(&error),
                        requested_versions,
                    )?;
                    capabilities.push(GatewayCapabilityResult::error("vision", &error));
                    warnings.push(error.message.clone());
                }
            }
        }

        let ok = capabilities.iter().all(|check| check.status == "ready");
        self.center.mark_probe_health_for_versions(configured_model_id, "ready", None,
            probe_versions(&text_result.meta)).map_err(GatewayError::configuration)?;
        Ok(GatewayTestResult {
            ok,
            message: if ok {
                "Connection and declared capabilities succeeded.".to_string()
            } else {
                format!(
                    "Text connection succeeded; capability checks need attention: {}.",
                    warnings.join(" ")
                )
            },
            meta: text_result.meta,
            capabilities,
            warnings,
        })
    }

    fn record_capability(
        &self,
        configured_model_id: &str,
        capability: &str,
        status: &str,
        error: Option<&GatewayError>,
        versions: (i64, i64, i64),
    ) -> Result<(), GatewayError> {
        self.center.record_capability_check_for_versions(
            configured_model_id,
            capability,
            status,
            error.map(|value| value.code.as_str()),
            error.and_then(|value| value.retry_after_seconds),
            versions,
        ).map_err(GatewayError::configuration)
    }

    fn finish_model_test_error(
        &self,
        model: &ConfiguredModelRuntime,
        error: GatewayError,
    ) -> GatewayError {
        let status = health_status_for_error(&error.code);
        if let Err(local_error) = self.center.mark_probe_health_for_versions(
            &model.configured_model_id, status, Some(&error.code),
            (model.configuration_version, model.connection_configuration_version, model.credential_version)) {
            return GatewayError::configuration(local_error);
        }
        error
    }
}

fn probe_versions(meta: &GatewayResponseMeta) -> (i64, i64, i64) {
    (meta.model_configuration_version, meta.connection_configuration_version, meta.credential_version)
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_budget_tokens: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_budget_wire_field: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_budget_semantics: Option<String>,
    #[serde(default)]
    pub input_tokens: Option<u64>,
    #[serde(default)]
    pub output_tokens: Option<u64>,
    #[serde(default)]
    pub cache_read_input_tokens: Option<u64>,
    #[serde(default)]
    pub cache_write_input_tokens: Option<u64>,
    #[serde(default)]
    pub reasoning_tokens: Option<u64>,
    #[serde(default)]
    pub answer_tokens: Option<u64>,
}

fn apply_output_budget_receipt(
    meta: &mut GatewayResponseMeta,
    support: Option<provider_registry::OutputTokenBudgetSupport>,
) {
    if let Some(provider_registry::OutputTokenBudgetSupport::Applied(applied)) = support {
        meta.output_budget_tokens = Some(applied.applied_tokens);
        meta.output_budget_wire_field = Some(applied.wire_field.to_string());
        meta.output_budget_semantics = Some(applied.semantics.to_string());
    }
}

impl GatewayResponseMeta {
    fn apply_usage(&mut self, response: &Value) {
        // Missing is unknown, not zero. A cache miss is not a cache write.
        let integer = |paths: &[&str]| paths.iter().find_map(|path|
            response.pointer(path).and_then(Value::as_u64));
        let ollama = self.provider_id == "ollama";
        self.input_tokens = integer(if ollama { &["/prompt_eval_count"] } else { &["/usage/prompt_tokens"] });
        self.output_tokens = integer(if ollama { &["/eval_count"] } else { &["/usage/completion_tokens"] });
        self.reasoning_tokens = match self.provider_id.as_str() {
            "openai" | "deepseek" | "qwen" | "openrouter" => integer(&["/usage/completion_tokens_details/reasoning_tokens"]),
            _ => None,
        };
        // Only a separately reported text counter proves visible-answer usage.
        // completion - reasoning is not safe with audio/prediction categories.
        self.answer_tokens = match self.provider_id.as_str() {
            "openai" => integer(&["/usage/completion_tokens_details/text_tokens"]),
            _ => None,
        };
        self.cache_read_input_tokens = match self.provider_id.as_str() {
            "deepseek" => integer(&["/usage/prompt_cache_hit_tokens"]),
            "ollama" => integer(&["/prompt_eval_cached_count"]),
            "qwen" | "kimi_cn" | "kimi_global" => integer(&["/usage/prompt_tokens_details/cached_tokens", "/usage/cached_tokens"]),
            "openai" | "glm" | "openrouter" => integer(&["/usage/prompt_tokens_details/cached_tokens"]),
            _ => None,
        };
        self.cache_write_input_tokens = match self.provider_id.as_str() {
            "openai" | "kimi_cn" | "kimi_global" | "openrouter" => integer(&["/usage/prompt_tokens_details/cache_write_tokens"]),
            _ => None,
        };
    }
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
    pub capabilities: Vec<GatewayCapabilityResult>,
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayCapabilityResult {
    pub capability: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_seconds: Option<u64>,
}

impl GatewayCapabilityResult {
    fn ready(capability: &str) -> Self {
        Self {
            capability: capability.to_string(),
            status: "ready".to_string(),
            error_code: None,
            retry_after_seconds: None,
        }
    }

    fn error(capability: &str, error: &GatewayError) -> Self {
        Self {
            capability: capability.to_string(),
            status: "error".to_string(),
            error_code: Some(error.code.clone()),
            retry_after_seconds: error.retry_after_seconds,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct GatewayError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
}

impl GatewayError {
    fn new(code: &str, message: impl Into<String>, retryable: bool) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
            retryable,
            retry_after_seconds: None,
            http_status: None,
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
    request: reqwest::RequestBuilder,
    secret: Option<&str>,
) -> Result<reqwest::RequestBuilder, GatewayError> {
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
    let response = tauri::async_runtime::block_on(async { request.send().await })
        .map_err(|_| GatewayError::network("The model provider could not be reached."))?;
    read_json_response(response, secret)
}

fn read_json_response(response: Response, secret: Option<&str>) -> Result<Value, GatewayError> {
    tauri::async_runtime::block_on(read_json_response_async(response, secret))
}

async fn read_json_response_async(mut response: Response, secret: Option<&str>) -> Result<Value, GatewayError> {
    let status = response.status();
    let host = response.url().host_str().unwrap_or("").to_string();
    let retry_after_seconds = if status.as_u16() == 429 {
        response
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .and_then(parse_retry_after_seconds)
    } else {
        None
    };
    if matches!(status.as_u16(), 401 | 403) {
        let (code, retryable) = match status.as_u16() {
            401 | 403 => ("MODEL_UNAUTHORIZED", false),
            _ => unreachable!(),
        };
        let mut error = GatewayError::new(
            code,
            format!("The provider returned HTTP {}.", status.as_u16()),
            retryable,
        );
        error.retry_after_seconds = retry_after_seconds;
        error.http_status = Some(status.as_u16());
        return Err(error);
    }
    if status.is_redirection() {
        return Err(GatewayError::new(
            "MODEL_REDIRECT_BLOCKED",
            "The provider attempted an HTTP redirect, which the credential policy blocks.",
            false,
        ));
    }
    if status.is_success() && response
        .content_length()
        .is_some_and(|length| length > MAX_PROVIDER_RESPONSE_BYTES)
    {
        return Err(GatewayError::new(
            "MODEL_RESPONSE_TOO_LARGE",
            "The provider response exceeds the gateway limit.",
            false,
        ));
    }
    let error_body = !status.is_success();
    let limit = if error_body { 64 * 1024 } else { MAX_PROVIDER_RESPONSE_BYTES } as usize;
    let read_body = async move {
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| ())? {
            if body.len() + chunk.len() > limit {
                body.zeroize();
                return Err(());
            }
            body.extend_from_slice(&chunk);
        }
        Ok(body)
    };
    // Unlike a detached blocking reader, dropping this timed-out future also
    // drops the response and aborts its body. No lingering 600-second reader.
    let mut body = if error_body {
        // Reserve 500ms of the two-second error budget for dispatch and
        // classification; the body future is dropped on expiry.
        tokio::time::timeout(Duration::from_millis(1500), read_body).await
            .ok().and_then(Result::ok).unwrap_or_default()
    } else {
        read_body.await.map_err(|_| GatewayError::new("MODEL_RESPONSE_TOO_LARGE",
            "The provider response was incomplete or exceeded the gateway limit.", false))?
    };
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
        let lowered_detail = detail.to_ascii_lowercase();
        let provider_error = serde_json::from_str::<Value>(&detail).ok();
        let error_code = provider_error.as_ref().and_then(|value| value.pointer("/error/code"));
        let quota_exhausted = matches!(error_code.and_then(Value::as_str),
            Some("insufficient_quota" | "quota_exceeded" | "billing_hard_limit_reached"))
            || host == "open.bigmodel.cn" && (error_code.and_then(Value::as_str) == Some("1308")
                || error_code.and_then(Value::as_u64) == Some(1308));
        let context_capacity = [
            "context_length_exceeded",
            "maximum context length",
            "context window",
            "too many tokens",
            "prompt is too long",
            "input is too long",
        ]
        .iter()
        .any(|marker| lowered_detail.contains(marker));
        let code = match status.as_u16() {
            401 | 403 => "MODEL_UNAUTHORIZED",
            413 => "MODEL_REQUEST_BODY_TOO_LARGE",
            408 => "MODEL_TIMEOUT",
            429 if quota_exhausted => "MODEL_QUOTA_EXHAUSTED",
            429 => "MODEL_RATE_LIMITED",
            value if value >= 500 => "MODEL_PROVIDER_UNAVAILABLE",
            _ if context_capacity => "MODEL_CONTEXT_CAPACITY",
            400 | 422
                if lowered_detail.contains("response_format")
                    && ["unsupported", "not support", "unknown", "invalid"]
                        .iter()
                        .any(|marker| lowered_detail.contains(marker)) =>
            {
                "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
            }
            _ => "MODEL_HTTP_ERROR",
        };
        let retryable = !quota_exhausted &&
            (matches!(status.as_u16(), 408 | 409 | 425 | 429) || status.is_server_error());
        // Error body is only classification input, never user-visible request
        // echoes, provider debug traces, credentials or private prompt text.
        let message = format!("The provider returned HTTP {} ({code}).", status.as_u16());
        let mut error = GatewayError::new(code, message, retryable);
        error.retry_after_seconds = retry_after_seconds;
        error.http_status = Some(status.as_u16());
        return Err(error);
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

fn parse_retry_after_seconds(value: &str) -> Option<u64> {
    if let Ok(seconds) = value.trim().parse::<u64>() { return Some(seconds.max(1)); }
    let date = httpdate::parse_http_date(value.trim()).ok()?;
    match date.duration_since(std::time::SystemTime::now()) {
        Ok(wait) => Some(wait.as_secs().saturating_add(u64::from(wait.subsec_nanos() > 0)).max(1)),
        Err(_) => Some(1),
    }
}

fn response_format_is_unsupported(error: &GatewayError) -> bool {
    error.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
        && matches!(error.http_status, Some(400) | Some(422))
}

fn parse_openai_compatible_message(
    response: &Value,
    invalid_content_message: &str,
) -> Result<(String, Option<String>), GatewayError> {
    let finish_reason = response
        .pointer("/choices/0/finish_reason")
        .and_then(Value::as_str);
    let message = response
        .pointer("/choices/0/message")
        .and_then(Value::as_object);
    let refusal = message
        .and_then(|value| value.get("refusal"))
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let is_refusal =
        refusal.is_some() || matches!(finish_reason, Some("refusal") | Some("content_filter"));
    if is_refusal {
        let content = refusal
            .or_else(|| {
                message
                    .and_then(|value| value.get("content"))
                    .and_then(Value::as_str)
            })
            .unwrap_or_default()
            .to_string();
        let normalized_finish_reason = if finish_reason == Some("content_filter") {
            "content_filter"
        } else {
            "refusal"
        };
        return Ok((content, Some(normalized_finish_reason.to_string())));
    }
    let content = message
        .and_then(|value| value.get("content"))
        .and_then(Value::as_str)
        .ok_or_else(|| {
            GatewayError::new("MODEL_RESPONSE_INVALID", invalid_content_message, false)
        })?;
    Ok((content.to_string(), finish_reason.map(str::to_string)))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct GatewayEventContext {
    protocol: String,
    request_id: String,
    run_id: String,
    stage_id: String,
    generation: u64,
    attempt_id: String,
}

struct GatewayEventSink {
    context: GatewayEventContext,
    seq: Mutex<u64>,
    partial: Mutex<Option<String>>,
}

impl GatewayEventSink {
    fn new(context: GatewayEventContext) -> Result<Self, GatewayError> {
        if context.protocol != "model-events-v1" ||
            [&context.request_id, &context.run_id, &context.stage_id, &context.attempt_id]
                .iter().any(|value| value.is_empty() || value.len() > 256 || value.chars().any(char::is_control)) {
            return Err(GatewayError::new("MODEL_GATEWAY_PROTOCOL_ERROR", "Invalid event identity.", false));
        }
        Ok(Self { context, seq: Mutex::new(0), partial: Mutex::new(None) })
    }

    fn frame(&self, kind: &str, data: Value) {
        let Ok(mut seq) = self.seq.lock() else { return; };
        *seq += 1;
        let frame = json!({"protocol": "model-events-v1", "context": self.context,
                           "seq": *seq, "kind": kind, "data": data});
        let mut stdout = std::io::stdout().lock();
        let _ = serde_json::to_writer(&mut stdout, &frame);
        let _ = stdout.write_all(b"\n");
        let _ = stdout.flush();
    }

    fn activity(&self, state: &str, bytes: usize) {
        self.frame("activity", json!({"state": state, "bytes": bytes}));
    }

    fn preserve_partial(&self, content: &str) {
        if !content.is_empty() {
            if let Ok(mut partial) = self.partial.lock() { *partial = Some(content.to_string()); }
        }
    }
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
    #[serde(default)]
    wire_contract: String,
    #[serde(default)]
    max_output_tokens: Option<u32>,
    #[serde(default)]
    event_context: Option<GatewayEventContext>,
}

fn default_json_mode() -> bool {
    true
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
struct GatewayCliResponse {
    ok: bool,
    wire_contract: &'static str,
    result: Option<GatewayGenerateResult>,
    error: Option<GatewayError>,
}

pub fn run_model_gateway_stdio() -> i32 {
    let request = read_cli_request();
    let events = match request.as_ref().ok().and_then(|value| value.event_context.clone()) {
        Some(context) => GatewayEventSink::new(context).ok().map(Arc::new),
        None => None,
    };
    let wire_contract = match request.as_ref().map(|value| value.operation.as_str()) {
        Ok("generate") => "text-single-request-v1",
        Ok("vision") => "vision-single-request-v1",
        _ => "",
    };
    let response = match request.and_then(|request| run_cli_request_with_events(request, events.clone())) {
        Ok(result) => GatewayCliResponse {
            ok: true,
            wire_contract,
            result: Some(result),
            error: None,
        },
        Err(error) => GatewayCliResponse {
            ok: false,
            wire_contract,
            result: None,
            error: Some(error),
        },
    };
    if let Some(events) = events {
        let partial = events.partial.lock().ok().and_then(|value| value.clone());
        events.frame("terminal", json!({"response": response, "partial_content": partial}));
        return 0;
    }
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
    let request: GatewayCliRequest = parsed?;
    validate_output_token_budget(request.max_output_tokens)?;
    Ok(request)
}

fn validate_output_token_budget(max_output_tokens: Option<u32>) -> Result<(), GatewayError> {
    if max_output_tokens == Some(0) {
        Err(GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The output-token budget must be a positive integer.",
            false,
        ))
    } else {
        Ok(())
    }
}

fn run_cli_request(request: GatewayCliRequest) -> Result<GatewayGenerateResult, GatewayError> {
    run_cli_request_with_events(request, None)
}

fn run_cli_request_with_events(request: GatewayCliRequest, events: Option<Arc<GatewayEventSink>>) -> Result<GatewayGenerateResult, GatewayError> {
    validate_output_token_budget(request.max_output_tokens)?;
    if request.event_context.is_some() && events.is_none() {
        return Err(GatewayError::new("MODEL_GATEWAY_PROTOCOL_ERROR", "Invalid event identity.", false));
    }
    // This contract has exactly one wire attempt. Its Python owner persists
    // authorization, debit and intent before any compatible format fallback.
    let required_contract = match request.operation.as_str() {
        "generate" => "text-single-request-v1",
        "vision" => "vision-single-request-v1",
        _ => "",
    };
    if required_contract.is_empty() || request.wire_contract != required_contract {
        return Err(GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The model wire contract is incompatible.",
            false,
        ));
    }
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
    let mut gateway = ModelGateway::new(center).with_output_token_budget(
        if request.operation == "generate" {
            request.max_output_tokens
        } else {
            None
        },
    );
    gateway.events = events;
    match request.operation.as_str() {
        "generate" => gateway.generate_once(
            &request.configured_model_id,
            &request.system_prompt,
            &request.user_prompt,
            request.json_mode,
        ),
        "vision" => gateway.generate_vision_with_format(
            &request.configured_model_id,
            &request.system_prompt,
            &request.user_prompt,
            &request.image_media_type,
            &request.image_base64,
            request.json_mode,
            false,
        ),
        _ => Err(GatewayError::new(
            "MODEL_GATEWAY_PROTOCOL_ERROR",
            "The gateway operation is not supported.",
            false,
        )),
    }
}
#[tauri::command]
pub async fn model_gateway_discover_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
) -> Result<Vec<DiscoveredModel>, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    let worker_center = center.clone();
    let worker_connection = connection_id.clone();
    let result = tauri::async_runtime::spawn_blocking(move ||
        ModelGateway::new(worker_center).discover_models(&worker_connection)).await
        .map_err(|_| GatewayError::new("MODEL_GATEWAY_UNAVAILABLE", "Model discovery worker stopped.", false))?;
    match result {
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
pub async fn model_gateway_rotate_connection_secret(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    secret: String,
) -> Result<ConnectionSummary, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    let secret = Zeroizing::new(secret);
    tauri::async_runtime::spawn_blocking(move ||
        ModelGateway::new(center).rotate_connection_secret(&connection_id, secret.as_str())).await
        .map_err(|_| GatewayError::new("MODEL_GATEWAY_UNAVAILABLE", "Credential update worker stopped.", false))?
}
#[tauri::command]
pub async fn model_gateway_test_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    model_id: Option<String>,
) -> Result<GatewayTestResult, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    tauri::async_runtime::spawn_blocking(move ||
        ModelGateway::new(center).test_connection(&connection_id, model_id.as_deref())).await
        .map_err(|_| GatewayError::new("MODEL_GATEWAY_UNAVAILABLE", "Connection test worker stopped.", false))?
}

#[tauri::command]
pub async fn model_gateway_test_model(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    configured_model_id: String,
) -> Result<GatewayTestResult, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    tauri::async_runtime::spawn_blocking(move ||
        ModelGateway::new(center).test_model(&configured_model_id)).await
        .map_err(|_| GatewayError::new("MODEL_GATEWAY_UNAVAILABLE", "Model test worker stopped.", false))?
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::InMemoryVault;
    use crate::model_center::{SaveConfiguredModelInput, SaveConnectionInput};
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::time::Instant;
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
        response_server_with_retry_after(status, body, request_count, None)
    }

    fn response_server_with_retry_after(
        status: &str,
        body: &str,
        request_count: usize,
        retry_after: Option<&str>,
    ) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let status = status.to_string();
        let body = body.to_string();
        let retry_after = retry_after
            .map(|seconds| format!("Retry-After: {seconds}\r\n"))
            .unwrap_or_default();
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
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{retry_after}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len(),
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        format!("http://{address}")
    }

    fn one_response_server(status: &str, body: &str) -> String {
        response_server(status, body, 1)
    }

    fn recorded_wire_server(content_type: &str, body: String) -> (reqwest::Url, mpsc::Receiver<Value>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = reqwest::Url::parse(&format!("http://{}/chat", listener.local_addr().unwrap())).unwrap();
        let content_type = content_type.to_string();
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut length = 0;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" { break; }
                if let Some(value) = line.to_lowercase().strip_prefix("content-length:") {
                    length = value.trim().parse::<usize>().unwrap();
                }
            }
            let mut request = vec![0; length];
            reader.read_exact(&mut request).unwrap();
            sender.send(serde_json::from_slice(&request).unwrap()).unwrap();
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        });
        (url, receiver)
    }

    #[test]
    fn documented_provider_policies_preserve_full_requests_and_decode_their_wire() {
        for provider in ["openai", "deepseek", "qwen", "kimi_cn", "kimi_global", "glm", "openrouter", "ollama", "gemini", "custom"] {
            let framing = provider_registry::text_stream_protocol(provider);
            let answer = "完整研究正文，不缩短。";
            let (content_type, body) = match framing {
                Some("ndjson") => ("application/x-ndjson", format!("{}\n{}\n",
                    json!({"message":{"content":answer},"done":false}),
                    json!({"message":{"content":""},"done":true,"done_reason":"stop","prompt_eval_count":11,"eval_count":5}))),
                Some("sse") => ("text/event-stream", format!("data: {}\n\ndata: {}\n\ndata: [DONE]\n\n",
                    json!({"choices":[{"index":0,"delta":{"content":answer},"finish_reason":"stop"}]}),
                    if matches!(provider, "openrouter" | "glm" | "kimi_cn" | "kimi_global" | "deepseek") {
                        json!({"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":5}})
                    } else { json!({"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":5}}) })),
                _ => ("application/json", json!({"choices":[{"message":{"content":answer},"finish_reason":"stop"}]}).to_string()),
            };
            let (url, receiver) = recorded_wire_server(content_type, body);
            let mut gateway = ModelGateway::new(center());
            gateway.events = Some(Arc::new(GatewayEventSink::new(GatewayEventContext {
                protocol: "model-events-v1".into(), request_id: format!("request:{provider}"), run_id: "test-run".into(),
                stage_id: "financial-analyst".into(), generation: 1, attempt_id: "attempt".into(),
            }).unwrap()));
            let mut payload = json!({"model":"kept-model", "messages":[{"role":"system","content":"full system"},{"role":"user","content":"full original evidence"}]});
            let value = gateway.post_text(&Client::new(), &url, &mut payload, None, provider).unwrap();
            let content = if provider == "ollama" { value.pointer("/message/content") } else { value.pointer("/choices/0/message/content") };
            assert_eq!(content.and_then(Value::as_str), Some(answer), "{provider}");
            let sent = receiver.recv_timeout(Duration::from_secs(2)).unwrap();
            assert_eq!(sent["messages"][1]["content"], "full original evidence");
            assert_eq!(sent["model"], "kept-model");
            assert_eq!(sent.get("stream").and_then(Value::as_bool).unwrap_or(false), framing.is_some(), "{provider}");
            assert_eq!(sent.pointer("/stream_options/include_usage").and_then(Value::as_bool),
                provider_registry::stream_requests_usage(provider).then_some(true), "{provider}");
        }
    }

    #[test]
    fn supplier_cache_usage_is_not_inferred_from_misses_or_other_adapters() {
        let (center, id) = configured_custom("http://127.0.0.1:9".into(), None);
        let model = center.configured_model_runtime(&id).unwrap();
        let gateway = ModelGateway::new(center);
        let response = json!({"usage":{"prompt_tokens":100,"completion_tokens":25,"prompt_cache_hit_tokens":7,
            "prompt_cache_miss_tokens":93,"cached_tokens":9,"prompt_tokens_details":{"cached_tokens":11,"cache_write_tokens":13},
            "completion_tokens_details":{"reasoning_tokens":5,"text_tokens":20}},
            "prompt_eval_count":101,"eval_count":26,"prompt_eval_cached_count":17});
        for (provider, read, write) in [("openai",Some(11),Some(13)),("deepseek",Some(7),None),
            ("qwen",Some(11),None),("kimi_cn",Some(11),Some(13)),("kimi_global",Some(11),Some(13)),
            ("glm",Some(11),None),("openrouter",Some(11),Some(13)),("ollama",Some(17),None),
            ("gemini",None,None),("custom",None,None)] {
            let mut meta = gateway.result(&model, String::new(), None).meta;
            meta.provider_id = provider.into();
            meta.apply_usage(&response);
            assert_eq!(meta.cache_read_input_tokens, read, "{provider}");
            assert_eq!(meta.cache_write_input_tokens, write, "{provider}");
            assert_eq!(meta.input_tokens, Some(if provider == "ollama" {101} else {100}));
            assert_eq!(meta.reasoning_tokens, if ["openai", "deepseek", "qwen", "openrouter"].contains(&provider) {Some(5)} else {None});
            assert_eq!(meta.answer_tokens, if provider == "openai" {Some(20)} else {None});
            meta.apply_usage(&json!({}));
            assert_eq!(meta.input_tokens, None);
            assert_eq!(meta.cache_read_input_tokens, None);
            assert_eq!(meta.cache_write_input_tokens, None);
            assert_eq!(meta.reasoning_tokens, None);
            assert_eq!(meta.answer_tokens, None);
        }
    }

    #[test]
    fn known_quota_exhaustion_is_not_retryable_rate_limiting() {
        let endpoint = one_response_server("429 Too Many Requests", r#"{"error":{"code":"insufficient_quota","message":"private echoed input"}}"#);
        let (center, id) = configured_custom(endpoint, None);
        let error = ModelGateway::new(center).generate_once(&id,"system","user",true).unwrap_err();
        assert_eq!(error.code, "MODEL_QUOTA_EXHAUSTED");
        assert!(!error.retryable);
        assert_eq!(error.http_status, Some(429));
        assert!(!error.message.contains("private"));
    }

    fn stalled_error_response_server(
        status: &str,
        retry_after: Option<&str>,
    ) -> (String, mpsc::Sender<()>, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let status = status.to_string();
        let retry_after = retry_after
            .map(|seconds| format!("Retry-After: {seconds}\r\n"))
            .unwrap_or_default();
        let (release, wait_for_release) = mpsc::channel();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            while reader.read_line(&mut line).unwrap() > 0 {
                if line == "\r\n" {
                    break;
                }
                line.clear();
            }
            let partial_body =
                r#"{"error":{"message":"gateway-secret private-provider-body"}}"#;
            let response = format!(
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{retry_after}Content-Length: 4096\r\nConnection: keep-alive\r\n\r\n{partial_body}"
            );
            stream.write_all(response.as_bytes()).unwrap();
            stream.flush().unwrap();
            let _ = wait_for_release.recv_timeout(Duration::from_secs(4));
        });
        (format!("http://{address}"), release, server)
    }

    fn sequenced_response_server(responses: &[(&str, &str)]) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let responses: Vec<(String, String)> = responses
            .iter()
            .map(|(status, body)| ((*status).to_string(), (*body).to_string()))
            .collect();
        std::thread::spawn(move || {
            for (status, body) in responses {
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
                    body.len(),
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });
        format!("http://{address}")
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
    fn provider_auth_errors_do_not_echo_a_secret_response_body() {
        let endpoint = one_response_server("401 Unauthorized", "gateway-secret");
        let (center, model_id) = configured_custom(endpoint, Some("gateway-secret"));
        let error = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_UNAUTHORIZED");
        assert_eq!(error.message, "The provider returned HTTP 401.");
        assert!(!error.message.contains("gateway-secret"));
    }

    #[test]
    fn provider_context_capacity_is_classified_without_exposing_request_text() {
        let endpoint = one_response_server(
            "400 Bad Request",
            r#"{"error":{"code":"context_length_exceeded","message":"maximum context length exceeded"}}"#,
        );
        let (center, model_id) = configured_custom(endpoint, Some("gateway-secret"));
        let error = ModelGateway::new(center)
            .generate(
                &model_id,
                "private system prompt",
                "private user prompt",
                true,
            )
            .unwrap_err();
        assert_eq!(error.code, "MODEL_CONTEXT_CAPACITY");
        assert!(!error.retryable);
        assert!(!error.message.contains("private system prompt"));
        assert!(!error.message.contains("private user prompt"));
        assert!(!error.message.contains("gateway-secret"));
    }

    #[test]
    fn rate_limit_retry_after_is_bounded_and_returned_without_body_details() {
        let endpoint = response_server_with_retry_after(
            "429 Too Many Requests",
            r#"{"error":{"message":"rate limit"}}"#,
            1,
            Some("19"),
        );
        let (center, model_id) = configured_custom(endpoint, None);
        let error = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_RATE_LIMITED");
        assert_eq!(error.retry_after_seconds, Some(19));
        assert!(error.retryable);
    }

    #[test]
    fn authorization_and_rate_limit_headers_fail_before_a_stalled_error_body() {
        for (status, code, retry_after, expected_retry_after, retryable, http_status) in [
            ("401 Unauthorized", "MODEL_UNAUTHORIZED", None, None, false, 401),
            ("403 Forbidden", "MODEL_UNAUTHORIZED", None, None, false, 403),
            (
                "429 Too Many Requests",
                "MODEL_RATE_LIMITED",
                Some("19"),
                Some(19),
                true,
                429,
            ),
        ] {
            let (endpoint, release, server) =
                stalled_error_response_server(status, retry_after);
            let (center, model_id) = configured_custom(endpoint, Some("gateway-secret"));
            let started = Instant::now();
            let error = ModelGateway::new(center)
                .generate(&model_id, "system", "user", true)
                .unwrap_err();
            let elapsed = started.elapsed();
            let _ = release.send(());
            server.join().unwrap();

            assert!(
                elapsed < Duration::from_secs(2),
                "{status} took {elapsed:?} while its response body remained open"
            );
            assert_eq!(error.code, code);
            assert_eq!(error.retryable, retryable);
            assert_eq!(error.retry_after_seconds, expected_retry_after);
            assert_eq!(error.http_status, Some(http_status));
            assert!(!error.message.contains("gateway-secret"));
            assert!(!error.message.contains("private-provider-body"));
        }
    }

    #[test]
    fn retry_after_preserves_server_minimum_delta_seconds() {
        assert_eq!(parse_retry_after_seconds("0"), Some(1));
        assert_eq!(parse_retry_after_seconds("900"), Some(900));
        assert_eq!(parse_retry_after_seconds("tomorrow"), None);
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

    fn configured_vision(endpoint: String) -> (Arc<ModelCenter>, String) {
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
        (center, model_id)
    }

    #[test]
    fn single_vision_wire_returns_format_rejection_without_an_internal_second_request() {
        let (center, model_id) = configured_vision(one_response_server(
            "400 Bad Request",
            r#"{"error":{"message":"response_format unsupported"}}"#,
        ));
        let png = base64::engine::general_purpose::STANDARD
            .encode([0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let error = ModelGateway::new(center)
            .generate_vision_with_format(
                &model_id,
                "system",
                "user",
                "image/png",
                &png,
                true,
                false,
            )
            .unwrap_err();
        // The one-response server is closed. An implicit second POST would
        // turn this into a transport error instead of this explicit rejection.
        assert_eq!(error.code, "MODEL_RESPONSE_FORMAT_UNSUPPORTED");
        assert_eq!(error.http_status, Some(400));
    }

    #[test]
    fn single_text_wire_returns_format_rejection_without_an_internal_second_request() {
        for (status, status_text) in [(400, "400 Bad Request"), (422, "422 Unprocessable Entity")] {
            let endpoint = one_response_server(
                status_text,
                r#"{"error":{"code":"MODEL_RESPONSE_FORMAT_UNSUPPORTED","message":"response_format unsupported"}}"#,
            );
            let (center, model_id) = configured_custom(endpoint, None);
            let error = ModelGateway::new(center)
                .generate_once(&model_id, "system", "user", true)
                .unwrap_err();
            // The one-response server is closed. An implicit second POST would
            // turn this into a transport error instead of this explicit rejection.
            assert_eq!(error.code, "MODEL_RESPONSE_FORMAT_UNSUPPORTED");
            assert_eq!(error.http_status, Some(status));
        }
    }

    #[test]
    fn openai_message_refusal_is_returned_when_content_is_null() {
        let response = json!({
            "choices": [{
                "message": {"content": null, "refusal": "I cannot comply."},
                "finish_reason": "stop"
            }]
        });
        let parsed = parse_openai_compatible_message(
            &response,
            "The provider response did not contain message.content.",
        )
        .unwrap();
        assert_eq!(parsed.0, "I cannot comply.");
        assert_eq!(parsed.1.as_deref(), Some("refusal"));
    }

    #[test]
    fn openai_content_filter_preserves_empty_content_and_finish_reason() {
        let response = json!({
            "choices": [{
                "message": {"content": "", "refusal": null},
                "finish_reason": "content_filter"
            }]
        });
        let parsed = parse_openai_compatible_message(
            &response,
            "The provider response did not contain message.content.",
        )
        .unwrap();
        assert_eq!(parsed.0, "");
        assert_eq!(parsed.1.as_deref(), Some("content_filter"));
    }

    #[test]
    fn openai_non_refusal_still_requires_string_content() {
        let response = json!({
            "choices": [{
                "message": {"content": null},
                "finish_reason": "stop"
            }]
        });
        let error = parse_openai_compatible_message(
            &response,
            "The provider response did not contain message.content.",
        )
        .unwrap_err();
        assert_eq!(error.code, "MODEL_RESPONSE_INVALID");
    }

    #[test]
    fn openai_refusal_is_one_wire_and_does_not_open_format_fallback() {
        let endpoint = one_response_server(
            "200 OK",
            r#"{"choices":[{"message":{"content":null,"refusal":"I cannot comply."},"finish_reason":"stop"}]}"#,
        );
        let (center, model_id) = configured_custom(endpoint, None);
        let result = ModelGateway::new(center)
            .generate(&model_id, "system", "user", true)
            .unwrap();
        assert_eq!(result.content, "I cannot comply.");
        assert_eq!(result.meta.finish_reason.as_deref(), Some("refusal"));
    }

    #[test]
    fn openai_vision_refusal_uses_the_same_one_wire_parser() {
        let (center, model_id) = configured_vision(one_response_server(
            "200 OK",
            r#"{"choices":[{"message":{"content":null,"refusal":"Vision declined."},"finish_reason":"refusal"}]}"#,
        ));
        let png = base64::engine::general_purpose::STANDARD
            .encode([0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let result = ModelGateway::new(center)
            .generate_vision(&model_id, "system", "user", "image/png", &png)
            .unwrap();
        assert_eq!(result.content, "Vision declined.");
        assert_eq!(result.meta.finish_reason.as_deref(), Some("refusal"));
    }

    #[test]
    fn text_wire_json_disabled_preserves_model_prompts_and_omits_response_format() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let (sender, receiver) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut content_length = 0;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    content_length = value.trim().parse::<usize>().unwrap();
                }
            }
            let mut body = vec![0; content_length];
            reader.read_exact(&mut body).unwrap();
            sender
                .send(serde_json::from_slice::<Value>(&body).unwrap())
                .unwrap();
            let response =
                r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#;
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", response.len(), response).unwrap();
        });
        let (center, model_id) = configured_custom(endpoint, None);
        let result = ModelGateway::new(center)
            .generate_once(&model_id, "complete-system", "complete-user", false)
            .unwrap();
        assert_eq!(result.content, r#"{"ok":true}"#);
        let payload = receiver.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(payload["model"], "test-model");
        assert_eq!(payload["messages"][0]["content"], "complete-system");
        assert_eq!(payload["messages"][1]["content"], "complete-user");
        assert!(payload.get("response_format").is_none());
        worker.join().unwrap();
    }

    #[test]
    fn server_error_with_format_words_is_not_a_proven_format_rejection() {
        let (center, model_id) = configured_vision(one_response_server(
            "500 Internal Server Error",
            "response_format unsupported",
        ));
        let png = base64::engine::general_purpose::STANDARD
            .encode([0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let error = ModelGateway::new(center)
            .generate_vision_with_format(
                &model_id,
                "system",
                "user",
                "image/png",
                &png,
                true,
                false,
            )
            .unwrap_err();
        assert_eq!(error.code, "MODEL_PROVIDER_UNAVAILABLE");
        assert!(!response_format_is_unsupported(&error));
    }

    #[test]
    fn legacy_vision_cli_contract_is_rejected_before_configuration_or_network() {
        let request = GatewayCliRequest {
            operation: "vision".into(),
            configured_model_id: "missing".into(),
            system_prompt: "system".into(),
            user_prompt: "user".into(),
            json_mode: true,
            image_media_type: "image/png".into(),
            image_base64: "unused".into(),
            wire_contract: String::new(),
            max_output_tokens: None,
            event_context: None,
        };
        assert_eq!(
            run_cli_request(request).unwrap_err().code,
            "MODEL_GATEWAY_PROTOCOL_ERROR"
        );
    }

    #[test]
    fn cli_output_budget_is_optional_positive_integer_and_zero_is_rejected_early() {
        let base = json!({
            "operation": "generate",
            "configured_model_id": "missing",
            "system_prompt": "system",
            "user_prompt": "user",
            "json_mode": false,
            "wire_contract": "text-single-request-v1"
        });
        let absent: GatewayCliRequest = serde_json::from_value(base.clone()).unwrap();
        assert_eq!(absent.max_output_tokens, None);

        let mut positive = base.clone();
        positive["max_output_tokens"] = json!(8192);
        let parsed: GatewayCliRequest = serde_json::from_value(positive).unwrap();
        assert_eq!(parsed.max_output_tokens, Some(8192));

        for invalid in [json!(-1), json!(1.5), json!("8192"), json!(4294967296u64)] {
            let mut request = base.clone();
            request["max_output_tokens"] = invalid;
            assert!(serde_json::from_value::<GatewayCliRequest>(request).is_err());
        }

        let mut zero = base;
        zero["max_output_tokens"] = json!(0);
        let request: GatewayCliRequest = serde_json::from_value(zero).unwrap();
        let error = run_cli_request(request).unwrap_err();
        assert_eq!(error.code, "MODEL_GATEWAY_PROTOCOL_ERROR");
    }

    #[test]
    fn scoped_output_budget_reaches_only_the_selected_text_request() {
        let response = r#"{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}"#;
        let (capped_endpoint, capped_receiver) = recorded_wire_server("application/json", response.into());
        let (uncapped_endpoint, uncapped_receiver) = recorded_wire_server("application/json", response.into());
        let (center, model_id) = configured_custom(capped_endpoint.to_string(), None);
        let mut capped_model = center.configured_model_runtime(&model_id).unwrap();
        capped_model.provider_id = "openai".into();
        capped_model.model_id = "gpt-5.6-sol".into();
        let mut uncapped_model = capped_model.clone();
        uncapped_model.endpoint = uncapped_endpoint.to_string();
        let full_user_prompt = "evidence-fragment:".repeat(2048);

        let base_gateway = ModelGateway::new(center);
        let scoped_gateway = base_gateway.clone().with_output_token_budget(Some(321));
        let capped_result = scoped_gateway
            .generate_openai_compatible(
                &Client::new(), &capped_model, None, "system", &full_user_prompt, false, false,
            )
            .unwrap();
        let uncapped_result = base_gateway
            .generate_openai_compatible(
                &Client::new(), &uncapped_model, None, "system", "user", false, false,
            )
            .unwrap();

        let capped_payload = capped_receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        let uncapped_payload = uncapped_receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(capped_payload["max_completion_tokens"], 321);
        assert_eq!(capped_payload["messages"][1]["content"], full_user_prompt);
        assert_eq!(capped_payload["messages"][0]["content"], "system");
        assert!(uncapped_payload.get("max_completion_tokens").is_none());
        assert!(uncapped_payload.get("max_tokens").is_none());
        assert_eq!(capped_result.meta.output_budget_tokens, Some(321));
        assert_eq!(capped_result.meta.output_budget_wire_field.as_deref(), Some("max_completion_tokens"));
        assert_eq!(capped_result.meta.output_budget_semantics.as_deref(), Some("total_generated_tokens_including_reasoning"));
        assert_eq!(uncapped_result.meta.output_budget_tokens, None);
        assert_eq!(
            serde_json::to_value(capped_result.meta).unwrap()["output_budget_tokens"],
            321
        );
        assert!(serde_json::to_value(uncapped_result.meta)
            .unwrap()
            .get("output_budget_tokens")
            .is_none());
    }

    #[test]
    fn provider_output_budget_keys_are_observed_on_local_http_wires() {
        let (center, configured_model_id) =
            configured_custom("http://127.0.0.1:9".into(), None);
        let response = r#"{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}"#;
        for (provider, model_id, requested, expected) in [
            ("openai", "gpt-5.6-sol", 200_000, Some(("max_completion_tokens", 128_000))),
            ("openai", "gpt-5.6-terra", 131_072, Some(("max_completion_tokens", 128_000))),
            ("deepseek", "deepseek-v4-flash", 500_000, Some(("max_tokens", 393_216))),
            ("qwen", "qwen3.5-plus", 131_072, Some(("max_completion_tokens", 65_536))),
            ("qwen", "qwen3.8-max", 200_000, Some(("max_completion_tokens", 131_072))),
            ("qwen", "qwen3.7-plus", 131_072, Some(("max_completion_tokens", 131_072))),
            ("qwen", "qwen3.7-flash", 98_304, Some(("max_completion_tokens", 98_304))),
            ("kimi_cn", "kimi-k2.5", 131_072, None),
            ("kimi_global", "kimi-k2.5", 131_072, None),
            ("openrouter", "openai/gpt-4", 131_072, None),
            ("gemini", "gemini-3.7-flash", 131_072, None),
            ("glm", "glm-5.2", 131_072, None),
            ("custom", "private-model", 131_072, None),
            ("qwen", "unclassified-qwen-model", 131_072, None),
            ("qwen", "qwen-plus", 131_072, None),
        ] {
            let (endpoint, receiver) = recorded_wire_server("application/json", response.into());
            let mut model = center
                .configured_model_runtime(&configured_model_id)
                .unwrap();
            model.provider_id = provider.into();
            model.model_id = model_id.into();
            model.endpoint = endpoint.to_string();
            let gateway = ModelGateway::new(center.clone()).with_output_token_budget(Some(requested));
            let result = gateway
                .generate_openai_compatible(
                    &Client::new(), &model, None, "system", "evidence", false, false,
                )
                .unwrap();
            let sent = receiver.recv_timeout(Duration::from_secs(2)).unwrap();
            match expected {
                Some(("max_completion_tokens", actual_tokens)) => {
                    assert_eq!(sent["max_completion_tokens"], actual_tokens, "{provider}/{model_id}");
                    assert!(sent.get("max_tokens").is_none(), "{provider}/{model_id}");
                    assert_eq!(result.meta.output_budget_tokens, Some(actual_tokens));
                    assert_eq!(result.meta.output_budget_wire_field.as_deref(), Some("max_completion_tokens"));
                    assert_eq!(result.meta.output_budget_semantics.as_deref(), Some("total_generated_tokens_including_reasoning"));
                }
                Some(("max_tokens", actual_tokens)) => {
                    assert_eq!(sent["max_tokens"], actual_tokens, "{provider}/{model_id}");
                    assert!(sent.get("max_completion_tokens").is_none(), "{provider}/{model_id}");
                    assert_eq!(result.meta.output_budget_tokens, Some(actual_tokens));
                    assert_eq!(result.meta.output_budget_wire_field.as_deref(), Some("max_tokens"));
                    assert_eq!(result.meta.output_budget_semantics.as_deref(), Some("total_generated_tokens_including_reasoning"));
                }
                _ => {
                    assert!(sent.get("max_tokens").is_none(), "{provider}/{model_id}");
                    assert!(sent.get("max_completion_tokens").is_none(), "{provider}/{model_id}");
                    assert_eq!(result.meta.output_budget_tokens, None);
                    assert_eq!(result.meta.output_budget_wire_field, None);
                    assert_eq!(result.meta.output_budget_semantics, None);
                }
            }
        }

        let (endpoint, receiver) = recorded_wire_server(
            "application/json",
            r#"{"message":{"content":"ok"},"done_reason":"stop"}"#.into(),
        );
        let mut ollama_model = center
            .configured_model_runtime(&configured_model_id)
            .unwrap();
        ollama_model.provider_id = "ollama".into();
        ollama_model.model_id = "qwen3:8b".into();
        ollama_model.endpoint = endpoint.to_string();
        ollama_model.temperature = Some(0.4);
        let result = ModelGateway::new(center)
            .with_output_token_budget(Some(456))
            .generate_ollama(
                &Client::new(), &ollama_model, None, "system", "evidence", false,
            )
            .unwrap();
        let sent = receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(sent.pointer("/options/temperature"), Some(&json!(0.4)));
        assert!(sent.get("max_tokens").is_none());
        assert!(sent.get("max_completion_tokens").is_none());
        assert!(sent.pointer("/options/num_predict").is_none());
        assert_eq!(result.meta.output_budget_tokens, None);
    }

    #[test]
    fn oversized_local_prompt_uses_body_error_without_truncating_or_sending() {
        let prompt = "original-material".repeat(7 * 1024 * 1024 / 17 + 1);
        let prompt_len = prompt.len();
        let error = ModelGateway::new(center())
            .generate_once("missing-model", "", &prompt, false)
            .unwrap_err();
        assert_eq!(error.code, "MODEL_REQUEST_BODY_TOO_LARGE");
        assert_eq!(prompt.len(), prompt_len);
        assert!(error.message.contains("exceeds the gateway limit"));
    }

    #[test]
    fn legacy_text_cli_contract_is_rejected_before_configuration_or_network() {
        let request = GatewayCliRequest {
            operation: "generate".into(),
            configured_model_id: "missing".into(),
            system_prompt: "system".into(),
            user_prompt: "user".into(),
            json_mode: false,
            image_media_type: String::new(),
            image_base64: String::new(),
            wire_contract: String::new(),
            max_output_tokens: None,
            event_context: None,
        };
        assert_eq!(
            run_cli_request(request).unwrap_err().code,
            "MODEL_GATEWAY_PROTOCOL_ERROR"
        );
    }

    #[test]
    fn format_disabled_vision_still_sends_the_complete_image_and_prompt() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        let (sender, receiver) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut content_length = 0;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    content_length = value.trim().parse::<usize>().unwrap();
                }
            }
            assert!(content_length < 1024 * 1024);
            let mut body = vec![0; content_length];
            reader.read_exact(&mut body).unwrap();
            sender
                .send(serde_json::from_slice::<Value>(&body).unwrap())
                .unwrap();
            let response =
                r#"{"choices":[{"message":{"content":"{\"facts\":[]}"},"finish_reason":"stop"}]}"#;
            write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", response.len(), response).unwrap();
        });
        let (center, model_id) = configured_vision(endpoint);
        let png = base64::engine::general_purpose::STANDARD
            .encode([0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let result = ModelGateway::new(center).with_output_token_budget(Some(123))
            .generate_vision_with_format(
                &model_id,
                "entire-system",
                "entire-user",
                "image/png",
                &png,
                false,
                false,
            )
            .unwrap();
        assert_eq!(result.content, r#"{"facts":[]}"#);
        let payload = receiver.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(payload.get("response_format").is_none());
        assert!(payload.get("max_tokens").is_none());
        assert!(payload.get("max_completion_tokens").is_none());
        assert!(payload.pointer("/options/num_predict").is_none());
        assert_eq!(payload["messages"][0]["content"], "entire-system");
        assert_eq!(payload["messages"][1]["content"][0]["text"], "entire-user");
        assert_eq!(
            payload["messages"][1]["content"][1]["image_url"]["url"],
            format!("data:image/png;base64,{png}")
        );
        worker.join().unwrap();
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

        let result = ModelGateway::new(center.clone()).test_model(&model_id).unwrap();
        assert!(result.ok);
        assert!(result.message.contains("declared capabilities"));
        assert_eq!(
            result
                .capabilities
                .iter()
                .map(|check| (check.capability.as_str(), check.status.as_str()))
                .collect::<Vec<_>>(),
            vec![
                ("text_chat", "ready"),
                ("structured_json", "ready"),
                ("vision", "ready"),
            ]
        );
        let checks = center.list_models().unwrap()[0].capability_checks.clone();
        assert_eq!(checks.len(), 3);
        assert!(checks.iter().all(|check| check.status == "ready"));
    }

    #[test]
    fn model_test_keeps_text_ready_when_structured_json_is_not_an_object() {
        let endpoint = one_response_server("200 OK", r#"{"choices":[{"message":{"content":"[]"},"finish_reason":"stop"}]}"#);
        let (center, model_id) = configured_custom(endpoint, None);
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: model_id.clone(),
                connection_id: "custom-local".into(),
                model_id: "test-model".into(),
                alias: "Structured check".into(),
                enabled: true,
                capabilities: vec!["text_chat".into(), "structured_json".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();

        let result = ModelGateway::new(center.clone()).test_model(&model_id).unwrap();
        assert!(!result.ok);
        assert_eq!(
            result
                .capabilities
                .iter()
                .map(|check| (check.capability.as_str(), check.status.as_str()))
                .collect::<Vec<_>>(),
            vec![("text_chat", "ready"), ("structured_json", "error")]
        );
        let checks = center.list_models().unwrap()[0].capability_checks.clone();
        assert_eq!(checks[0].status, "error");
        assert_eq!(checks[0].error_code.as_deref(), Some("MODEL_STRUCTURED_JSON_INVALID"));
        assert_eq!(checks[1].status, "ready");
    }

    #[test]
    fn model_test_vision_failure_does_not_revoke_text_success() {
        let endpoint = sequenced_response_server(&[
            ("200 OK", r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#),
            ("500 Internal Server Error", r#"{"error":{"message":"temporary provider failure"}}"#),
        ]);
        let (center, model_id) = configured_custom(endpoint, None);
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: model_id.clone(),
                connection_id: "custom-local".into(),
                model_id: "test-model".into(),
                alias: "Vision warning".into(),
                enabled: true,
                capabilities: vec!["text_chat".into(), "structured_json".into(), "vision".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();

        let result = ModelGateway::new(center.clone()).test_model(&model_id).unwrap();
        assert!(!result.ok);
        assert!(result
            .capabilities
            .iter()
            .any(|check| check.capability == "text_chat" && check.status == "ready"));
        assert!(result
            .capabilities
            .iter()
            .any(|check| check.capability == "structured_json" && check.status == "ready"));
        let vision = result
            .capabilities
            .iter()
            .find(|check| check.capability == "vision")
            .unwrap();
        assert_eq!(vision.status, "error");
        assert_eq!(vision.error_code.as_deref(), Some("MODEL_PROVIDER_UNAVAILABLE"));
        let checks = center.list_models().unwrap()[0].capability_checks.clone();
        assert!(checks
            .iter()
            .any(|check| check.capability == "text_chat" && check.status == "ready"));
        assert!(checks
            .iter()
            .any(|check| check.capability == "vision" && check.status == "error"));
    }

    #[test]
    fn model_test_persists_retry_after_for_text_failure() {
        let endpoint = response_server_with_retry_after(
            "429 Too Many Requests",
            r#"{"error":{"message":"rate limit"}}"#,
            1,
            Some("17"),
        );
        let (center, model_id) = configured_custom(endpoint, None);
        let error = ModelGateway::new(center.clone()).test_model(&model_id).unwrap_err();
        assert_eq!(error.code, "MODEL_RATE_LIMITED");
        let checks = center.list_models().unwrap()[0].capability_checks.clone();
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].capability, "text_chat");
        assert_eq!(checks[0].status, "error");
        assert_eq!(checks[0].retry_after_seconds, Some(17));
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
    fn custom_connection_probe_works_without_a_model() {
        let endpoint = one_response_server("200 OK", r#"{"data":[]}"#);
        let center = center();
        center
            .save_connection(SaveConnectionInput {
                connection_id: "custom-probe".into(),
                provider_id: "custom".into(),
                display_name: "Custom probe".into(),
                region: "global".into(),
                endpoint,
                enabled: true,
            })
            .unwrap();

        let result = ModelGateway::new(center.clone())
            .test_connection("custom-probe", None)
            .unwrap();
        assert!(result.ok);
        assert_eq!(result.meta.model_id, "endpoint-probe");
        assert!(result.message.contains("model inference"));
        assert!(center.list_models().unwrap().is_empty());
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
