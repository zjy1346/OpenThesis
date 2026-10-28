"""Durable, allow-listed repair of missing top-level model fields.

These tests exercise the real journal/executor seam with an offline wire.  A
field repair only restores the explicitly requested output shape; it must not
rewrite the original prompt, silently add unrelated sections, or bypass the
normal research validator.
"""
from __future__ import annotations

import json

import pytest

from openthesis.demo import demo_facts
from openthesis.financial_evidence import build_fact_evidence
from openthesis.providers import ProviderError
from openthesis.research import verify_agent_output
from openthesis.research_execution.model import (
    ModelStageCancelled,
    ModelStageExecutor,
    ModelStageRequest,
)
from openthesis.research_execution.model_output import ModelOutputContract, OutputField
from openthesis.resilience.contracts import RecoveryLimits, digest
from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_text_output_recovery import OfflineWire, _action_row, _request_row


_SYSTEM = "完整系统提示：保留所有原始研究材料"
_USER = json.dumps({"research_context": {"company": {"cik": "fixture"},
    "evidence_partitions": {"quantitative_facts": [{"evidence_id": "source:annual", "value": 100,
        "concept": "revenue", "currency": "CNY", "fiscal_year": 2025, "raw_text": "revenue=100"}]}},
    "output_language": "zh-CN"}, ensure_ascii=False)
_CONTRACT = ModelOutputContract(
    "test-fields-v1",
    (OutputField("financial_quality"), OutputField("claims", ("array",))),
)
_ALT_CONTRACT = ModelOutputContract(
    "test-fields-v2",
    (OutputField("financial_quality"), OutputField("claims", ("array",))),
)


def _request(contract: ModelOutputContract | None = _CONTRACT) -> ModelStageRequest:
    return ModelStageRequest("analysis", _SYSTEM, _USER, output_contract=contract)


def _prepare(store, *, journal, request):
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    return generation


def _execute_field_repair(
    store,
    provider,
    *,
    request: ModelStageRequest | None = None,
    journal=None,
    single_attempt: bool = False,
):
    request = request or _request()
    journal = journal or _journal(store)
    generation = _prepare(store, journal=journal, request=request)
    with journal.bind("analysis", generation):
        return ModelStageExecutor(
            provider,
            ImmediateScheduler(),
            cancel_check=journal.cancel_check,
            request_journal=journal,
        ).execute(request, single_attempt=single_attempt)


def _action_payload(store):
    row = _action_row(store)
    assert row is not None and row["result_json"] is not None
    envelope = json.loads(row["result_json"])
    return envelope["candidate"]


def test_missing_field_repairs_only_requested_section_and_preserves_unknown_material(tmp_path):
    store = _store(tmp_path)
    original = {
        "structured_output_valid": True,
        "claims": [{"text": "original claim", "evidence_ids": ["fact:known"]}],
        "unknown_extension": {"opaque": [1, 2, 3]},
    }
    repair = {
        "structured_output_valid": True,
        "section_patches": {"financial_quality": {"status": "verified"}},
    }
    provider = OfflineWire([original, repair])

    result = _execute_field_repair(store, provider)

    assert result["financial_quality"] == repair["section_patches"]["financial_quality"]
    assert result["claims"] == original["claims"]
    assert result["unknown_extension"] == original["unknown_extension"]
    assert "section_patches" not in result
    assert len(provider.wires) == 2
    assert provider.wires[1]["system_prompt"] == provider.wires[0]["system_prompt"] == _SYSTEM
    assert not provider.wires[1]["user_prompt"].startswith(_USER)
    targeted = json.loads(provider.wires[1]['user_prompt'].split('\n\n[OpenThesis')[0])
    assert targeted['target_fields'] == ['financial_quality']
    assert targeted['research_context']['evidence_partitions']['quantitative_facts'][0]['value'] == 100
    assert "original claim" not in provider.wires[1]["user_prompt"]
    payload = _action_payload(store)
    assert payload["original_result"] == original
    assert payload["result"] == repair


def test_wrong_type_repairs_only_claims_and_retains_other_valid_field(tmp_path):
    store = _store(tmp_path)
    original = {
        "structured_output_valid": True,
        "financial_quality": {"status": "verified"},
        "claims": {"wrong": "shape"},
        "opaque_extension": "keep",
    }
    repair = {
        "structured_output_valid": True,
        "section_patches": {"claims": [{"text": "repaired claim"}]},
    }
    result = _execute_field_repair(store, OfflineWire([original, repair]))

    assert result["claims"] == repair["section_patches"]["claims"]
    assert result["financial_quality"] == original["financial_quality"]
    assert result["opaque_extension"] == original["opaque_extension"]


def test_complete_contract_does_not_open_a_second_wire(tmp_path):
    store = _store(tmp_path)
    complete = {
        "structured_output_valid": True,
        "financial_quality": {"status": "verified"},
        "claims": [{"text": "already complete"}],
    }
    provider = OfflineWire([complete])

    assert _execute_field_repair(store, provider) == complete
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


