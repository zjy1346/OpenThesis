"""Claim path repair uses the existing durable request and incident budget."""
import json
from unittest.mock import patch

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageRequest
from openthesis.resilience.contracts import RecoveryLimits
from test_model_request_journal import _store, _journal
from test_text_field_recovery import _execute_field_repair
from test_text_output_recovery import OfflineWire, _action_row, _request_row
from test_research_claim_repair import contract, fact, ambiguous_candidate, patch_response


def setup():
    original = ambiguous_candidate()
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    plan = repair.plan(original)
    patch = patch_response(plan, {"value": 120, "unit": "CNY", "end_date": "2025-12-31"})
    request = ModelStageRequest("analysis", "unchanged system", "full original evidence",
                                claim_repair_contract=repair)
    return original, patch, request


def test_one_claim_wire_persists_original_and_reuses_repaired_candidate_on_restart(tmp_path):
    store = _store(tmp_path)
    original, patch, request = setup()
    provider = OfflineWire([original, patch])
    result = _execute_field_repair(store, provider, request=request)
    assert result["claims"][0]["value"] == 120
    assert result["executive_summary"] == original["executive_summary"]
    assert len(provider.wires) == 2
    assert provider.wires[1]["user_prompt"].startswith(request.user_prompt)
    assert provider.wires[1]["system_prompt"] == request.system_prompt
    payload = json.loads(_action_row(store)["result_json"])["candidate"]
    assert payload["original_result"] == original
    assert payload["result"] == patch
    assert payload["claim_repair_plan"]["validator_version"] == request.claim_repair_contract.validator_version
    assert json.loads(_request_row(store)["candidate_json"]) == result
    # Reconstruct journal/provider, keeping the same durable run identity.
    no_wire = OfflineWire([])
    assert _execute_field_repair(store, no_wire, request=request) == result
    assert no_wire.wires == []


@pytest.mark.parametrize("failure", ["wrong_patch", "truncated", "refusal", "timeout"])
def test_failed_claim_repair_keeps_candidate_and_cannot_reissue_same_wire(tmp_path, failure):
    store = _store(tmp_path)
    original, patch, request = setup()
    if failure == "wrong_patch":
        patch["claim_patches"][0]["fields"]["value"] = 999
    elif failure == "truncated":
        patch["_response_meta"] = {"finish_reason": "length"}
    elif failure == "refusal":
        patch["_response_meta"] = {"refusal": "refused"}
    else:
        patch = TimeoutError("unknown wire outcome")
    provider = OfflineWire([original, patch])
    result = _execute_field_repair(store, provider, request=request)
    assert result["executive_summary"] == original["executive_summary"]
    assert result["claims"] == original["claims"]
    assert result["_claim_repair"]["state"] == "pending"
    assert len(provider.wires) == 2
    assert json.loads(_request_row(store)["candidate_json"]) == original
    if failure == "timeout":
        retried = _execute_field_repair(store, provider, request=request)
        assert retried["claims"] == original["claims"]
        assert retried["_claim_repair"]["code"] in {"MODEL_SUBMISSION_UNKNOWN", "MODEL_REQUEST_IN_PROGRESS"}
        replacement = _journal(store, config="explicit-alternative-model")
        with pytest.raises(ProviderError) as caught:
            _execute_field_repair(store, provider, request=request, journal=replacement)
        assert caught.value.code in {"MODEL_SUBMISSION_UNKNOWN", "MODEL_REQUEST_IN_PROGRESS"}
    else:
        retried = _execute_field_repair(store, provider, request=request)
        assert retried["claims"] == original["claims"]
    assert len(provider.wires) == 2


def test_zero_budget_does_not_clear_original_or_launch_an_extra_request(tmp_path):
    store = _store(tmp_path)
    original, patch, request = setup()
    provider = OfflineWire([original, patch])
    journal = _journal(store)
    journal.repair_limits = RecoveryLimits(calls=0, transient_retries=0)
    result = _execute_field_repair(store, provider, request=request, journal=journal)
    assert result["claims"] == original["claims"]
    assert result["_claim_repair"]["state"] == "pending"
    assert len(provider.wires) == 1


def test_semantic_repair_plan_change_does_not_reset_incident_budget(tmp_path):
    store = _store(tmp_path)
    original, patch, request = setup()
    patch["claim_patches"][0]["fields"]["value"] = 999
    provider = OfflineWire([original, patch])
    first = _execute_field_repair(store, provider, request=request)
    assert first["_claim_repair"]["state"] == "pending"
    request.claim_repair_contract.validator_version = "new-validator"
    second = _execute_field_repair(store, provider, request=request)
    assert second["claims"] == original["claims"]
    assert len(provider.wires) == 2


def test_context_overflow_keeps_full_candidate_and_sends_no_truncated_repair(tmp_path):
    store = _store(tmp_path)
    original, patch, request = setup()
    request = ModelStageRequest(request.stage_id, request.system_prompt, request.user_prompt,
        max_input_bytes=request.input_bytes, claim_repair_contract=request.claim_repair_contract)
    provider = OfflineWire([original, patch])
    result = _execute_field_repair(store, provider, request=request)
    assert result["claims"] == original["claims"]
    assert result["_claim_repair"]["code"] == "MODEL_CLAIM_REPAIR_CONTEXT_REQUIRED"
    assert len(provider.wires) == 1


@pytest.mark.parametrize("failure_point", ["plan", "prompt"])
def test_local_claim_preparation_error_keeps_received_body_across_restart(tmp_path, failure_point):
    store = _store(tmp_path)
    original, _, request = setup()
    provider = OfflineWire([original])
    with patch.object(request.claim_repair_contract, failure_point, side_effect=RuntimeError("private failure detail")):
        result = _execute_field_repair(store, provider, request=request)
    assert result["claims"] == original["claims"]
    assert result["executive_summary"] == original["executive_summary"]
    assert result["_claim_repair"]["code"] == "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"
    assert "private failure detail" not in json.dumps(result)
    assert len(provider.wires) == 1
    assert json.loads(_request_row(store)["candidate_json"]) == original
    no_wire = OfflineWire([])
    with patch.object(request.claim_repair_contract, failure_point, side_effect=RuntimeError("still local")):
        restored = _execute_field_repair(store, no_wire, request=request)
    assert restored["claims"] == original["claims"] and no_wire.wires == []
