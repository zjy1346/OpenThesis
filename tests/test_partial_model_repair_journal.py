"""Formal journal/coordinator persistence of independent repair progress.

The offline wire exercises publication and restart, not a live provider or
whole-company report acceptance. Claims still use the real research verifier.
"""
from copy import deepcopy
import json

from test_model_request_journal import _store
from test_text_field_recovery import _execute_field_repair, _request
from test_text_output_recovery import OfflineWire, _request_row
from test_research_claim_repair import ambiguous_candidate, contract, fact
from openthesis.research_execution.model import ModelStageRequest


def _outcomes(store):
    with store.connect() as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT outcome_json FROM recovery_plans WHERE outcome_json IS NOT NULL")]


def test_field_partial_commit_survives_restart_without_repairing_healthy_target(tmp_path):
    store = _store(tmp_path)
    original = {"structured_output_valid": True, "claims": {"bad": "shape"},
                "business_model": "Healthy, unchanged."}
    patch = {"structured_output_valid": True, "repair_note": "One target remains.",
             "section_patches": {"financial_quality": "Review of financial quality.",
                                 "claims": {"still": "wrong"},
                                 "business_model": "Unrequested overwrite"}}
    provider = OfflineWire([original, patch])
    result = _execute_field_repair(store, provider)
    assert result["financial_quality"] == "Review of financial quality."
    assert result["business_model"] == original["business_model"]
    assert result["claims"] == original["claims"]
    assert result["_field_repair"]["targets"] == ["claims"]
    assert result["_field_repair"]["accepted_targets"] == ["financial_quality"]
    outcome = _outcomes(store)[0]
    assert outcome["state"] == "partial_committed"
    assert outcome["accepted_delta"] == ["financial_quality"]
    assert len(outcome["resolved_issue_ids"]) == len(outcome["remaining_issue_ids"]) == 1
    assert json.loads(_request_row(store)["candidate_json"]) == result
    no_wire = OfflineWire([])
    restart = _execute_field_repair(store, no_wire)
    assert restart["financial_quality"] == result["financial_quality"]
    assert restart["business_model"] == original["business_model"]
    assert restart["_field_repair"]["targets"] == ["claims"]
    assert no_wire.wires == []


def test_claim_partial_commit_preserves_proof_and_replans_only_unresolved_claim(tmp_path):
    store = _store(tmp_path)
    original = ambiguous_candidate()
    original["claims"].append(deepcopy(original["claims"][0]))
    original["claims"][0]["claim_id"] = "claim:first"
    original["claims"][1]["claim_id"] = "claim:second"
    repair = contract([fact(), fact("fact:previous", year=2024, value=100)])
    plan = repair.plan(original)
    assert len(plan.targets) == 2
    patch = {"structured_output_valid": True, "repair_note": "Still missing one unit.",
             "claim_patches": [
                 {"path": plan.targets[0].path, "claim_id": "claim:first",
                  "fields": {"value": 120, "unit": "CNY", "end_date": "2025-12-31"}},
                 {"path": plan.targets[1].path, "claim_id": "claim:second",
                  "fields": {"value": 120, "end_date": "2025-12-31"}},
             ]}
    request = ModelStageRequest("analysis", "system", "Full original source evidence",
                                claim_repair_contract=repair)
    result = _execute_field_repair(store, OfflineWire([original, patch]), request=request)
    assert result["claims"][0]["value"] == 120
    assert result["claims"][1] == original["claims"][1]
    assert result["executive_summary"] == original["executive_summary"]
    assert result["_claim_repair"]["targets"] == ["/claims/1"]
    assert [target.path for target in repair.plan(result).targets] == ["/claims/1"]
    outcome = _outcomes(store)[0]
    assert outcome["state"] == "partial_committed"
    assert outcome["accepted_delta"] == ["/claims/0"]
    assert len(outcome["remaining_issue_ids"]) == 1
    no_wire = OfflineWire([])
    restart = _execute_field_repair(store, no_wire, request=request)
    assert restart["claims"] == result["claims"]
    assert no_wire.wires == []