@pytest.mark.parametrize("repair", [
    {
        "structured_output_valid": True,
        "section_patches": {"financial_quality": {}, "claims": []},
        "unexpected_top_level": "reject",
    },
    {"structured_output_valid": True, "section_patches": {}},
    {"structured_output_valid": True,
     "section_patches": {"financial_quality": 7}},
])
def test_field_patch_shape_or_allowlist_violation_is_rejected_without_third_wire(tmp_path, repair):
    store = _store(tmp_path)
    original = {"structured_output_valid": True, "claims": [{"text": "preserve"}]}
    provider = OfflineWire([original, repair, {"structured_output_valid": True}])

    result = _execute_field_repair(store, provider)
    if repair.get("unexpected_top_level"):
        # 3.0.2: unknown safe metadata and an unrequested patch are isolated,
        # not permission to overwrite healthy claims or reject a valid target.
        assert result["financial_quality"] == {}
        assert result["claims"] == original["claims"]
        assert "unexpected_top_level" not in result
        assert "_field_repair" not in result
    else:
        assert result["_field_repair"]["state"] == "pending"
        assert result["_field_repair"]["code"] == "MODEL_FIELD_REPAIR_INVALID"
        assert result["_field_repair"]["targets"] == ["financial_quality"]

    assert len(provider.wires) == 2
    payload = _action_payload(store)
    assert payload["original_result"] == original
    assert payload["result"] == repair
    row = _request_row(store)
    assert row["state"] == "candidate"
    expected = result if repair.get("unexpected_top_level") else original
    assert json.loads(row["candidate_json"]) == expected


@pytest.mark.parametrize("meta", [
    {"finish_reason": "refusal", "refusal": "not available"},
    {"finish_reason": "content_filter"},
])
def test_refusal_or_content_filter_never_repairs_missing_fields(tmp_path, meta):
    store = _store(tmp_path)
    original = {"structured_output_valid": True, "_response_meta": meta}
    provider = OfflineWire([original, {
        "structured_output_valid": True,
        "section_patches": {"financial_quality": {}, "claims": []},
    }])

    assert _execute_field_repair(store, provider) == original
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


def test_single_attempt_never_repairs_fields(tmp_path):
    store = _store(tmp_path)
    original = {"structured_output_valid": True}
    provider = OfflineWire([original, {
        "structured_output_valid": True,
        "section_patches": {"financial_quality": {}, "claims": []},
    }])

    assert _execute_field_repair(store, provider, single_attempt=True) == original
    assert len(provider.wires) == 1


def test_unknown_field_repair_timeout_is_not_retried_after_reconstruction_or_contract_change(tmp_path):
    store = _store(tmp_path)
    original = {"structured_output_valid": True, "claims": [{"text": "keep"}]}
    provider = OfflineWire([original, TimeoutError("response status unknown")])

    first = _execute_field_repair(store, provider, request=_request(_CONTRACT))
    assert first["_field_repair"] == {
        "state": "pending", "code": "MODEL_SUBMISSION_UNKNOWN", "targets": ["financial_quality"],
    }
    second = _execute_field_repair(store, provider, request=_request(_ALT_CONTRACT))
    assert second["_field_repair"] == first["_field_repair"]
    assert len(provider.wires) == 2


def test_zero_field_repair_budget_preserves_original_candidate_without_compensation(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    journal.repair_limits = RecoveryLimits(calls=0, transient_retries=0)
    original = {"structured_output_valid": True, "claims": [{"text": "keep"}]}
    provider = OfflineWire([original])

    result = _execute_field_repair(store, provider, journal=journal)
    assert result["_field_repair"] == {
        "state": "pending", "code": "RECOVERY_INCIDENT_BUDGET_EXHAUSTED",
        "targets": ["financial_quality"],
    }
    assert len(provider.wires) == 1
    row = _request_row(store)
    assert row["state"] == "candidate"
    assert json.loads(row["candidate_json"]) == original


def test_contract_changes_do_not_change_wire_input_digest_but_contract_digest_is_distinct():
    first = _request(_CONTRACT)
    second = _request(_ALT_CONTRACT)
    assert first.input_digest == second.input_digest
    assert digest(first.output_contract.to_dict()) != digest(second.output_contract.to_dict())


def test_merged_field_with_unknown_evidence_still_fails_existing_validator(tmp_path):
    store = _store(tmp_path)
    original = {
        "structured_output_valid": True,
        "financial_quality": {"status": "verified"},
    }
    repair = {
        "structured_output_valid": True,
        "section_patches": {
            "claims": [{
                "kind": "fact",
                "concept": "revenue",
                "value": 7_150_000_000,
                "unit": "USD",
                "currency": "USD",
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "evidence_ids": ["fact:not-in-demo"],
            }]
        },
    }
    result = _execute_field_repair(store, OfflineWire([original, repair]))
    evidence = build_fact_evidence(demo_facts())
    evidence_records = {item["evidence_id"]: item for item in evidence}
    checked = verify_agent_output(
        result,
        set(evidence_records),
        evidence_records=evidence_records,
    )
    assert checked["passed"] is False
    assert any("引用不存在" in issue or "Unknown evidence" in issue for issue in checked["issues"])
