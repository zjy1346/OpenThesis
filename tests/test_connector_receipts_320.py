"""Connection declarations never become provider certification or alter source prompts."""
import pytest

from openthesis.providers import ModelConfig, RustModelGatewayProvider, ProviderError
from openthesis.research_execution.responses import received_response, response_record
from openthesis.resilience.contracts import candidate_document_json


def test_gateway_freezes_version_outside_prompts_for_text_and_vision(monkeypatch):
    provider = RustModelGatewayProvider(ModelConfig(configured_model_id="private", configuration_version=7))
    wires = []
    monkeypatch.setattr(provider, "_invoke_gateway", lambda wire: wires.append(wire) or {})
    provider.generate_once("unchanged system", "full original materials")
    provider.generate_vision_once("unchanged system", "full original materials", b"\x89PNG\r\n\x1a\n")
    assert len(wires) == 2
    for wire in wires:
        assert wire["expected_configuration_version"] == 7
        assert wire["system_prompt"] == "unchanged system"
        assert wire["user_prompt"] == "full original materials"


def test_endpoint_usage_source_survives_typed_persistence_without_zero_or_certification():
    response = received_response({"analysis": "full answer"}, {
        "usage_source": "endpoint_reported_upstream_unverified", "usage_contract": "openai",
        "transport_observation": "declared_stream_returned_non_stream_body",
        "input_tokens": 100, "cache_read_input_tokens": 80, "cache_write_input_tokens": None,
    })
    record, _ = response_record(response)
    metadata = record["receipt"]["metadata"]
    assert metadata["usage_source"] == "endpoint_reported_upstream_unverified"
    assert metadata["cache_write_input_tokens"] is None
    assert metadata["cache_read_input_tokens"] == 80
    assert metadata["transport_observation"] == "declared_stream_returned_non_stream_body"
    assert "provider_verified" not in metadata
    candidate_document_json(record, max_bytes=16 * 1024 * 1024)


@pytest.mark.parametrize("code", ["MODEL_CONFIGURATION_CHANGED", "MODEL_OUTPUT_BUDGET_POLICY_REQUIRED", "MODEL_CONNECTION_AUTH_PAUSED"])
def test_pre_send_configuration_rejection_is_not_an_unknown_submission(tmp_path, code):
    from test_model_request_journal import _store, _registry, _journal, _request, _row
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "source-version")["analysis"]
    request = _request()
    def invoke():
        raise ProviderError("fix connection", code=code, not_sent=True)
    with pytest.raises(ProviderError, match="fix connection"):
        journal.execute(request, invoke, ("analysis", generation))
    assert _row(store, request)["state"] == "rejected"
