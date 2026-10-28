"""Durable recovery for provider responses that are present but unusable.

These tests intentionally exercise the real ModelStageExecutor/journal seam.
The provider is an offline wire fixture; a returned object is still only a
candidate and these tests do not assert research-quality admission.
"""
from __future__ import annotations

import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import (
    ModelStageCancelled,
    ModelStageExecutor,
    ModelStageRequest,
    SynthesisContextLimitError,
)
from openthesis.resilience.contracts import RecoveryLimits, digest
from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_text_format_recovery import OfflineWire


_SYSTEM = "完整系统提示：保留所有原始研究材料"
_USER = "完整用户材料：revenue=100；evidence=source:annual"


def _bad(kind: str, **extra):
    return {
        "structured_output_valid": False,
        "_response_error": kind,
        "narrative": "malformed response",
        **extra,
    }


def _prepare(store, *, journal=None, request=None):
    journal = journal or _journal(store)
    request = request or ModelStageRequest("analysis", _SYSTEM, _USER)
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    return journal, request, generation


def _execute(store, provider, *, journal=None, request=None):
    journal, request, generation = _prepare(store, journal=journal, request=request)
    with journal.bind("analysis", generation):
        result = ModelStageExecutor(
            provider,
            ImmediateScheduler(),
            cancel_check=journal.cancel_check,
            request_journal=journal,
        ).execute(request)
    return result


def _request_row(store):
    with store.connect() as db:
        return db.execute("SELECT * FROM model_request_journal").fetchone()


def _action_row(store):
    with store.connect() as db:
        return db.execute("SELECT * FROM recovery_actions ORDER BY started_at DESC LIMIT 1").fetchone()


def test_invalid_json_repair_preserves_original_prompt_and_bad_result(tmp_path):
    store = _store(tmp_path)
    original = _bad("invalid_json")
    repaired = {"claims": [{"text": "verified candidate"}], "structured_output_valid": True}
    provider = OfflineWire([original, repaired])

    assert _execute(store, provider) == repaired
    assert len(provider.wires) == 2
    first, second = provider.wires
    assert second["system_prompt"] == first["system_prompt"] == _SYSTEM
    assert second["user_prompt"].startswith(_USER)
    assert len(second["user_prompt"]) > len(_USER)
    assert "malformed response" not in second["user_prompt"]
    assert second["configured_model_id"] == first["configured_model_id"] == "offline-model"
    assert second["json_mode"] is True and first["json_mode"] is True
    with store.connect() as db:
        action = db.execute("SELECT * FROM recovery_actions").fetchone()
        request = db.execute("SELECT * FROM model_request_journal").fetchone()
    assert action is not None
    payload = json.loads(action["result_json"])["candidate"]
    assert payload["original_result"] == original
    assert payload["result"] == repaired
    assert request["state"] == "candidate"


@pytest.mark.parametrize("error_kind", ["empty_content", "invalid_shape"])
def test_repairable_empty_or_invalid_shape_response_gets_one_bounded_repair(tmp_path, error_kind):
    store = _store(tmp_path)
    provider = OfflineWire([
        _bad(error_kind),
        {"claims": [{"text": "repaired"}], "structured_output_valid": True},
    ])

    result = _execute(store, provider)
    assert result["structured_output_valid"] is True
    assert len(provider.wires) == 2
    assert provider.wires[1]["user_prompt"].startswith(_USER)


def test_second_invalid_output_is_not_repaired_again_and_both_versions_are_audited(tmp_path):
    store = _store(tmp_path)
    first_bad = _bad("invalid_json")
    second_bad = _bad("invalid_shape")
    provider = OfflineWire([first_bad, second_bad, {"structured_output_valid": True}])

    result = _execute(store, provider)
    assert result == {**first_bad, "_output_repair": {
        "state": "pending", "code": "MODEL_OUTPUT_REPAIR_INVALID",
    }}
    assert len(provider.wires) == 2
    action = _action_row(store)
    assert action is not None
    payload = json.loads(action["result_json"])["candidate"]
    assert payload["original_result"] == first_bad
    assert payload["result"] == second_bad
    request = _request_row(store)
    assert request["state"] == "candidate"
    assert json.loads(request["candidate_json"]) == first_bad
    assert request["candidate_digest"] == digest(first_bad)


def test_unknown_repair_timeout_is_not_blindly_retried_after_reconstruction(tmp_path):
    store = _store(tmp_path)
    provider = OfflineWire([_bad("invalid_json"), TimeoutError("response status unknown")])

    first = _execute(store, provider)
    assert first["_output_repair"] == {"state": "pending", "code": "MODEL_SUBMISSION_UNKNOWN"}
    second = _execute(store, provider)
    assert second["_output_repair"] == first["_output_repair"]
    assert len(provider.wires) == 2


