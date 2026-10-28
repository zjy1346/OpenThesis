"""Provider safety signals cannot become JSON repair candidates."""
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from openthesis.providers import ModelConfig, RustModelGatewayProvider


@pytest.mark.parametrize("reason,content", [
    ("refusal", ""),
    ("refusal", '{"claims":[],"structured_output_valid":true}'),
    ("content_filter", ""),
    ("content_filter", '{"claims":[],"structured_output_valid":true}'),
])
def test_explicit_refusal_is_not_valid_output_even_when_content_is_json(reason, content):
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="offline-refusal"))
    response = {"ok": True, "wire_contract": "text-single-request-v1",
                "result": {"content": content, "meta": {"finish_reason": reason}}}
    completed = CompletedProcess([], 0, json.dumps(response).encode(), b"")
    with patch.object(provider, "_validated_gateway_path", return_value=Path("offline-fixture.exe")), \
            patch("openthesis.providers.subprocess.run", return_value=completed) as wire:
        result = provider.generate_once("full system", "all evidence")
    assert result["structured_output_valid"] is False
    assert result["_response_error"] == "provider_refusal"
    assert result["_response_meta"]["finish_reason"] == reason
    assert wire.call_count == 1


def test_normal_completed_json_still_passes_protocol_parsing():
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="offline-success"))
    response = {"ok": True, "wire_contract": "text-single-request-v1",
                "result": {"content": '{"ok":true}', "meta": {"finish_reason": "stop"}}}
    completed = CompletedProcess([], 0, json.dumps(response).encode(), b"")
    with patch.object(provider, "_validated_gateway_path", return_value=Path("offline-fixture.exe")), \
            patch("openthesis.providers.subprocess.run", return_value=completed) as wire:
        result = provider.generate_once("full system", "all evidence")
    assert result["structured_output_valid"] is True and result["ok"] is True
    assert wire.call_count == 1
