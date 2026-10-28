use serde_json::{json, Map, Value};

const MAX_TOTAL_INPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_VISIBLE_CONTENT_BYTES: usize = 16 * 1024 * 1024;
const MAX_LINE_OR_EVENT_BYTES: usize = 1024 * 1024;

const TOO_LARGE: &str = "MODEL_RESPONSE_TOO_LARGE";
const INVALID_RESPONSE: &str = "MODEL_RESPONSE_INVALID";
const INTERRUPTED: &str = "MODEL_STREAM_INTERRUPTED";
const PROVIDER_ERROR: &str = "MODEL_STREAM_PROVIDER_ERROR";

#[derive(Clone, Copy)]
pub(crate) enum StreamFormat {
    Sse,
    Ndjson,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct StreamActivity {
    pub kind: &'static str,
    pub bytes: usize,
}

pub(crate) struct ProviderStreamDecoder {
    format: StreamFormat,
    line: Vec<u8>,
    total_input_bytes: usize,
    visible_content_bytes: usize,
    content: String,
    refusal: Option<String>,
    finish_reason: Option<String>,
    usage: Option<Value>,
    prompt_eval_count: Option<Value>,
    prompt_eval_cached_count: Option<Value>,
    eval_count: Option<Value>,
    done_reason: Option<Value>,
    completed: bool,
    reasoning_fields: Option<Vec<&'static str>>,
    sse_event_bytes: usize,
    sse_data: Vec<u8>,
    sse_has_data: bool,
}

impl ProviderStreamDecoder {
    pub(crate) fn new(format: StreamFormat) -> Self {
        Self {
            format,
            line: Vec::new(),
            total_input_bytes: 0,
            visible_content_bytes: 0,
            content: String::new(),
            refusal: None,
            finish_reason: None,
            usage: None,
            prompt_eval_count: None,
            prompt_eval_cached_count: None,
            eval_count: None,
            done_reason: None,
            completed: false,
            reasoning_fields: None,
            sse_event_bytes: 0,
            sse_data: Vec::new(),
            sse_has_data: false,
        }
    }

    pub(crate) fn push(&mut self, chunk: &[u8]) -> Result<Vec<StreamActivity>, &'static str> {
        self.total_input_bytes = self
            .total_input_bytes
            .checked_add(chunk.len())
            .filter(|size| *size <= MAX_TOTAL_INPUT_BYTES)
            .ok_or(TOO_LARGE)?;

        let mut activities = Vec::new();
        for &byte in chunk {
            if self.completed {
                if !matches!(byte, b'\r' | b'\n') {
                    return Err(INTERRUPTED);
                }
                continue;
            }

            if byte == b'\n' {
                let mut line = std::mem::take(&mut self.line);
                let line_ending_bytes = if line.last() == Some(&b'\r') {
                    line.pop();
                    2
                } else {
                    1
                };
                match self.format {
                    StreamFormat::Sse => {
                        self.process_sse_line(&line, line_ending_bytes, &mut activities)?
                    }
                    StreamFormat::Ndjson => self.process_ndjson_line(&line, &mut activities)?,
                }
            } else {
                self.line.push(byte);
                if self.line.len() > MAX_LINE_OR_EVENT_BYTES {
                    return Err(TOO_LARGE);
                }
            }
        }
        Ok(activities)
    }

