"""Reference transport decoding is local, reversible and after durable receipt."""
from dataclasses import replace
import json

import pytest

from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.research_execution.responses import ModelResponse
from test_model_request_journal import _journal, _registry, _store, _row
from test_model_stage_executor import ImmediateScheduler
from test_text_field_recovery import _execute_field_repair, _request
from test_text_output_recovery import OfflineWire


def test_binding_identity_is_part_of_request_and_plain_text_is_never_decoded():
    request = ModelStageRequest("analysis", "s", "same short wire", reference_bindings=(("@E0", "source:a"),))
    assert request.input_digest != replace(request, reference_bindings=(("@E0", "source:b"),)).input_digest
    result = request.decode_candidate(ModelResponse({"claims": [{"text": "@E0 remains prose", "evidence_ids": ["@E0", "@E99"]}]},
        {"response_received": True, "metadata": {"input_tokens": 120}}, ()))
    assert result["claims"][0] == {"text": "@E0 remains prose", "evidence_ids": ["source:a", "@E99"]}
    assert result.receipt["metadata"]["input_tokens"] == 120


def test_received_alias_candidate_survives_local_decode_failure_and_restart(tmp_path, monkeypatch):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "same-frozen-source")["analysis"]
    request = ModelStageRequest("analysis", "s", "wire", reference_bindings=(("@E0", "source:a"),))
    provider = OfflineWire([{"claims": [{"text": "Saved original", "evidence_ids": ["@E0"]}]}])
    original = ModelStageRequest.decode_candidate

    def fail_local(self, candidate):
        assert _row(store, request)["state"] == "candidate"
        raise ValueError("local decoder unavailable")

    monkeypatch.setattr(ModelStageRequest, "decode_candidate", fail_local)
    with journal.bind("analysis", generation), pytest.raises(ValueError, match="decoder unavailable"):
        ModelStageExecutor(provider, ImmediateScheduler(), request_journal=journal).execute(request)
    assert len(provider.wires) == 1
    assert "@E0" in _row(store, request)["candidate_json"]
    monkeypatch.setattr(ModelStageRequest, "decode_candidate", original)
    reopened = _journal(store)
    with reopened.bind("analysis", generation):
        result = ModelStageExecutor(OfflineWire([]), ImmediateScheduler(), request_journal=reopened).execute(request)
    assert result["claims"][0]["evidence_ids"] == ["source:a"]
    assert _row(store, request)["state"] == "candidate"


def test_field_repair_decodes_only_after_coordinator_saves_raw_reply(tmp_path):
    store = _store(tmp_path)
    request = replace(_request(), reference_bindings=(("@E0", "source:annual"),))
    provider = OfflineWire([
        {"structured_output_valid": True, "claims": [{"text": "original", "evidence_ids": ["@E0"]}]},
        {"structured_output_valid": True, "section_patches": {"financial_quality": {
            "analysis": "the unmodified narrative", "evidence_ids": ["@E0"]}}},
    ])
    result = _execute_field_repair(store, provider, request=request)
    assert result["claims"][0]["evidence_ids"] == ["source:annual"]
    assert result["financial_quality"]["evidence_ids"] == ["source:annual"]
    with store.connect() as db:
        received = json.loads(db.execute("SELECT result_json FROM recovery_actions").fetchone()[0])
    assert received["candidate"]["result"]["section_patches"]["financial_quality"]["evidence_ids"] == ["@E0"]
    assert len(provider.wires) == 2
    assert _execute_field_repair(store, OfflineWire([]), request=request)["financial_quality"] == result["financial_quality"]


def test_local_only_graph_revalidation_restores_original_citations_without_provider(tmp_path):
    from openthesis.research_execution.production import _local_journal_candidates
    store = _store(tmp_path)
    journal = _journal(store)
    registry = _registry(("analysis", ()))
    generation = journal.prepare(registry, "same-frozen-source")["analysis"]
    request = ModelStageRequest("analysis", "s", "wire", reference_bindings=(("@E0", "source:a"),))
    provider = OfflineWire([{"claims": [{"text": "original", "evidence_ids": ["@E0"]}]}])
    with journal.bind("analysis", generation):
        ModelStageExecutor(provider, ImmediateScheduler(), request_journal=journal).execute(request)
    reopened = _journal(store)
    recovered = _local_journal_candidates(reopened, registry, {})
    assert recovered["analysis"].candidate["claims"][0]["evidence_ids"] == ["source:a"]
    assert len(provider.wires) == 1
    assert "@E0" in _row(store, request)["candidate_json"]


def test_host_binding_receipt_is_not_model_authority_and_mismatched_identity_is_rejected():
    from openthesis.research_execution.reference_aliases import bind_response_record, restore_saved_references
    from openthesis.research_execution.responses import response_record, restore_response
    raw = ModelResponse({"claims": [{"evidence_ids": ["@E0"]}]}, {"response_received": True,
        "host_source_references": {"bindings": [["@E0", "source:invented"]]}}, [])
    record, receipt = response_record(raw)
    record, receipt = bind_response_record(record, receipt, (), "input")
    assert "host_source_references" not in receipt
    record, receipt = bind_response_record(record, receipt, (("@E0", "source:actual"),), "input")
    response = restore_response(record)
    assert restore_saved_references(response, "input")["claims"][0]["evidence_ids"] == ["source:actual"]
    with pytest.raises(ValueError, match="SOURCE_BINDINGS_INVALID"):
        restore_saved_references(response, "different-input")