def test_zero_repair_calls_preserves_bad_response_without_compensation(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    journal.repair_limits = RecoveryLimits(calls=0, transient_retries=0)
    original = _bad("invalid_json")
    provider = OfflineWire([original])

    result = _execute(store, provider, journal=journal)
    assert result["_output_repair"] == {
        "state": "pending", "code": "RECOVERY_INCIDENT_BUDGET_EXHAUSTED",
    }
    assert len(provider.wires) == 1
    request = _request_row(store)
    assert request["state"] == "candidate"
    assert json.loads(request["candidate_json"]) == original
    assert request["candidate_digest"] == digest(original)


@pytest.mark.parametrize("bad", [
    _bad("duplicate_json_key"),
    _bad("non_finite_json_number"),
    _bad("invalid_json", _response_meta={"finish_reason": "refusal"}),
    _bad("empty_content", _response_meta={"finish_reason": "content_filter"}),
])
def test_duplicate_nonfinite_refusal_or_content_filter_never_opens_repair(tmp_path, bad):
    store = _store(tmp_path)
    provider = OfflineWire([bad, {"structured_output_valid": True}])

    result = _execute(store, provider)
    assert result == bad
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


@pytest.mark.parametrize("flag", ["true", 1])
def test_non_boolean_structured_output_flag_in_repair_response_never_promotes(tmp_path, flag):
    store = _store(tmp_path)
    original = _bad("invalid_json")
    bad = {"structured_output_valid": flag, "_response_error": "invalid_shape"}
    provider = OfflineWire([original, bad, {"structured_output_valid": True}])

    result = _execute(store, provider)
    assert result["_output_repair"] == {
        "state": "pending", "code": "MODEL_OUTPUT_REPAIR_INVALID",
    }
    assert len(provider.wires) == 2
    request = _request_row(store)
    assert request["state"] == "candidate"
    assert json.loads(request["candidate_json"]) == original


def test_cancel_during_repair_keeps_paid_candidate_pending_without_promotion(tmp_path):
    store = _store(tmp_path)
    original = _bad("invalid_json")
    paid = {"structured_output_valid": True, "claims": [{"text": "paid"}]}

    def cancel_before_return():
        with store.connect() as db:
            row = db.execute("SELECT payload_json FROM research_runs WHERE run_id=?", ("journal-run",)).fetchone()
            payload = json.loads(row[0])
            payload["status"] = "cancelled"
            db.execute("UPDATE research_runs SET status='cancelled',payload_json=? WHERE run_id=?",
                       (json.dumps(payload, ensure_ascii=False), "journal-run"))
        return paid

    provider = OfflineWire([original, cancel_before_return])
    with pytest.raises(ModelStageCancelled):
        _execute(store, provider)
    assert len(provider.wires) == 2
    action = _action_row(store)
    assert action is not None and action["result_json"] is not None
    payload = json.loads(action["result_json"])["candidate"]
    assert payload["original_result"] == original
    assert payload["result"] == paid
    request = _request_row(store)
    assert request["state"] == "candidate"
    assert json.loads(request["candidate_json"]) == original
    assert request["candidate_digest"] == digest(original)


def test_generation_replacement_during_repair_keeps_paid_candidate_unpromoted(tmp_path):
    store = _store(tmp_path)
    original = _bad("invalid_json")
    stale = {"structured_output_valid": True, "claims": [{"text": "stale"}]}

    def replace_generation():
        with store.connect() as db:
            db.execute("UPDATE model_stage_request_heads SET generation=generation+1 WHERE run_id=? AND stage_id=?",
                       ("journal-run", "analysis"))
        return stale

    provider = OfflineWire([original, replace_generation])
    result = _execute(store, provider)
    assert result["_output_repair"]["state"] == "pending"
    assert result["_output_repair"]["code"] in {
        "MODEL_REQUEST_GENERATION_STALE", "RECOVERY_PLAN_STALE", "MODEL_CANCELLED",
    }
    assert len(provider.wires) == 2
    action = _action_row(store)
    assert action is not None and action["result_json"] is not None
    payload = json.loads(action["result_json"])["candidate"]
    assert payload["original_result"] == original
    assert payload["result"] == stale
    request = _request_row(store)
    assert request["state"] == "candidate"
    assert json.loads(request["candidate_json"]) == original
    assert request["candidate_digest"] == digest(original)


def test_repair_context_budget_fails_closed_without_truncating_original_material(tmp_path):
    store = _store(tmp_path)
    base_request = ModelStageRequest("analysis", _SYSTEM, _USER)
    request = ModelStageRequest("analysis", _SYSTEM, _USER,
                               max_input_bytes=base_request.input_bytes + 1)
    provider = OfflineWire([_bad("invalid_json"), {"structured_output_valid": True}])

    result = _execute(store, provider, request=request)
    assert result["_output_repair"] == {
        "state": "pending", "code": "MODEL_OUTPUT_REPAIR_CONTEXT_REQUIRED",
    }
    assert len(provider.wires) == 1
    assert provider.wires[0]["user_prompt"] == _USER
    request_row = _request_row(store)
    assert request_row["state"] == "candidate"
    assert json.loads(request_row["candidate_json"]) == _bad("invalid_json")
    assert request_row["candidate_digest"] == digest(_bad("invalid_json"))