    pub(crate) fn finish(self) -> Result<Value, &'static str> {
        if !self.completed || !self.line.is_empty() {
            return Err(INTERRUPTED);
        }
        match self.format {
            StreamFormat::Sse if self.sse_has_data || self.sse_event_bytes != 0 => Err(INTERRUPTED),
            StreamFormat::Sse | StreamFormat::Ndjson => Ok(self.into_response()),
        }
    }

    pub(crate) fn partial_content(&self) -> &str {
        &self.content
    }

    pub fn is_complete(&self) -> bool {
        self.completed
    }

    pub(crate) fn with_reasoning_fields(mut self, fields: &[&'static str]) -> Self {
        self.reasoning_fields = Some(fields.to_vec());
        self
    }

    fn process_sse_line(
        &mut self,
        line: &[u8],
        line_ending_bytes: usize,
        activities: &mut Vec<StreamActivity>,
    ) -> Result<(), &'static str> {
        if line.is_empty() {
            if self.sse_has_data {
                let data = std::mem::take(&mut self.sse_data);
                self.sse_has_data = false;
                if data == b"[DONE]" {
                    self.completed = true;
                } else if !data.is_empty() {
                    self.process_sse_payload(&data, activities)?;
                }
            }
            self.sse_event_bytes = 0;
            return Ok(());
        }

        self.sse_event_bytes = self
            .sse_event_bytes
            .checked_add(line.len() + line_ending_bytes)
            .filter(|size| *size <= MAX_LINE_OR_EVENT_BYTES)
            .ok_or(TOO_LARGE)?;

        if line[0] == b':' {
            activities.push(StreamActivity {
                kind: "heartbeat",
                bytes: line.len(),
            });
            return Ok(());
        }

        let colon = line.iter().position(|byte| *byte == b':');
        let (field, raw_value) = match colon {
            Some(index) => (&line[..index], &line[index + 1..]),
            None => (line, &[][..]),
        };
        let value = raw_value.strip_prefix(b" ").unwrap_or(raw_value);

        match field {
            b"event" if value == b"error" => Err(PROVIDER_ERROR),
            b"data" => {
                if self.sse_has_data {
                    self.sse_data.push(b'\n');
                }
                self.sse_data.extend_from_slice(value);
                self.sse_has_data = true;
                if self.sse_data.len() > MAX_LINE_OR_EVENT_BYTES {
                    return Err(TOO_LARGE);
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }

    fn process_sse_payload(
        &mut self,
        bytes: &[u8],
        activities: &mut Vec<StreamActivity>,
    ) -> Result<(), &'static str> {
        let value: Value = serde_json::from_slice(bytes).map_err(|_| INVALID_RESPONSE)?;
        let object = value.as_object().ok_or(INVALID_RESPONSE)?;
        if object.contains_key("error") {
            return Err(PROVIDER_ERROR);
        }

        let usage = object.get("usage").filter(|usage| !usage.is_null());
        if let Some(usage) = usage {
            self.merge_usage(usage)?;
        }

        let choices = object
            .get("choices")
            .and_then(Value::as_array)
            .ok_or(INVALID_RESPONSE)?;
        if choices.is_empty() {
            return if usage.is_some() {
                Ok(())
            } else {
                Err(INVALID_RESPONSE)
            };
        }
        let mut choice_zero = choices.iter().filter(|choice| {
            choice
                .as_object()
                .and_then(|choice| choice.get("index"))
                .and_then(Value::as_u64)
                == Some(0)
        });
        let choice = choice_zero.next().ok_or(INVALID_RESPONSE)?;
        if choice_zero.next().is_some() {
            return Err(INVALID_RESPONSE);
        }
        let choice = choice.as_object().ok_or(INVALID_RESPONSE)?;
        let finish_reason = optional_string(choice, "finish_reason")?;
        if finish_reason == Some("error") {
            return Err(PROVIDER_ERROR);
        }
        let delta = choice
            .get("delta")
            .and_then(Value::as_object)
            .ok_or(INVALID_RESPONSE)?;

        if let Some(text) = optional_string(delta, "content")? {
            self.append_content(text, activities)?;
        }
        self.append_reasoning_activity(delta, activities);
        if let Some(text) = optional_string(delta, "refusal")? {
            self.append_refusal(text, activities)?;
        }
        if let Some(reason) = finish_reason {
            self.finish_reason = Some(reason.to_owned());
        }
        Ok(())
    }

    fn process_ndjson_line(
        &mut self,
        line: &[u8],
        activities: &mut Vec<StreamActivity>,
    ) -> Result<(), &'static str> {
        if line.is_empty() {
            return Ok(());
        }
        let value: Value = serde_json::from_slice(line).map_err(|_| INVALID_RESPONSE)?;
        let object = value.as_object().ok_or(INVALID_RESPONSE)?;
        if object.contains_key("error") {
            return Err(PROVIDER_ERROR);
        }

        if let Some(message) = object.get("message") {
            let message = message.as_object().ok_or(INVALID_RESPONSE)?;
            if let Some(text) = optional_string(message, "content")? {
                self.append_content(text, activities)?;
            }
            self.append_reasoning_activity(message, activities);
        }

        remember_count(object, "prompt_eval_count", &mut self.prompt_eval_count)?;
        remember_count(
            object,
            "prompt_eval_cached_count",
            &mut self.prompt_eval_cached_count,
        )?;
        remember_count(object, "eval_count", &mut self.eval_count)?;
        if let Some(reason) = optional_string(object, "done_reason")? {
            self.done_reason = Some(Value::String(reason.to_owned()));
        }
        match object.get("done") {
            Some(Value::Bool(true)) => self.completed = true,
            Some(Value::Bool(false)) | None => {}
            Some(Value::Null) => {}
            Some(_) => return Err(INVALID_RESPONSE),
        }
        Ok(())
    }

    fn append_content(
        &mut self,
        text: &str,
        activities: &mut Vec<StreamActivity>,
    ) -> Result<(), &'static str> {
        if text.is_empty() {
            return Ok(());
        }
        self.reserve_visible_bytes(text.len())?;
        self.content.push_str(text);
        activities.push(StreamActivity {
            kind: "content",
            bytes: text.len(),
        });
        Ok(())
    }

    fn append_reasoning_activity(
        &self,
        object: &Map<String, Value>,
        activities: &mut Vec<StreamActivity>,
    ) {
        for field in [
            "reasoning_content",
            "thinking",
            "reasoning",
            "reasoning_details",
        ] {
            if !self.reasoning_field_enabled(field) {
                continue;
            }
            let Some(value) = object.get(field) else {
                continue;
            };
            let bytes = match value {
                Value::String(text) => text.len(),
                Value::Array(items) if field == "reasoning_details" => items
                    .iter()
                    .filter_map(Value::as_object)
                    .filter_map(|item| item.get("text").and_then(Value::as_str))
                    .map(str::len)
                    .fold(0usize, usize::saturating_add),
                _ => 0,
            };
            if bytes > 0 {
                activities.push(StreamActivity {
                    kind: "reasoning",
                    bytes,
                });
            }
        }
    }

    fn reasoning_field_enabled(&self, field: &str) -> bool {
        match &self.reasoning_fields {
            Some(fields) => fields.contains(&field),
            None => matches!(field, "reasoning_content" | "thinking" | "reasoning" | "reasoning_details"),
        }
    }

    fn append_refusal(
        &mut self,
        text: &str,
        activities: &mut Vec<StreamActivity>,
    ) -> Result<(), &'static str> {
        if text.is_empty() {
            return Ok(());
        }
        self.reserve_visible_bytes(text.len())?;
        self.refusal.get_or_insert_with(String::new).push_str(text);
        activities.push(StreamActivity {
            kind: "content",
            bytes: text.len(),
        });
        Ok(())
    }

    fn reserve_visible_bytes(&mut self, bytes: usize) -> Result<(), &'static str> {
        self.visible_content_bytes = self
            .visible_content_bytes
            .checked_add(bytes)
            .filter(|size| *size <= MAX_VISIBLE_CONTENT_BYTES)
            .ok_or(TOO_LARGE)?;
        Ok(())
    }

    fn merge_usage(&mut self, incoming: &Value) -> Result<(), &'static str> {
        if incoming.is_null() {
            return Ok(());
        }
        if !incoming.is_object() {
            return Err(INVALID_RESPONSE);
        }
        let target = self.usage.get_or_insert_with(|| Value::Object(Map::new()));
        merge_non_null(target, incoming);
        Ok(())
    }

    fn into_response(self) -> Value {
        match self.format {
            StreamFormat::Sse => {
                let finish_reason = if self
                    .refusal
                    .as_deref()
                    .is_some_and(|value| !value.is_empty())
                {
                    match self.finish_reason.as_deref() {
                        Some("content_filter") => Value::String("content_filter".to_owned()),
                        _ => Value::String("refusal".to_owned()),
                    }
                } else {
                    self.finish_reason.map(Value::String).unwrap_or(Value::Null)
                };
                let mut message = Map::new();
                message.insert("content".to_owned(), Value::String(self.content));
                if let Some(refusal) = self.refusal {
                    message.insert("refusal".to_owned(), Value::String(refusal));
                }
                let mut response = Map::new();
                response.insert(
                    "choices".to_owned(),
                    json!([{
                        "message": Value::Object(message),
                        "finish_reason": finish_reason,
                    }]),
                );
                if let Some(usage) = self.usage {
                    response.insert("usage".to_owned(), usage);
                }
                Value::Object(response)
            }
            StreamFormat::Ndjson => {
                let mut response = Map::new();
                response.insert("message".to_owned(), json!({"content": self.content}));
                response.insert("done".to_owned(), Value::Bool(true));
                if let Some(reason) = self.done_reason {
                    response.insert("done_reason".to_owned(), reason);
                }
                for (key, value) in [
                    ("prompt_eval_count", self.prompt_eval_count),
                    ("prompt_eval_cached_count", self.prompt_eval_cached_count),
                    ("eval_count", self.eval_count),
                ] {
                    if let Some(value) = value {
                        response.insert(key.to_owned(), value);
                    }
                }
                Value::Object(response)
            }
        }
    }
}

