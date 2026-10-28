"""Paid field patches remain recoverable without losing other source content."""
from dataclasses import replace
import json
from threading import Event, Thread
from unittest.mock import patch

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageCancelled
from openthesis.resilience.contracts import digest

from test_model_request_journal import _store
from test_text_format_recovery import OfflineWire
from test_text_field_recovery import _execute_field_repair, _request
from test_text_output_recovery import _action_row, _request_row, _bad


def _original():
    return {"structured_output_valid": True, "claims": [], "opaque": {"flag": True}}


def _repair():
    return {"structured_output_valid": True, "section_patches": {"financial_quality": "Review the evidence."}}


@pytest.mark.parametrize("failure_point", ["repairable_fields", "field_repair_prompt"])
def test_local_field_preparation_error_never_erases_received_body(tmp_path, failure_point):
    store = _store(tmp_path)
    original = _original()
    provider = OfflineWire([original])
    with patch("openthesis.research_execution.model." + failure_point, side_effect=RuntimeError("private detail")):
        result = _execute_field_repair(store, provider)
    assert result["opaque"] == original["opaque"] and result["claims"] == []
    assert result["_field_repair"]["code"] == "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"
    assert "private detail" not in json.dumps(result)
    assert json.loads(_request_row(store)["candidate_json"]) == original
    no_wire = OfflineWire([])
    with patch("openthesis.research_execution.model." + failure_point, side_effect=RuntimeError("local")):
        restored = _execute_field_repair(store, no_wire)
    assert restored["opaque"] == original["opaque"] and no_wire.wires == []


def test_field_publication_failure_reuses_paid_wire_and_preserves_both_candidates(tmp_path):
    store = _store(tmp_path)
    original, repair = _original(), _repair()
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_field_publish BEFORE UPDATE ON model_request_journal
                    WHEN OLD.state='candidate' AND NEW.candidate_digest != OLD.candidate_digest
                    BEGIN SELECT RAISE(ABORT, 'fixture_field_publish_failure'); END""")
    provider = OfflineWire([original, repair])
    result = _execute_field_repair(store, provider)
    assert result["_field_repair"]["state"] == "pending"
    assert result["_field_repair"]["code"] == "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"
    assert result["_field_repair"]["targets"] == ["financial_quality"]
    action = _action_row(store)
    assert action["state"] == "validating"
    observed = json.loads(action["result_json"])["candidate"]
    assert observed["original_result"] == original and observed["result"] == repair
    assert json.loads(_request_row(store)["candidate_json"]) == original
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_field_publish")
    assert _execute_field_repair(store, provider) == {**original, **repair["section_patches"]}
    assert len(provider.wires) == 2
    assert json.loads(_action_row(store)["usage_json"])["calls"] == 1


@pytest.mark.parametrize("mutation", ["cancel", "generation", "candidate"])
def test_late_field_wire_is_saved_but_cannot_replace_cancelled_or_changed_original(tmp_path, mutation):
    store = _store(tmp_path)
    original, repair = _original(), _repair()
    changed = {**original, "opaque": {"flag": 1}}

    def mutate_then_return():
        with store.connect() as db:
            if mutation == "cancel":
                row = db.execute("SELECT payload_json FROM research_runs").fetchone()
                payload = json.loads(row[0])
                payload["status"] = "cancelled"
                db.execute("UPDATE research_runs SET status='cancelled',payload_json=?", (json.dumps(payload),))
            elif mutation == "generation":
                db.execute("UPDATE model_stage_request_heads SET generation=generation+1")
            else:
                db.execute("UPDATE model_request_journal SET candidate_json=?,candidate_digest=?",
                           (json.dumps(changed), digest(changed)))
        return repair

    provider = OfflineWire([original, mutate_then_return])
    if mutation == "cancel":
        with pytest.raises(ModelStageCancelled):
            _execute_field_repair(store, provider)
    else:
        result = _execute_field_repair(store, provider)
        assert result["_field_repair"]["state"] == "pending"
        assert result["_field_repair"]["code"] in {
            "MODEL_REQUEST_GENERATION_STALE", "RECOVERY_PLAN_STALE",
            "RECOVERY_MODEL_REQUEST_COMMIT_CONTRACT_INVALID",
            "MODEL_REQUEST_CANDIDATE_CORRUPT",
        }
    assert len(provider.wires) == 2
    assert json.loads(_request_row(store)["candidate_json"]) == (changed if mutation == "candidate" else original)
    saved = json.loads(_action_row(store)["result_json"])
    assert saved["candidate"]["original_result"] == original
    assert saved["candidate"]["result"] == repair
    assert _action_row(store)["state"] != "committed"


def test_concurrent_field_repair_owns_only_one_extra_wire(tmp_path):
    store, started, release = _store(tmp_path), Event(), Event()
    original, repair = _original(), _repair()
    outcomes = []

    def delayed():
        started.set()
        assert release.wait(5)
        return repair

    provider = OfflineWire([original, delayed])

    def first():
        try:
            outcomes.append(_execute_field_repair(store, provider))
        except BaseException as error:
            outcomes.append(error)

    worker = Thread(target=first)
    worker.start()
    try:
        assert started.wait(5)
        racing = _execute_field_repair(store, provider)
        assert racing["_field_repair"] == {
            "state": "pending", "code": "MODEL_REQUEST_IN_PROGRESS",
            "targets": ["financial_quality"],
        }
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [{**original, **repair["section_patches"]}]
    assert len(provider.wires) == 2


def test_field_prompt_keeps_complete_materials_or_stops_before_wire(tmp_path):
    store = _store(tmp_path)
    request = _request()
    request = replace(request, max_input_bytes=request.input_bytes + 1)
    original = _original()
    provider = OfflineWire([original])
    result = _execute_field_repair(store, provider, request=request)
    assert result["_field_repair"] == {
        "state": "pending", "code": "MODEL_FIELD_REPAIR_CONTEXT_REQUIRED",
        "targets": ["financial_quality"],
    }
    assert len(provider.wires) == 1
    assert json.loads(_request_row(store)["candidate_json"]) == original


def test_unknown_json_boundary_does_not_request_full_protocol_regeneration(tmp_path):
    store = _store(tmp_path)
    parsed = _original()
    provider = OfflineWire([_bad("invalid_json"), parsed, _repair()])
    result = _execute_field_repair(store, provider)
    assert result['_output_repair'] == {'state': 'pending', 'code': 'MODEL_OUTPUT_NO_SAFE_CONTINUATION'}
    assert result['_response_error'] == 'invalid_json'
    assert len(provider.wires) == 1
    assert json.loads(_request_row(store)["candidate_json"]) == _bad('invalid_json')
    with store.connect() as db:
        usage = [json.loads(row[0])["calls"] for row in db.execute("SELECT usage_json FROM recovery_actions")]
    assert sum(usage) == 0
