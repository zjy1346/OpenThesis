"""Malformed response candidates must not leak explicit credential values."""
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from openthesis.providers import ModelConfig, RustModelGatewayProvider
from openthesis.research_execution.responses import response_record


@pytest.mark.parametrize("content", [
    '{"business_model":"independent research","api_key":"private-canary","api_key":"private-canary"}',
    '{"business_model":"independent research","authorization":"private-canary"',
    '{"business_model":"independent research","api_\\u006bey":"private-canary","broken":',
    '{"business_model":"independent research","cookie":{"value":"private-canary"},"broken":',
])
def test_received_malformed_json_does_not_persist_secret_values(content):
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="test"))
    wire = {"ok": True, "wire_contract": "text-single-request-v1", "result": {
        "content": content, "meta": {"configured_model_id": "test", "credential_version": 1}}}
    with patch.object(provider, "_validated_gateway_path", return_value=Path("unused.exe")), patch(
            "openthesis.providers.subprocess.run", return_value=CompletedProcess([], 0,
            json.dumps(wire).encode(), b"")):
        result = provider.generate_once("system", "user")
    record, _ = response_record(result)
    persisted = json.dumps(record)
    assert "private-canary" not in persisted
    assert "independent research" in persisted
    assert any(issue["code"] == "MODEL_RESPONSE_SECRET_FIELD_ISOLATED" for issue in record["diagnostics"])