fn optional_string<'a>(
    object: &'a Map<String, Value>,
    field: &str,
) -> Result<Option<&'a str>, &'static str> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) => Ok(Some(value)),
        Some(_) => Err(INVALID_RESPONSE),
    }
}

fn remember_count(
    object: &Map<String, Value>,
    field: &str,
    slot: &mut Option<Value>,
) -> Result<(), &'static str> {
    match object.get(field) {
        None | Some(Value::Null) => Ok(()),
        Some(Value::Number(value)) if value.as_u64().is_some() => {
            *slot = Some(Value::Number(value.clone()));
            Ok(())
        }
        Some(_) => Err(INVALID_RESPONSE),
    }
}

fn merge_non_null(target: &mut Value, incoming: &Value) {
    if incoming.is_null() {
        return;
    }
    match (target, incoming) {
        (Value::Object(target), Value::Object(incoming)) => {
            for (key, value) in incoming {
                if value.is_null() {
                    continue;
                }
                let entry = target.entry(key.clone()).or_insert(Value::Null);
                merge_non_null(entry, value);
            }
        }
        (target, incoming) => *target = incoming.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::{ProviderStreamDecoder, StreamFormat};
    use serde_json::json;

    #[test]
    fn universal_chat_reasoning_fields_are_observed_without_usage_brand_321() {
        for (field, value) in [("reasoning_content", json!("not persisted")),
            ("reasoning", json!("not persisted")), ("thinking", json!("not persisted")),
            ("reasoning_details", json!([{"text":"not persisted"}]))] {
            let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
            let mut delta=json!({}); delta[field]=value;
            let frame = format!("data: {}\n\n", json!({"choices":[{"index":0,"delta":delta}]}));
            let activities = decoder.push(frame.as_bytes()).unwrap();
            assert!(activities.iter().any(|activity| activity.kind == "reasoning"), "missing {field}");
            decoder.push(b"data: [DONE]\n\n").unwrap();
            let output = decoder.finish().unwrap().to_string();
            assert!(!output.contains("not persisted"));
        }
    }

    #[test]
    fn completion_requires_protocol_terminal_not_finish_reason() {
        let mut sse = ProviderStreamDecoder::new(StreamFormat::Sse);
        sse.push(
            b"data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"total_tokens\":1}}\n\n",
        )
        .unwrap();
        assert!(!sse.is_complete());
        sse.push(b"data: [DONE]\n\n").unwrap();
        assert!(sse.is_complete());

        let mut ndjson = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        ndjson.push(b"{\"done\":false}\n").unwrap();
        assert!(!ndjson.is_complete());
        ndjson.push(b"{\"done\":true}\n").unwrap();
        assert!(ndjson.is_complete());
    }

    #[test]
    fn provider_reasoning_allowlist_counts_activity_without_retaining_text() {
        let input = concat!(
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"not allowed\",\"thinking\":\"not allowed\",\"reasoning\":\"hidden\",\"reasoning_details\":[{\"type\":\"reasoning.text\",\"text\":\"推理\"},{\"type\":\"reasoning.encrypted\",\"data\":\"opaque\"},\"unknown\",{\"text\":7}],\"content\":\"answer\"},\"finish_reason\":null}]}\n\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
            "data: [DONE]\n\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse)
            .with_reasoning_fields(&["reasoning", "reasoning_details"]);
        let activities = decoder.push(input.as_bytes()).unwrap();

        assert_eq!(decoder.partial_content(), "answer");
        assert_eq!(
            activities
                .iter()
                .map(|activity| (activity.kind, activity.bytes))
                .collect::<Vec<_>>(),
            vec![
                ("content", "answer".len()),
                ("reasoning", "hidden".len()),
                ("reasoning", "推理".len()),
            ]
        );
        let response = decoder.finish().unwrap().to_string();
        assert!(response.contains("answer"));
        assert!(!response.contains("hidden"));
        assert!(!response.contains("推理"));
        assert!(!response.contains("opaque"));
    }

    #[test]
    fn ndjson_reasoning_allowlist_and_unknown_shapes_do_not_break_content() {
        let mut decoder =
            ProviderStreamDecoder::new(StreamFormat::Ndjson).with_reasoning_fields(&["thinking"]);
        let activities = decoder
            .push(
                b"{\"message\":{\"content\":\"ok\",\"thinking\":\"plan\",\"reasoning_content\":\"ignore\",\"reasoning_details\":{\"text\":\"unknown shape\"}},\"done\":true}\n",
            )
            .unwrap();

        assert_eq!(decoder.partial_content(), "ok");
        assert_eq!(
            activities
                .iter()
                .map(|activity| (activity.kind, activity.bytes))
                .collect::<Vec<_>>(),
            vec![("content", 2), ("reasoning", 4)]
        );
        let response = decoder.finish().unwrap().to_string();
        assert!(!response.contains("plan"));
        assert!(response.contains("ok"));

        let mut unknown = ProviderStreamDecoder::new(StreamFormat::Sse)
            .with_reasoning_fields(&["reasoning", "reasoning_details"]);
        let activities = unknown
            .push(
                b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"still valid\",\"reasoning\":{\"text\":\"unknown\"},\"reasoning_details\":{\"text\":\"unknown\"}},\"finish_reason\":null}]}\n\n",
            )
            .unwrap();
        assert_eq!(unknown.partial_content(), "still valid");
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].kind, "content");
    }

    #[test]
    fn sse_accepts_usage_only_chunk_with_empty_choices() {
        let input = concat!(
            "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":17,\"completion_tokens\":4}}\n\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"answer\"},\"finish_reason\":null}]}\n\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
            "data: [DONE]\n\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        let activities = decoder.push(input.as_bytes()).unwrap();

        assert_eq!(decoder.partial_content(), "answer");
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].kind, "content");
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": "answer"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 17, "completion_tokens": 4}
            })
        );
    }

    #[test]
    fn sse_accumulates_only_choice_index_zero() {
        let input = concat!(
            "data: {\"choices\":[{\"index\":1,\"delta\":{\"content\":\"other\"},\"finish_reason\":null},{\"index\":0,\"delta\":{\"content\":\"first\"},\"finish_reason\":null}]}\n\n",
            "data: {\"choices\":[{\"index\":1,\"delta\":{},\"finish_reason\":\"stop\"},{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
            "data: [DONE]\n\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        let activities = decoder.push(input.as_bytes()).unwrap();

        assert_eq!(decoder.partial_content(), "first");
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].bytes, 5);
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": "first"},
                    "finish_reason": "stop"
                }]
            })
        );
    }

    #[test]
    fn sse_accumulates_utf8_split_across_single_byte_chunks_until_done() {
        let input = concat!(
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"中\"},\"finish_reason\":null}]}\r\n\r\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\r\n\r\n",
            "data: [DONE]\r\n\r\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        let mut activities = Vec::new();

        for byte in input.as_bytes() {
            activities.extend(decoder.push(std::slice::from_ref(byte)).unwrap());
        }

        assert_eq!(decoder.partial_content(), "中");
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].kind, "content");
        assert_eq!(activities[0].bytes, "中".len());
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": "中"},
                    "finish_reason": "stop"
                }]
            })
        );
    }

    #[test]
    fn sse_handles_crlf_multiline_data_heartbeats_and_keeps_reasoning_private() {
        let input = concat!(
            ": ping\r\n\r\n",
            "data: {\"choices\":[\r\n",
            "data: {\"index\":0,\"delta\":{\"role\":\"assistant\",\"thinking\":\"推理\",\"content\":\"好\"},\"finish_reason\":null}]}\r\n\r\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\r\n\r\n",
            "data: [DONE]\r\n\r\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        let activities = decoder.push(input.as_bytes()).unwrap();

        assert_eq!(decoder.partial_content(), "好");
        assert_eq!(activities.len(), 3);
        assert_eq!(activities[0].kind, "heartbeat");
        let mut generated_kinds = activities[1..]
            .iter()
            .map(|activity| activity.kind)
            .collect::<Vec<_>>();
        generated_kinds.sort_unstable();
        assert_eq!(generated_kinds, vec!["content", "reasoning"]);
        assert!(activities
            .iter()
            .any(|activity| activity.kind == "reasoning" && activity.bytes == "推理".len()));
        assert!(activities
            .iter()
            .any(|activity| activity.kind == "content" && activity.bytes == "好".len()));
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": "好"},
                    "finish_reason": "stop"
                }]
            })
        );
    }

    #[test]
    fn sse_merges_last_non_null_usage_and_normalizes_refusal_finish() {
        let input = concat!(
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"refusal\":\"不能回答\"},\"finish_reason\":null}],\"usage\":{\"prompt_tokens\":5,\"total_tokens\":null,\"prompt_tokens_details\":{\"cached_tokens\":2},\"prompt_cache_hit_tokens\":2}}\n\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":null,\"completion_tokens\":1}}\n\n",
            "data: [DONE]\n\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        let activities = decoder.push(input.as_bytes()).unwrap();

        assert_eq!(decoder.partial_content(), "");
        assert_eq!(activities.len(), 1);
        assert_eq!(activities[0].kind, "content");
        assert_eq!(activities[0].bytes, "不能回答".len());
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": "", "refusal": "不能回答"},
                    "finish_reason": "refusal"
                }],
                "usage": {
                    "prompt_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "prompt_cache_hit_tokens": 2,
                    "completion_tokens": 1
                }
            })
        );
    }

    #[test]
    fn sse_provider_errors_are_static_and_do_not_echo_the_payload() {
        let mut event_error = ProviderStreamDecoder::new(StreamFormat::Sse);
        assert_eq!(
            event_error.push(b"event: error\ndata: SECRET provider detail\n\n"),
            Err("MODEL_STREAM_PROVIDER_ERROR")
        );

        let mut field_error = ProviderStreamDecoder::new(StreamFormat::Sse);
        assert_eq!(
            field_error.push(b"data: {\"error\":{\"message\":\"SECRET provider detail\"}}\n\n"),
            Err("MODEL_STREAM_PROVIDER_ERROR")
        );

        let mut finish_error = ProviderStreamDecoder::new(StreamFormat::Sse);
        assert_eq!(
            finish_error.push(
                b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"SECRET body\"},\"finish_reason\":\"error\"}]}\n\n"
            ),
            Err("MODEL_STREAM_PROVIDER_ERROR")
        );
        assert_eq!(finish_error.partial_content(), "");
        assert!(!finish_error.is_complete());
    }

    #[test]
    fn sse_role_and_tool_metadata_do_not_create_reasoning_activity() {
        let input = concat!(
            "data: {\"choices\":[{\"index\":0,\"delta\":{\"role\":\"tool\",\"tool_calls\":[{\"id\":\"call_1\"}]},\"finish_reason\":null}]}\n\n",
            "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
            "data: [DONE]\n\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Sse);
        assert!(decoder.push(input.as_bytes()).unwrap().is_empty());
        assert_eq!(decoder.partial_content(), "");
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "choices": [{
                    "message": {"content": ""},
                    "finish_reason": "stop"
                }]
            })
        );
    }

    #[test]
    fn ndjson_accumulates_utf8_content_and_preserves_done_and_usage_counts() {
        let input = concat!(
            "{\"message\":{\"content\":\"中\"}}\r\n",
            "{\"message\":{\"thinking\":\"plan\"},\"prompt_eval_cached_count\":3}\n",
            "{\"message\":{\"content\":\"文\"},\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":9,\"eval_count\":2}\r\n"
        );
        let mut decoder = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        let mut activities = Vec::new();
        for byte in input.as_bytes() {
            activities.extend(decoder.push(std::slice::from_ref(byte)).unwrap());
        }

        assert_eq!(decoder.partial_content(), "中文");
        assert_eq!(
            activities
                .iter()
                .map(|activity| activity.kind)
                .collect::<Vec<_>>(),
            vec!["content", "reasoning", "content"]
        );
        assert_eq!(activities[1].bytes, 4);
        assert_eq!(
            decoder.finish().unwrap(),
            json!({
                "message": {"content": "中文"},
                "done": true,
                "done_reason": "stop",
                "prompt_eval_count": 9,
                "prompt_eval_cached_count": 3,
                "eval_count": 2
            })
        );
    }

    #[test]
    fn ndjson_missing_cache_counts_remain_absent_and_provider_error_is_redacted() {
        let mut missing = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        missing
            .push(b"{\"message\":{\"content\":\"ok\"},\"done\":true}\n")
            .unwrap();
        let response = missing.finish().unwrap();
        assert!(response.get("prompt_eval_cached_count").is_none());
        assert!(response.get("eval_count").is_none());

        let mut error = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        assert_eq!(
            error.push(b"{\"error\":\"SECRET path\"}\n"),
            Err("MODEL_STREAM_PROVIDER_ERROR")
        );
    }

    #[test]
    fn finish_rejects_missing_terminators_and_incomplete_records() {
        let mut missing_done = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        missing_done
            .push(b"{\"message\":{\"content\":\"partial\"}}\n")
            .unwrap();
        assert_eq!(missing_done.finish(), Err("MODEL_STREAM_INTERRUPTED"));

        let mut incomplete_json = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        incomplete_json.push(b"{\"message\":").unwrap();
        assert_eq!(incomplete_json.finish(), Err("MODEL_STREAM_INTERRUPTED"));

        let mut missing_done_event = ProviderStreamDecoder::new(StreamFormat::Sse);
        missing_done_event
            .push(
                b"data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
            )
            .unwrap();
        assert_eq!(missing_done_event.finish(), Err("MODEL_STREAM_INTERRUPTED"));
    }

    #[test]
    fn decoder_enforces_line_event_and_total_input_limits() {
        let mut overlong_line = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        let line = vec![b'x'; 1024 * 1024 + 1];
        assert_eq!(overlong_line.push(&line), Err("MODEL_RESPONSE_TOO_LARGE"));

        let mut oversized_event = ProviderStreamDecoder::new(StreamFormat::Sse);
        let first = format!("data: {}\n", "a".repeat(600 * 1024));
        let second = format!("data: {}\n", "b".repeat(500 * 1024));
        assert_eq!(
            oversized_event.push(format!("{first}{second}").as_bytes()),
            Err("MODEL_RESPONSE_TOO_LARGE")
        );

        let mut oversized_input = ProviderStreamDecoder::new(StreamFormat::Ndjson);
        let input = vec![b' '; 16 * 1024 * 1024 + 1];
        assert_eq!(
            oversized_input.push(&input),
            Err("MODEL_RESPONSE_TOO_LARGE")
        );
    }
}
