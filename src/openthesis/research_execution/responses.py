"""Safe received-response adaptation, shared by provider and request journals.

External research fields stay open. The internal envelope separates source
content from a typed receipt and path-only diagnostics; neither is a factual
validation. No raw gateway metadata, credentials or exception text is retained.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from urllib.parse import parse_qsl, urlsplit
from typing import Any, Mapping

from ..resilience.contracts import canonical_json, sensitive_candidate_field


RESPONSE_SCHEMA = "openthesis.model-response.v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_ABSENT = object()
_INTEGER_META = frozenset({"credential_version", "connection_configuration_version",
    "model_configuration_version", "configuration_version", "content_length",
    "free_verified_at", "timeout_seconds"})
_STRING_META = frozenset({"configured_model_id", "connection_id", "provider_id", "model_id",
    "alias", "endpoint_host", "finish_reason", "billing_class", "free_source_url", "refusal"})


def redact_response_text(text: str) -> tuple[str, list[dict]]:
    """Remove explicit JSON credential values even when surrounding JSON is bad.

    Scan complete string tokens rather than searching inside arbitrary prose.
    Escaped JSON keys are decoded, and an unterminated secret value is never
    retained. Duplicate-key/shape validity remains the parser's responsibility.
    """
    decoder = json.JSONDecoder()
    cursor, last = 0, 0
    pieces, issues = [], []
    while cursor < len(text):
        if text[cursor] != '"':
            cursor += 1
            continue
        try:
            token, end = decoder.raw_decode(text, cursor)
        except ValueError:
            cursor += 1
            continue
        separator = end
        while separator < len(text) and text[separator].isspace():
            separator += 1
        if (isinstance(token, str) and separator < len(text) and text[separator] == ":"
                and sensitive_candidate_field(token)):
            start = separator + 1
            while start < len(text) and text[start].isspace():
                start += 1
            try:
                _value, value_end = decoder.raw_decode(text, start)
            except ValueError:
                value_end = len(text)
            pieces.extend((text[last:start], '"[credential value isolated]"'))
            issues.append({"code": "MODEL_RESPONSE_SECRET_FIELD_ISOLATED",
                           "path": f"$.wire.content@{cursor}"})
            last, cursor = value_end, value_end
        else:
            cursor = end
    if not issues:
        return text, []
    pieces.append(text[last:])
    return "".join(pieces), issues


def gateway_received_response(text: str, metadata: Any, parse, *, expected_model_id: str,
                              expected_connection_id: str) -> ModelResponse:
    safe_text, issues = redact_response_text(text)
    result = received_response(parse(safe_text), metadata,
        expected_model_id=expected_model_id, expected_connection_id=expected_connection_id)
    result.diagnostics.extend(issues)
    result.receipt["raw_content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result.receipt["received_bytes"] = len(text.encode("utf-8"))
    return result


class ModelResponse(dict):
    """Dict-compatible candidate with detached host-owned receipt/diagnostics."""

    def __init__(self, content: Mapping[str, Any], receipt: dict, diagnostics: list[dict]):
        super().__init__(deepcopy(dict(content)))
        self.receipt = deepcopy(receipt)
        self.diagnostics = deepcopy(diagnostics)
        # Compatibility readers inspect refusal/finish reason. This is a
        # normalized view, never the raw transport object or model-owned meta.
        self["_response_meta"] = deepcopy(receipt.get("metadata", {}))


def safe_response_content(value: Any, issues: list[dict], *, path: str = "$", depth: int = 0) -> Any:
    """Isolate non-JSON/credential fields, leaving independent siblings intact."""
    if depth > 64:
        issues.append({"code": "MODEL_RESPONSE_DEPTH_LIMIT", "path": path})
        return _ABSENT
    if isinstance(value, dict):
        clean = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                issues.append({"code": "MODEL_RESPONSE_KEY_INVALID", "path": path})
                continue
            target = path + "." + key
            if (sensitive_candidate_field(key) or path.startswith("$.receipt.extensions")
                    and key.casefold().startswith(("secret_", "credential_", "authorization_", "password_"))):
                issues.append({"code": "MODEL_RESPONSE_SECRET_FIELD_ISOLATED", "path": target})
                continue
            item = safe_response_content(nested, issues, path=target, depth=depth + 1)
            if item is not _ABSENT:
                clean[key] = item
        return clean
    if isinstance(value, (tuple, list)):
        result = []
        for index, nested in enumerate(value):
            item = safe_response_content(nested, issues, path=f"{path}[{index}]", depth=depth + 1)
            if item is not _ABSENT:
                result.append(item)
        return result
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    issues.append({"code": "MODEL_RESPONSE_VALUE_INVALID", "path": path})
    return _ABSENT


def received_response(content: dict, metadata: Any, *, expected_model_id: str = "",
                      expected_connection_id: str = "") -> ModelResponse:
    """Normalize a received candidate; explicit transport identity conflicts fail."""
    issues: list[dict] = []
    metadata = metadata if isinstance(metadata, dict) else {}
    receipt = {"schema": RESPONSE_SCHEMA, "response_received": True, "metadata": {}, "extensions": {}}
    for name, value in metadata.items():
        path = "$.receipt.metadata." + str(name)
        if name in _INTEGER_META:
            if value is None and name == "free_verified_at":
                receipt["metadata"][name] = None
            elif type(value) is int and value >= 0:
                receipt["metadata"][name] = value
            else:
                issues.append({"code": "MODEL_RESPONSE_META_TYPE_INVALID", "path": path})
        elif name in _STRING_META:
            sensitive_url = False
            if name == "free_source_url" and isinstance(value, str):
                try:
                    parsed = urlsplit(value)
                    sensitive_url = bool(parsed.username or parsed.password or any(
                        sensitive_candidate_field(key) for key, _ in parse_qsl(parsed.query)))
                except ValueError:
                    sensitive_url = True
            if sensitive_url:
                issues.append({"code": "MODEL_RESPONSE_SECRET_FIELD_ISOLATED", "path": path})
            elif value is None or isinstance(value, str) and len(value) <= 4096:
                receipt["metadata"][name] = value
            else:
                issues.append({"code": "MODEL_RESPONSE_META_TYPE_INVALID", "path": path})
        elif name == "temperature":
            if value is None or type(value) in (float, int) and math.isfinite(value):
                receipt["metadata"][name] = value
            else:
                issues.append({"code": "MODEL_RESPONSE_META_TYPE_INVALID", "path": path})
        else:
            safe = safe_response_content({name: value}, issues, path="$.receipt.extensions")
            # Optional diagnostics have a declared independent retention cap.
            # Domain content is never silently truncated to fit this cap.
            if len(canonical_json({**receipt["extensions"], **safe}).encode("utf-8")) <= 64 * 1024:
                receipt["extensions"].update(safe)
            else:
                issues.append({"code": "MODEL_RESPONSE_OPTIONAL_META_LIMIT", "path": path})
    for name, expected in (("configured_model_id", expected_model_id),
                           ("connection_id", expected_connection_id)):
        if expected and name in metadata and metadata[name] != expected:
            # Imported lazily: providers calls this Adapter after module init.
            from ..providers import ProviderError
            raise ProviderError("Received response identity conflicts with request",
                code="MODEL_RESPONSE_IDENTITY_MISMATCH", response_received=True,
                processing_phase="response_identity", content_path="$.receipt.metadata." + name,
                safe_receipt={"schema": RESPONSE_SCHEMA, "response_received": True,
                              "identity_conflict": name})
    clean = dict(content)
    for name in ("_response_meta", "_response_receipt", "_response_diagnostics"):
        if name in clean:
            clean.pop(name)
            issues.append({"code": "MODEL_RESPONSE_CONTROL_FIELD_ISOLATED", "path": "$.content." + name})
    clean = safe_response_content(clean, issues, path="$.content")
    return ModelResponse(clean, receipt, issues)


def response_record(value: Any) -> tuple[dict, dict]:
    """Canonical durable representation and safe receipt, for new or old callers."""
    if not isinstance(value, dict):
        raise ValueError("MODEL_STAGE_CANDIDATE_INVALID")
    if isinstance(value, ModelResponse):
        response = value
    elif "_response_meta" in value:
        response = received_response({key: nested for key, nested in value.items()
                                      if key != "_response_meta"}, value.get("_response_meta"))
    else:
        issues: list[dict] = []
        clean = safe_response_content(value, issues)
        receipt = {"schema": RESPONSE_SCHEMA, "response_received": True}
        if not issues and clean.get("schema") != RESPONSE_SCHEMA:
            # Historical non-gateway callers retain their exact body/digest.
            return clean, receipt
        response = ModelResponse(clean, receipt, issues)
    content = {key: nested for key, nested in response.items() if key != "_response_meta"}
    return {"schema": RESPONSE_SCHEMA, "content": content,
            "receipt": deepcopy(response.receipt), "diagnostics": deepcopy(response.diagnostics)}, response.receipt


def restore_response(record: dict) -> dict:
    if record.get("schema") != RESPONSE_SCHEMA:
        return record
    if (not isinstance(record.get("content"), dict) or not isinstance(record.get("receipt"), dict)
            or not isinstance(record.get("diagnostics"), list)
            or record["receipt"].get("response_received") is not True):
        raise ValueError("MODEL_RESPONSE_ENVELOPE_INVALID")
    return ModelResponse(record["content"], record["receipt"], record["diagnostics"])
