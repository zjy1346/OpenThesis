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
use std::net::{IpAddr, ToSocketAddrs};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tauri::{AppHandle, State, Emitter};
use zeroize::{Zeroize, Zeroizing};

const MAX_GATEWAY_REQUEST_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_RESPONSE_BYTES: u64 = 16 * 1024 * 1024;
const MAX_PROVIDER_ERROR_CHARS: usize = 800;

#[derive(Clone)]
pub(crate) struct ModelGateway {
    center: Arc<ModelCenter>,
    events: Option<Arc<GatewayEventSink>>,
    max_output_tokens: Option<u32>,
    probe: bool,
    expected_configuration_version: Option<i64>,
    probe_parent: String,
    cancellation: Option<Arc<AtomicBool>>,
}

impl ModelGateway {
    pub(crate) fn new(center: Arc<ModelCenter>) -> Self {
        Self { center, events: None, max_output_tokens: None, probe: false, expected_configuration_version: None, probe_parent: String::new(), cancellation:None }
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
        if (!provider.supports_discovery || provider.models_path.is_none()) && connection.transport.models_endpoint.is_none() {
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
        let url = connection.transport.url(&connection.endpoint, if connection.protocol == "ollama" { "/api/tags" } else { provider.models_path.unwrap_or("/models") }, true).map_err(GatewayError::configuration)?;
        let request = authorize(client.get(url), secret_ref)?;
        let attempt=self.center.reserve_metadata_probe(connection_id,connection.configuration_version,connection.credential_version).map_err(GatewayError::configuration)?;
        let received = tauri::async_runtime::block_on(async { request.send().await })
            .map_err(GatewayError::delivery)
            .and_then(|response| read_json_response(response,secret_ref)).map_err(|mut error| {
                if matches!(error.http_status,Some(404|405)) {
                    error.code="MODEL_DISCOVERY_UNSUPPORTED".into();
                    error.message="This platform does not expose a model list; enter a model ID manually.".into(); error.retryable=false;
                }
                error
            });
        let _=self.center.finish_probe(&attempt,if received.is_ok(){"response_received"}else{"metadata_error"},&json!({"kind":"metadata","generation_calls":0,"error_code":received.as_ref().err().map(|e|&e.code)}).to_string());
        // Listing authorization may differ from generation. Record it only in
        // the metadata receipt; it must not pause otherwise configured models.
        let payload = received?;
        let ids = if connection.protocol == "ollama" {
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
                capabilities: Vec::new(),
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
        self.check_model_version(&model)?;
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
        if connection.configuration_version != model.connection_configuration_version || connection.credential_version != model.credential_version {
            return Err(GatewayError::new("MODEL_CONFIGURATION_CHANGED", "Connection changed before dispatch; no request sent.", false));
        }
        if !self.probe && connection.status == "unauthorized" {
            return Err(GatewayError::new("MODEL_CONNECTION_AUTH_PAUSED", "This connection is paused after an authentication failure. Update its key or explicitly test it in Model Center.", false));
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
        let timeout = u64::try_from(model.timeout_seconds)
            .unwrap_or(180)
            .clamp(5, 600);
        let client = build_client(&connection, timeout)?;
        let result = if model.protocol == "ollama" {
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
        };
        if let Err(error) = &result {
            if secret_override.is_none() && error.code == "MODEL_UNAUTHORIZED" {
                let _ = self.center.mark_probe_health_for_versions(&model.configured_model_id,"unauthorized",Some(&error.code),(model.configuration_version,model.connection_configuration_version,model.credential_version));
            }
        }
        result
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
        self.check_model_version(&model)?;
        if connection.configuration_version != model.connection_configuration_version || connection.credential_version != model.credential_version { return Err(GatewayError::new("MODEL_CONFIGURATION_CHANGED","Connection changed before dispatch; no request sent.",false)); }
        if !self.probe && connection.status == "unauthorized" { return Err(GatewayError::new("MODEL_CONNECTION_AUTH_PAUSED","Update the connection key or explicitly test it before sending more work.",false)); }
        let secret_ref = secret.as_ref().map(|value| value.as_str());
        let timeout = u64::try_from(model.timeout_seconds)
            .unwrap_or(180)
            .clamp(5, 600);
        let client = build_client(&connection, timeout)?;
        let declared_budget;
        let response = if model.protocol == "ollama" {
            let url = model.transport.url(&model.endpoint, "api/chat", false).map_err(GatewayError::configuration)?;
            let mut payload = json!({
                "model": model.model_id,
                "stream": false,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt, "images": [image_base64]}
                ]
            });
            if let Some(profile) = &model.capability_profile { profile.apply_json(&mut payload,&model.protocol,json_mode); }
            else if json_mode { payload["format"] = json!("json"); }
            declared_budget = self.apply_declared_budget(&model,&mut payload)?;
            self.probe_wire(&model,"vision",||self.post_text_contract(&client, &url, &mut payload, secret_ref, &model.provider_id, model.capability_profile.as_ref()))
                .map_err(|error| self.pause_on_auth_failure(&model, error))?
        } else {
            let url = model.transport.url(&model.endpoint, "chat/completions", false).map_err(GatewayError::configuration)?;
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
            if let Some(profile) = &model.capability_profile { profile.apply_json(&mut payload,&model.protocol,json_mode); }
            else if json_mode && model.provider_id != "custom" {
                payload["response_format"] = json!({"type": "json_object"});
            }
            if let Some(temperature) = model.temperature {
                payload["temperature"] = json!(temperature);
            }
            // A physical call is one attempt. Parameter repairs belong to the caller's budgeted ledger.
            let _ = allow_format_fallback;
            declared_budget = self.apply_declared_budget(&model,&mut payload)?;
            self.probe_wire(&model,"vision",||self.post_text_contract(&client, &url, &mut payload, secret_ref, &model.provider_id, model.capability_profile.as_ref()))
                .map_err(|error| self.pause_on_auth_failure(&model, error))?
        };
        let (content, finish_reason) = if model.protocol == "ollama" {
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
        let mut result = self.result(&model, content, finish_reason);
        result.meta.apply_usage(&response);
        if let Some((tokens,field,semantics)) = declared_budget { result.meta.output_budget_tokens=Some(tokens); result.meta.output_budget_wire_field=Some(field); result.meta.output_budget_semantics=Some(semantics); }
        Ok(result)
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
        let url = model.transport.url(&model.endpoint, "chat/completions", false).map_err(GatewayError::configuration)?;
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
        if let Some(profile) = &model.capability_profile { profile.apply_json(&mut payload,&model.protocol,json_mode); }
        else if json_mode && model.provider_id != "custom" {
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
        let declared_budget = self.apply_declared_budget(model,&mut payload)?;
        let _ = allow_format_fallback;
        let response = self.probe_wire(model,"text",||self.post_text_contract(client, &url, &mut payload, secret, &model.provider_id, model.capability_profile.as_ref()))?;
        let (content, finish_reason) = parse_openai_compatible_message(
            &response,
            "The provider response did not contain message.content.",
        )?;
        let mut result = self.result(model, content, finish_reason);
        result.meta.apply_usage(&response);
        apply_output_budget_receipt(&mut result.meta, output_budget_support);
        if let Some((tokens,field,semantics)) = declared_budget { result.meta.output_budget_tokens=Some(tokens); result.meta.output_budget_wire_field=Some(field); result.meta.output_budget_semantics=Some(semantics); }
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
        let url = model.transport.url(&model.endpoint, "api/chat", false).map_err(GatewayError::configuration)?;
        let mut payload = json!({
            "model": model.model_id,
            "stream": false,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        });
        if let Some(profile) = &model.capability_profile { profile.apply_json(&mut payload,&model.protocol,json_mode); }
        else if json_mode {
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
        let declared_budget = self.apply_declared_budget(model,&mut payload)?;
        let response = self.probe_wire(model,"text",||self.post_text_contract(client, &url, &mut payload, secret, &model.provider_id, model.capability_profile.as_ref()))?;
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
        if let Some((tokens,field,semantics)) = declared_budget { result.meta.output_budget_tokens=Some(tokens); result.meta.output_budget_wire_field=Some(field); result.meta.output_budget_semantics=Some(semantics); }
        Ok(result)
    }

    fn pause_on_auth_failure(&self, model: &ConfiguredModelRuntime, error: GatewayError) -> GatewayError {
        if error.code == "MODEL_UNAUTHORIZED" {
            let _ = self.center.mark_probe_health_for_versions(
                &model.configured_model_id, "unauthorized", Some(&error.code),
                (model.configuration_version, model.connection_configuration_version, model.credential_version),
            );
        }
        error
    }

    fn apply_declared_budget(&self, model: &ConfiguredModelRuntime, payload: &mut Value) -> Result<Option<(u32,String,String)>,GatewayError> {
        let Some(profile) = &model.capability_profile else {
            let known_budget = provider_registry::apply_output_token_budget(
                &model.provider_id, &model.model_id, &mut json!({}), self.max_output_tokens.unwrap_or(1),
            );
            if matches!(known_budget, provider_registry::OutputTokenBudgetSupport::Unsupported) {
                return Err(GatewayError::new("MODEL_OUTPUT_BUDGET_POLICY_REQUIRED","Declare an output parameter or acknowledge the absence of a verified hard output/cost cap in this connection's settings.",false));
            }
            return Ok(None);
        };
        profile.validate(&model.protocol).map_err(GatewayError::configuration)?;
        // A user declaration is necessary for unknown compatible contracts, never inferred from a model ID.
        let requested = self.max_output_tokens.or(profile.output_limit).unwrap_or(8192);
        // A declaration replaces, rather than adds to, preset budget keys.
        for key in ["max_tokens","max_completion_tokens"] { payload.as_object_mut().expect("gateway payload").remove(key); }
        profile.apply_budget(payload,requested).map_err(|_| GatewayError::new("MODEL_OUTPUT_BUDGET_POLICY_REQUIRED","Set this connection's output parameter or explicitly acknowledge that output cost cannot be hard-capped.",false))
    }
    fn check_model_version(&self, model: &ConfiguredModelRuntime) -> Result<(),GatewayError> {
        if self.expected_configuration_version.is_some_and(|version| version != model.configuration_version) {
            return Err(GatewayError::new("MODEL_CONFIGURATION_CHANGED","Model connection or capabilities changed since this research was configured; select the updated configuration before sending.",false));
        }
        Ok(())
    }
    fn as_probe(&self) -> Self {
        let mut probe=self.clone(); probe.probe=true; probe.max_output_tokens=Some(64);
        probe.probe_parent=format!("probe:{}:{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d|d.as_nanos()).unwrap_or_default());
        probe
    }
    fn probe_wire(&self, model: &ConfiguredModelRuntime, operation: &str, send: impl FnOnce()->Result<Value,GatewayError>) -> Result<Value,GatewayError> {
        if !self.probe { return send(); }
        let attempt=self.center.reserve_probe(&self.probe_parent,model,operation).map_err(GatewayError::configuration)?;
        let result=send();
        let (outcome,receipt)=match &result {
            Ok(response)=>{let mut meta=self.result(model,String::new(),None).meta; meta.apply_usage(response); ("response_received",json!({"attempt_id":attempt,"parent_request_id":self.probe_parent,"metadata":meta}))},
            Err(error)=>(if error.http_status.is_some() {"rejected"} else {"remote_unknown"},json!({"attempt_id":attempt,"parent_request_id":self.probe_parent,"error_code":error.code,"http_status":error.http_status})),
        };
        // Preserve a received result even if local audit completion fails. The durable reservation remains unknown, never free/zero.
        let _=self.center.finish_probe(&attempt,outcome,&receipt.to_string());
        result
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
                usage_contract: model.capability_profile.as_ref().map(|p| p.usage.clone()),
                http_status: None,
                provider_request_id: None,
                reasoning_in_output: None,
                transport_observation: None,
                usage_source: if model.capability_profile.as_ref().is_some_and(|p| p.upstream != "direct") || model.provider_id == "custom" { "endpoint_reported_upstream_unverified".into() } else { "provider_response".into() },
            },
            content,
        }
    }

    fn post_text(
        &self, client: &Client, url: &reqwest::Url, payload: &mut Value,
        secret: Option<&str>, provider_id: &str,
    ) -> Result<Value, GatewayError> {
        self.post_text_contract(client,url,payload,secret,provider_id,None)
    }
    fn post_text_contract(
        &self, client: &Client, url: &reqwest::Url, payload: &mut Value,
        secret: Option<&str>, provider_id: &str, profile: Option<&crate::connection_contract::CapabilityProfile>,
    ) -> Result<Value, GatewayError> {
        let events = self.events.as_ref();
        let protocol = profile.map(|p| p.stream.as_str()).filter(|p| *p != "none").or_else(|| if profile.is_none() { provider_registry::text_stream_protocol(provider_id) } else { None });
        if protocol.is_some() {
            payload["stream"] = json!(true);
            // These adapters explicitly document include_usage. Do not send it
            // to an arbitrary endpoint merely labelled OpenAI-compatible.
            if profile.map(|p| p.include_usage).unwrap_or_else(|| provider_registry::stream_requests_usage(provider_id)) {
                payload["stream_options"] = json!({"include_usage": true});
            }
        }
        if let Some(events)=events { events.activity("connecting", 0); }
        let request = authorize(client.post(url.clone()).json(payload), secret)?;
        if let Some(events)=events { events.activity("waiting_response",0); }
        let mut response = self.await_provider(||request.send())?;
        if let Some(events)=events { events.activity("connected", 0); }
        if !response.status().is_success() {
            return self.await_gateway(read_json_response_async(response, secret));
        }
        let status = response.status().as_u16();
        let request_id = ["x-request-id", "request-id"].iter().find_map(|name|
            response.headers().get(*name).and_then(|v| v.to_str().ok())
                .filter(|v| v.len() <= 256 && v.chars().all(|c| c.is_ascii_alphanumeric() || "_.:-".contains(c))).map(str::to_string));
        let annotate = |mut value: Value| {
            if let Some(object) = value.as_object_mut() {
                object.insert("_openthesis_http_status".into(), json!(status));
                object.insert("_openthesis_provider_request_id".into(), json!(request_id));
            }
            value
        };
        let content_type = response.headers().get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok()).unwrap_or("").to_ascii_lowercase();
        let format = match content_type.as_str() {
            value if value.contains("text/event-stream") => StreamFormat::Sse,
            value if value.contains("ndjson") || value.contains("jsonl") => StreamFormat::Ndjson,
            _ => {
                let mut value = self.await_gateway(read_json_response_async(response, secret))?;
                if let Some(object) = value.as_object_mut() { object.insert("_openthesis_transport_observation".into(),json!(if protocol.is_some() {"declared_stream_returned_non_stream_body"} else {"non_streaming_response"})); }
                return Ok(annotate(value));
            }
        };
        let mut decoder = ProviderStreamDecoder::new(format);
        loop {
            let previous_length = decoder.partial_content().len();
            let chunk = match self.await_provider(||response.chunk()) {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(error) => {
                    if let Some(events)=events { events.preserve_partial(decoder.partial_content()); }
                    if error.code=="MODEL_CALL_CANCELLED" { return Err(error); }
                    return Err(GatewayError::new(
                        if error.code=="MODEL_SUBMISSION_UNKNOWN" { "MODEL_TIMEOUT" } else { "MODEL_STREAM_INTERRUPTED" },
                        "The remote stream did not complete within this request; submission outcome is unknown.", false));
                }
            };
            match decoder.push(&chunk) {
                Ok(activity) => {
                    let added = &decoder.partial_content()[previous_length..];
                    if let Some(events)=events {
                        if !added.is_empty() { events.frame("content", json!({"text": added})); }
                        for item in activity { events.activity(item.kind, item.bytes); }
                    }
                },
                Err(code) => {
                    if let Some(events)=events { events.preserve_partial(decoder.partial_content()); }
                    return Err(GatewayError::new(code, "The remote stream could not be completed.", false));
                }
            }
            if decoder.is_complete() { break; }
        }
        let partial = decoder.partial_content().to_string();
        decoder.finish().map(annotate).map_err(|code| {
            if let Some(events)=events { events.preserve_partial(&partial); }
            GatewayError::new(code, "The remote stream has no confirmed terminal record.", false)
        })
    }

    fn await_provider<T,F:std::future::Future<Output=Result<T,reqwest::Error>>>(&self, make:impl FnOnce()->F) -> Result<T,GatewayError> {
        // reqwest creates timeout timers in send(), so construct its future
        // inside the same runtime used to poll/cancel it.
        self.await_gateway(async {make().await.map_err(GatewayError::delivery)})
    }

    fn await_gateway<T>(&self, future:impl std::future::Future<Output=Result<T,GatewayError>>) -> Result<T,GatewayError> {
        tauri::async_runtime::block_on(async {
            if let Some(cancel)=&self.cancellation {
                tokio::select! {
                    result=future => result,
                    _=async { while !cancel.load(Ordering::Acquire) { tokio::time::sleep(Duration::from_millis(75)).await; } } => {
                        let mut error=GatewayError::new("MODEL_CALL_CANCELLED","Stopped waiting; the remote result may still be unknown. This request will not be resent.",false);
                        error.delivery_state=Some("unknown".into()); Err(error)
                    }
                }
            } else { future.await }
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
            let probe = self.as_probe();
            probe.generate_with_secret(
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
            .mark_connection_status_for_versions(connection_id, "ready", None, summary.configuration_version, summary.credential_version);
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
                    .mark_connection_status_for_versions(connection_id, "disabled", Some(&error.code), connection.configuration_version, connection.credential_version);
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
            // Metadata access is not a successful generation test.
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
                    credential_version: connection.credential_version,
                    connection_configuration_version: connection.configuration_version,
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
                    usage_contract: connection.capability_profile.as_ref().map(|p| p.usage.clone()),
                    transport_observation: None,
                    usage_source: "endpoint_metadata_no_generation".into(),
                    http_status: None,
                    provider_request_id: None,
                    reasoning_in_output: None,
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
            transport: connection.transport.clone(),
            protocol: connection.protocol.clone(), capability_profile: connection.capability_profile.clone(),
            configured_model_id: format!("connection-test.{connection_id}"),
            connection_id: connection_id.to_string(),
            provider_id: connection.provider_id.clone(),
            endpoint: connection.endpoint.clone(),
            credential_version: connection.credential_version,
            connection_enabled: true,
            connection_configuration_version: connection.configuration_version,
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
        let mut probe = self.as_probe();
        probe.expected_configuration_version = Some(model.configuration_version);
        let result = if connection.protocol == "ollama" {
            probe.generate_ollama(
                &client,
                &model,
                secret_ref,
                "You are a connection test. Return JSON only.",
                r#"{"task":"Return {\"ok\":true}."}"#,
                false,
            )
        } else {
            probe.generate_openai_compatible(
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
                    .mark_connection_status_for_versions(connection_id, "ready", None, connection.configuration_version, connection.credential_version);
                Ok(GatewayTestResult {
                    ok: true,
                    message: format!("Connection test succeeded with {}.", model_id),
                    meta: result.meta,
                    capabilities: Vec::new(),
                    warnings: Vec::new(),
                })
            }
            Err(error) => {
                let _ = self.center.mark_connection_status_for_versions(
                    connection_id,
                    health_status_for_error(&error.code),
                    Some(&error.code),
                    connection.configuration_version, connection.credential_version,
                );
                Err(error)
            }
        }
    }
    pub(crate) fn test_model(
        &self,
        configured_model_id: &str,
    ) -> Result<GatewayTestResult, GatewayError> {
        self.test_model_capabilities(configured_model_id, false)
    }
    pub(crate) fn test_model_capabilities(&self, configured_model_id: &str, test_vision: bool) -> Result<GatewayTestResult,GatewayError> {
        let model = self
            .center
            .configured_model_runtime(configured_model_id)
            .map_err(GatewayError::configuration)?;
        let mut capabilities = Vec::new();
        let mut warnings = Vec::new();
        let mut auth_error = None;
        let requested_versions = (model.configuration_version, model.connection_configuration_version,
                                  model.credential_version);
        let mut probe = self.as_probe();
        probe.expected_configuration_version = Some(model.configuration_version);
        let text_result = match probe.generate_once(
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
                if error.code=="MODEL_CALL_CANCELLED" { return Err(error); }
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

        if test_vision && model
            .capabilities
            .iter()
            .any(|capability| capability == "vision")
        {
            const TEST_PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
            match probe.generate_vision(
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
                    if error.code == "MODEL_UNAUTHORIZED" { auth_error = Some(error.code.clone()); }
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
        // A successful text probe is not permission to undo a later vision 401/403.
        // Keep the text observation, but pause new work on the rejected connection.
        self.center.mark_probe_health_for_versions(configured_model_id,
            if auth_error.is_some() { "unauthorized" } else { "ready" }, auth_error.as_deref(),
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
    #[serde(default)]
    pub http_status: Option<u16>,
    #[serde(default)]
    pub provider_request_id: Option<String>,
    #[serde(default)]
    pub reasoning_in_output: Option<bool>,
    #[serde(default)]
    pub transport_observation: Option<String>,
    #[serde(default)]
    pub usage_contract: Option<String>,
    #[serde(default)]
    pub usage_source: String,
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
        self.http_status = response.get("_openthesis_http_status").and_then(Value::as_u64).and_then(|v| u16::try_from(v).ok());
        self.provider_request_id = response.get("_openthesis_provider_request_id").and_then(Value::as_str).map(str::to_string);
        self.transport_observation = response.get("_openthesis_transport_observation").and_then(Value::as_str).filter(|s| ["declared_stream_returned_non_stream_body","non_streaming_response"].contains(s)).map(str::to_string);
        // Missing is unknown, not zero. A cache miss is not a cache write.
        let integer = |paths: &[&str]| paths.iter().find_map(|path|
            response.pointer(path).and_then(Value::as_u64));
        let contract = self.usage_contract.as_deref().unwrap_or(&self.provider_id);
        let ollama = contract == "ollama";
        self.input_tokens = integer(if ollama { &["/prompt_eval_count"] } else { &["/usage/prompt_tokens"] });
        self.output_tokens = integer(if ollama { &["/eval_count"] } else { &["/usage/completion_tokens"] });
        self.reasoning_tokens = match contract {
            "openai" | "deepseek" | "qwen" | "openrouter" => integer(&["/usage/completion_tokens_details/reasoning_tokens"]),
            _ => None,
        };
        // Only a separately reported text counter proves visible-answer usage.
        // completion - reasoning is not safe with audio/prediction categories.
        self.answer_tokens = match contract {
            "openai" => integer(&["/usage/completion_tokens_details/text_tokens"]),
            _ => None,
        };
        self.cache_read_input_tokens = match contract {
            "deepseek" => integer(&["/usage/prompt_cache_hit_tokens"]),
            "ollama" => integer(&["/prompt_eval_cached_count"]),
            "qwen" | "kimi_cn" | "kimi_global" => integer(&["/usage/prompt_tokens_details/cached_tokens", "/usage/cached_tokens"]),
            "openai" | "glm" | "openrouter" => integer(&["/usage/prompt_tokens_details/cached_tokens"]),
            _ => None,
        };
        self.cache_write_input_tokens = match contract {
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delivery_state: Option<String>,
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
            delivery_state: if matches!(code,"MODEL_CONFIGURATION_CHANGED"|"MODEL_OUTPUT_BUDGET_POLICY_REQUIRED"|"MODEL_CONNECTION_AUTH_PAUSED") {Some("not_sent".into())} else {None},
            code: code.to_string(),
            message: message.into(),
            retryable,
            retry_after_seconds: None,
            http_status: None,
        }
    }

    fn configuration(message: impl Into<String>) -> Self {
        let message = message.into();
        let code = if ["MODEL_ENDPOINT_FORBIDDEN_TARGET","MODEL_PROXY_INVALID","MODEL_PROXY_PAC_UNSUPPORTED","MODEL_MODELS_ENDPOINT_ORIGIN","MODEL_TRANSPORT_INVALID","MODEL_TEST_BUSY","MODEL_TEST_ID_INVALID","MODEL_ENDPOINT_MODE_INVALID"].contains(&message.as_str()) { message.as_str() }
        else if message == "MODEL_CREDENTIAL_MISSING"
            || message == "stored credential was not found"
        {
            "MODEL_CREDENTIAL_MISSING"
        } else {
            "MODEL_CONFIGURATION_ERROR"
        }.to_string();
        let safe_message = if code == "MODEL_CREDENTIAL_MISSING" {
            "The saved credential is unavailable. Reconfigure this connection in Model Center."
                .to_string()
        } else {
            message
        };
        let mut error=Self::new(&code, safe_message, false);
        error.delivery_state=Some("not_sent".into()); error
    }

    fn delivery(error: reqwest::Error) -> Self {
        let not_sent=error.is_connect();
        use std::error::Error;
        let mut chain=error.to_string().to_ascii_lowercase(); let mut source=error.source();
        while let Some(item)=source { chain.push_str(&item.to_string().to_ascii_lowercase()); source=item.source(); }
        let code=if !not_sent {"MODEL_SUBMISSION_UNKNOWN"}
            else if chain.contains("model_endpoint_forbidden_target") {"MODEL_ENDPOINT_FORBIDDEN_TARGET"}
            else if chain.contains("certificate") || chain.contains("tls") || chain.contains("ssl") {"MODEL_TLS_ERROR"}
            else if chain.contains("proxy") || chain.contains("tunnel") {"MODEL_PROXY_ERROR"}
            else if chain.contains("dns") || chain.contains("resolve") || chain.contains("no such host") {"MODEL_DNS_ERROR"}
            else {"MODEL_NETWORK_ERROR"};
        let mut result=Self::new(code,if not_sent {"Connection could not be established; inspect the target and network settings. No request was sent."} else {"The request delivery outcome is unknown; it will not be resent automatically."},not_sent && code!="MODEL_ENDPOINT_FORBIDDEN_TARGET");
        result.delivery_state=Some(if not_sent {"not_sent"} else {"unknown"}.into());
        result
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
    crate::connection_contract::request_url(base,path).map_err(|_| {
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
    connection.transport.validate(&connection.endpoint).map_err(GatewayError::configuration)?;
    let mut builder = Client::builder()
        .redirect(Policy::none())
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(timeout_seconds.clamp(5, 600)))
        .dns_resolver(Arc::new(ModelTargetResolver));
    match connection.transport.network_mode.as_str() {
        "direct" => builder=builder.no_proxy(),
        "proxy" => builder=builder.no_proxy().proxy(reqwest::Proxy::all(connection.transport.proxy_url.as_deref().unwrap_or_default())
            .map_err(|_| GatewayError::configuration("MODEL_PROXY_INVALID"))?),
        _ => {
            // reqwest's system-proxy feature reads environment and Windows
            // static settings. It does not execute PAC/WPAD scripts.
            let loopback=reqwest::Url::parse(&connection.endpoint).ok().is_some_and(|url| is_loopback_host(url.host_str().unwrap_or_default()));
            if !loopback && system_proxy_requires_pac() && !has_environment_proxy(&connection.endpoint) {
                return Err(GatewayError::configuration("MODEL_PROXY_PAC_UNSUPPORTED"));
            }
        }
    }

    builder.build().map_err(|_| {
        GatewayError::new(
            "MODEL_CLIENT_ERROR",
            "The secure model client could not be created.",
            false,
        )
    })
}

/// Resolution happens inside the selected reqwest transport, never as a
/// synchronous public-IP preflight. Private/Fake-IP targets remain allowed.
#[derive(Debug)]
struct ModelTargetResolver;
impl reqwest::dns::Resolve for ModelTargetResolver {
    fn resolve(&self,name:reqwest::dns::Name) -> reqwest::dns::Resolving {
        let host=name.as_str().to_owned();
        Box::pin(async move {
            let addresses=tokio::task::spawn_blocking(move || (host.as_str(),0).to_socket_addrs().map(|values| values.collect::<Vec<_>>())).await??;
            if addresses.is_empty() || addresses.iter().any(|address| !provider_registry::allowed_model_ip(address.ip())) {
                return Err(std::io::Error::other("MODEL_ENDPOINT_FORBIDDEN_TARGET").into());
            }
            Ok(Box::new(addresses.into_iter()) as reqwest::dns::Addrs)
        })
    }
}

fn has_environment_proxy(endpoint: &str) -> bool {
    let scheme=reqwest::Url::parse(endpoint).ok().map(|url| url.scheme().to_string()).unwrap_or_default();
    let keys=if scheme=="https" { ["HTTPS_PROXY","https_proxy","ALL_PROXY","all_proxy"] } else { ["HTTP_PROXY","http_proxy","ALL_PROXY","all_proxy"] };
    keys.iter()
        .any(|key| std::env::var(key).is_ok_and(|value| !value.trim().is_empty()))
}

#[cfg(windows)]
fn system_proxy_requires_pac() -> bool {
    use windows_sys::Win32::{Foundation::GlobalFree, Networking::WinHttp::{WinHttpGetIEProxyConfigForCurrentUser,WINHTTP_CURRENT_USER_IE_PROXY_CONFIG}};
    let mut config=WINHTTP_CURRENT_USER_IE_PROXY_CONFIG::default();
    unsafe {
        if WinHttpGetIEProxyConfigForCurrentUser(&mut config)==0 { return false; }
        let automatic=!config.lpszAutoConfigUrl.is_null() && *config.lpszAutoConfigUrl!=0;
        let has_static=!config.lpszProxy.is_null() && *config.lpszProxy!=0;
        for pointer in [config.lpszAutoConfigUrl,config.lpszProxy,config.lpszProxyBypass] {
            if !pointer.is_null() { GlobalFree(pointer.cast()); }
        }
        automatic && !has_static
    }
}
#[cfg(not(windows))]
fn system_proxy_requires_pac() -> bool { false }

fn is_loopback_host(host: &str) -> bool {
    matches!(host, "localhost" | "127.0.0.1" | "::1" | "[::1]")
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
            404 if lowered_detail.contains("model") => "MODEL_MODEL_NOT_FOUND",
            400 | 422 if lowered_detail.contains("stream") && ["unsupported","not support","unknown parameter","not allowed"].iter().any(|marker|lowered_detail.contains(marker)) => "MODEL_STREAM_UNSUPPORTED",
            400 | 422 if ["max_tokens","max_completion_tokens","num_predict"].iter().any(|field|lowered_detail.contains(field)) && ["unsupported","not support","unknown parameter","not allowed"].iter().any(|marker|lowered_detail.contains(marker)) => "MODEL_OUTPUT_PARAMETER_UNSUPPORTED",
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
    observer: Option<Arc<dyn Fn(&Value)+Send+Sync>>,
    finished: AtomicBool,
}

impl GatewayEventSink {
    fn new(context: GatewayEventContext) -> Result<Self, GatewayError> {
        if context.protocol != "model-events-v1" ||
            [&context.request_id, &context.run_id, &context.stage_id, &context.attempt_id]
                .iter().any(|value| value.is_empty() || value.len() > 256 || value.chars().any(char::is_control)) {
            return Err(GatewayError::new("MODEL_GATEWAY_PROTOCOL_ERROR", "Invalid event identity.", false));
        }
        Ok(Self { context, seq: Mutex::new(0), partial: Mutex::new(None), observer:None, finished:AtomicBool::new(false) })
    }

    fn frame(&self, kind: &str, data: Value) {
        let Ok(mut seq) = self.seq.lock() else { return; };
        if self.finished.load(Ordering::Acquire) { return; }
        if matches!(kind,"error"|"result") { self.finished.store(true,Ordering::Release); }
        *seq += 1;
        let frame = json!({"protocol": "model-events-v1", "context": self.context,
                           "seq": *seq, "kind": kind, "data": data});
        if let Some(observer)=&self.observer { observer(&frame); return; }
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
    #[serde(default)]
    expected_configuration_version: Option<i64>,
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
    gateway.expected_configuration_version = request.expected_configuration_version;
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
    // Listing proves only metadata access; 404/empty/stale results do not revoke text capabilities.
    result
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
    test_vision: Option<bool>,
    operation_id: Option<String>,
) -> Result<GatewayTestResult, GatewayError> {
    let center = state.get(&app).map_err(GatewayError::configuration)?;
    let model=center.configured_model_runtime(&configured_model_id).map_err(GatewayError::configuration)?;
    let operation_id=operation_id.unwrap_or_else(||format!("test:{}",std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|v|v.as_nanos()).unwrap_or_default()));
    let cancel=state.begin_test(&model.connection_id,&operation_id).map_err(GatewayError::configuration)?;
    let mut sink=GatewayEventSink::new(GatewayEventContext {protocol:"model-events-v1".into(),request_id:operation_id.clone(),run_id:configured_model_id.clone(),stage_id:"model-test".into(),generation:model.configuration_version.max(0) as u64,attempt_id:operation_id.clone()})?;
    sink.observer=Some(Arc::new(move |frame| {let _=app.emit("openthesis-model-test",frame);}));
    let events=Arc::new(sink); let worker_events=events.clone();
    events.activity("queued",0);
    let result=tauri::async_runtime::spawn_blocking(move || {
        let mut gateway=ModelGateway::new(center); gateway.events=Some(worker_events); gateway.cancellation=Some(cancel);
        gateway.test_model_capabilities(&configured_model_id,test_vision.unwrap_or(false))
    }).await.map_err(|_| GatewayError::new("MODEL_GATEWAY_UNAVAILABLE","Model test worker stopped.",false)).and_then(|result|result);
    state.finish_test(&model.connection_id,&operation_id);
    match &result {Ok(value)=>events.frame("result",json!({"ok":value.ok})),Err(error)=>events.frame("error",json!({"code":error.code}))};
    result
}

#[tauri::command]
pub async fn model_gateway_cancel_test(state:State<'_,ModelCenterState>,operation_id:String) -> Result<(),GatewayError> {
    state.cancel_test(&operation_id); Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn metadata_auth_failure_does_not_pause_generation_321() {
        let (center,_)=configured_custom(one_response_server("403 Forbidden",r#"{"error":{"message":"models listing permission unavailable"}}"#),None);
        assert_eq!(ModelGateway::new(center.clone()).discover_models("custom-local").unwrap_err().http_status,Some(403));
        assert_ne!(center.connection_runtime("custom-local").unwrap().status,"unauthorized");
    }
    #[test]
    fn cancellation_interrupts_nonstream_body_without_retry_or_capability_failure_321() {
        let (endpoint,release,server)=stalled_error_response_server("200 OK",None);
        let (center,id)=configured_custom(endpoint,None);
        let previous=center.list_models().unwrap()[0].health_status.clone();
        let cancel=Arc::new(AtomicBool::new(false)); let signal=cancel.clone();
        let worker=std::thread::spawn(move || {std::thread::sleep(Duration::from_millis(200));signal.store(true,Ordering::Release);});
        let mut gateway=ModelGateway::new(center.clone()); gateway.cancellation=Some(cancel);
        let started=Instant::now(); let result=gateway.test_model_capabilities(&id,false);
        let _=release.send(()); server.join().unwrap(); worker.join().unwrap();
        let error=result.unwrap_err(); assert_eq!(error.code,"MODEL_CALL_CANCELLED");
        assert_eq!(error.delivery_state.as_deref(),Some("unknown")); assert!(!error.retryable);
        assert!(started.elapsed()<Duration::from_secs(2));
        assert_eq!(center.list_models().unwrap()[0].health_status,previous);
    }
    #[test]
    fn configured_client_creation_does_not_resolve_or_pin_customer_dns_321() {
        let (center, _) = configured_custom("https://customer-model.invalid/v1".into(), None);
        let connection = center.connection_runtime("custom-local").unwrap();
        assert!(build_client(&connection, 10).is_ok(), "creating a configured client must not require public DNS");
    }

    #[test]
    fn custom_stream_without_observer_has_the_same_single_wire_contract_321() {
        let body = format!("data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"index":0,"delta":{"content":"complete answer"},"finish_reason":"stop"}]}));
        let (url, receiver) = recorded_wire_server("text/event-stream", body);
        let gateway = ModelGateway::new(center());
        let mut payload = json!({"model":"private/exact","messages":[]});
        let profile = crate::connection_contract::CapabilityProfile { stream:"sse".into(), ..Default::default() };
        let result = gateway.post_text_contract(&Client::new(), &url, &mut payload, None, "custom", Some(&profile));
        assert!(result.is_ok(), "the optional event observer must not disable stream decoding: {result:?}");
        assert_eq!(result.unwrap()["choices"][0]["message"]["content"], "complete answer");
        assert_eq!(receiver.recv_timeout(Duration::from_secs(2)).unwrap()["stream"], true);
    }
    #[test]
    fn full_completion_url_is_normalized_before_metadata_or_generation_320() {
        assert_eq!(endpoint_url("https://proxy.example/team/v1/chat/completions/", "chat/completions").unwrap().as_str(), "https://proxy.example/team/v1/chat/completions");
        assert_eq!(endpoint_url("https://proxy.example/team/v1/chat/completions/", "/models").unwrap().as_str(), "https://proxy.example/team/v1/models");
    }
    #[test]
    fn custom_sse_usage_and_output_contract_are_connection_scoped_320() {
        let body = format!("data: {}\n\ndata: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"index":0,"delta":{"content":"完整材料"},"finish_reason":"stop"}]}),json!({"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":80}}}));
        let (url,receiver)=recorded_wire_server("text/event-stream",body);
        let endpoint=url.origin().ascii_serialization();
        let (center,id)=configured_custom(endpoint.clone(),None);
        center.save_connection(SaveConnectionInput {transport:None,connection_id:"custom-local".into(),provider_id:"custom".into(),display_name:"Proxy".into(),region:"custom".into(),endpoint,enabled:true,protocol:Some("chat_completions".into()),capability_profile:Some(crate::connection_contract::CapabilityProfile {stream:"sse".into(),include_usage:true,usage:"openai".into(),output_parameter:"max_tokens".into(),output_limit:Some(2000),output_semantics:"visible_output_tokens_only".into(),upstream:"proxy".into(),..Default::default()})}).unwrap();
        let mut gateway=ModelGateway::new(center).with_output_token_budget(Some(512));
        gateway.events=Some(Arc::new(GatewayEventSink::new(GatewayEventContext{protocol:"model-events-v1".into(),request_id:"test320".into(),run_id:"isolation".into(),stage_id:"financial-analyst".into(),generation:1,attempt_id:"one".into()}).unwrap()));
        let result=gateway.generate_once(&id,"full system","full source",true).unwrap();
        let wire=receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(wire["stream"],true); assert_eq!(wire["stream_options"]["include_usage"],true);
        assert_eq!(wire["max_tokens"],512); assert!(wire.get("max_completion_tokens").is_none()); assert!(wire.get("response_format").is_none());
        assert_eq!(wire["messages"][1]["content"],"full source");
        assert_eq!(result.content,"完整材料"); assert_eq!(result.meta.cache_read_input_tokens,Some(80));
        assert_eq!(result.meta.usage_source,"endpoint_reported_upstream_unverified");
        assert_eq!(result.meta.output_budget_semantics.as_deref(),Some("visible_output_tokens_only"));
    }
    #[test]
    fn connection_auth_failure_pauses_later_work_without_another_wire_320() {
        let (center,id)=configured_custom(one_response_server("401 Unauthorized",r#"{"error":{"message":"unauthorized"}}"#),None);
        let gateway=ModelGateway::new(center);
        assert_eq!(gateway.generate_once(&id,"system","source",false).unwrap_err().code,"MODEL_UNAUTHORIZED");
        assert_eq!(gateway.generate_once(&id,"system","source",false).unwrap_err().code,"MODEL_CONNECTION_AUTH_PAUSED");
    }
    #[test]
    fn text_test_does_not_implicitly_send_a_declared_image_320() {
        let (center,id)=configured_vision(one_response_server("200 OK",r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#));
        let result=ModelGateway::new(center.clone()).test_model(&id).unwrap();
        assert!(result.ok); assert!(!result.capabilities.iter().any(|c|c.capability=="vision"));
        assert!(!center.list_models().unwrap()[0].capability_checks.iter().any(|c|c.capability=="vision"));
    }
    #[test]
    fn directory_discovery_does_not_certify_structured_json_320() {
        let (center,_)=configured_custom(one_response_server("200 OK",r#"{"data":[{"id":"Private/Case-ID"}]}"#),None);
        let discovered=ModelGateway::new(center).discover_models("custom-local").unwrap();
        assert_eq!(discovered[0].model_id,"Private/Case-ID"); assert!(discovered[0].capabilities.is_empty());
    }
    #[test]
    fn changed_configuration_and_unknown_budget_are_rejected_before_wire_320() {
        let (center,id)=configured_custom("http://127.0.0.1:9".into(),None);
        let mut gateway=ModelGateway::new(center.clone()); gateway.expected_configuration_version=Some(999);
        assert_eq!(gateway.generate_once(&id,"system","source",true).unwrap_err().code,"MODEL_CONFIGURATION_CHANGED");
        center.save_connection(SaveConnectionInput{transport:None,connection_id:"custom-local".into(),provider_id:"custom".into(),display_name:"Unknown".into(),region:"custom".into(),endpoint:"http://127.0.0.1:9".into(),enabled:true,protocol:Some("chat_completions".into()),capability_profile:Some(Default::default())}).unwrap();
        let error=ModelGateway::new(center).generate_once(&id,"system","source",true).unwrap_err();
        assert_eq!(error.code,"MODEL_OUTPUT_BUDGET_POLICY_REQUIRED"); assert_eq!(error.delivery_state.as_deref(),Some("not_sent"));
    }
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
            meta.usage_contract = None;
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
                transport: None,
                protocol: None, capability_profile: Some(crate::connection_contract::CapabilityProfile {allow_unbounded_output:true,..Default::default()}),
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
            expected_configuration_version: None,
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
        capped_model.capability_profile = None;
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
            if provider != "custom" { model.capability_profile = None; }
            model.model_id = model_id.into();
            model.endpoint = endpoint.to_string();
            let gateway = ModelGateway::new(center.clone()).with_output_token_budget(Some(requested));
            if expected.is_none() {
                model.capability_profile = None;
                let error = gateway.generate_openai_compatible(
                    &Client::new(), &model, None, "system", "evidence", false, false,
                ).unwrap_err();
                assert_eq!(error.code, "MODEL_OUTPUT_BUDGET_POLICY_REQUIRED", "{provider}/{model_id}");
                assert_eq!(error.delivery_state.as_deref(), Some("not_sent"));
                assert!(receiver.try_recv().is_err(), "unknown contract must not send a wire");
                // Explicit consent is a connection policy, not a guessed vendor parameter.
                model.capability_profile = Some(crate::connection_contract::CapabilityProfile {
                    allow_unbounded_output: true, ..Default::default()
                });
            }
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
        ollama_model.capability_profile = None;
        ollama_model.model_id = "qwen3:8b".into();
        ollama_model.endpoint = endpoint.to_string();
        ollama_model.temperature = Some(0.4);
        let gateway = ModelGateway::new(center).with_output_token_budget(Some(456));
        let error = gateway.generate_ollama(
            &Client::new(), &ollama_model, None, "system", "evidence", false,
        ).unwrap_err();
        assert_eq!(error.code, "MODEL_OUTPUT_BUDGET_POLICY_REQUIRED");
        assert!(receiver.try_recv().is_err());
        ollama_model.capability_profile = Some(crate::connection_contract::CapabilityProfile {
            allow_unbounded_output: true, ..Default::default()
        });
        let result = gateway
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
            expected_configuration_version: None,
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

        let result = ModelGateway::new(center.clone()).test_model_capabilities(&model_id,true).unwrap();
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
    fn vision_auth_failure_pauses_connection_even_after_text_test_succeeds_320() {
        let endpoint = sequenced_response_server(&[
            ("200 OK", r#"{"choices":[{"message":{"content":"{\"ok\":true}"},"finish_reason":"stop"}]}"#),
            ("401 Unauthorized", r#"{"error":{"message":"vision access denied"}}"#),
        ]);
        let (center, id) = configured_custom(endpoint, None);
        center.save_model(SaveConfiguredModelInput {
            configured_model_id: id.clone(), connection_id: "custom-local".into(),
            model_id: "test-model".into(), alias: "Vision auth check".into(), enabled: true,
            capabilities: vec!["text_chat".into(), "vision".into()],
            context_window_hint: None, temperature: None, timeout_seconds: None,
        }).unwrap();
        let result = ModelGateway::new(center.clone()).test_model_capabilities(&id, true).unwrap();
        assert!(!result.ok);
        assert!(result.capabilities.iter().any(|c| c.capability == "text_chat" && c.status == "ready"));
        assert_eq!(center.connection_runtime("custom-local").unwrap().status, "unauthorized");
        assert_eq!(ModelGateway::new(center).generate_once(&id, "system", "source", false).unwrap_err().code, "MODEL_CONNECTION_AUTH_PAUSED");
    }

    #[test]
    fn vision_only_auth_failure_pauses_text_on_same_connection_320() {
        let (center, id) = configured_custom(one_response_server("403 Forbidden", r#"{"error":{"message":"denied"}}"#), None);
        center.save_model(SaveConfiguredModelInput {
            configured_model_id: id.clone(), connection_id: "custom-local".into(),
            model_id: "test-model".into(), alias: "Vision auth check".into(), enabled: true,
            capabilities: vec!["text_chat".into(), "vision".into()],
            context_window_hint: None, temperature: None, timeout_seconds: None,
        }).unwrap();
        let gateway = ModelGateway::new(center.clone());
        let png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
        assert_eq!(gateway.generate_vision(&id,"system","source","image/png",png).unwrap_err().code,"MODEL_UNAUTHORIZED");
        assert_eq!(center.connection_runtime("custom-local").unwrap().status,"unauthorized");
        assert_eq!(gateway.generate_once(&id,"system","source",false).unwrap_err().code,"MODEL_CONNECTION_AUTH_PAUSED");
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

        let result = ModelGateway::new(center.clone()).test_model_capabilities(&model_id,true).unwrap();
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
                transport: None,
                protocol: None, capability_profile: None,
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
                transport: None,
                protocol: None, capability_profile: None,
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
            "unknown"
        );
    }

    #[test]
    fn configured_targets_allow_private_and_fake_ip_but_not_metadata_or_nonunicast() {
        for address in ["10.0.0.1", "127.0.0.1", "198.18.0.7", "::1", "fd00::1"] {
            assert!(provider_registry::allowed_model_ip(address.parse().unwrap()));
        }
        for address in ["169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::", "ff02::1"] {
            assert!(!provider_registry::allowed_model_ip(address.parse().unwrap()));
        }
    }
}
