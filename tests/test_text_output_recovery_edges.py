"""Returned-output recovery must not bypass evidence or multiply wire attempts."""
from copy import deepcopy
import json
from threading import Event, Thread

import pytest

from openthesis.demo import demo_facts
from openthesis.providers import ProviderError
from openthesis.research import build_fact_evidence, verify_agent_output
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.executor import StageExecutor, StageValidation
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.resilience.contracts import digest

from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_research_workflow import _verified_demo_revenue_claim
from test_text_format_recovery import OfflineWire, format_rejection
from test_text_output_recovery import _bad, _execute, _prepare, _request_row, _action_row


@pytest.mark.parametrize("initial", [_bad("invalid_json"), format_rejection()])
def test_explicit_single_attempt_has_no_nested_recovery_wire_even_with_journal(tmp_path, initial):
    store, provider = _store(tmp_path), OfflineWire([initial])
    journal, request, generation = _prepare(store)
    with journal.bind("analysis", generation):
        executor = ModelStageExecutor(provider, ImmediateScheduler(), request_journal=journal)
        if isinstance(initial, BaseException):
            with pytest.raises(ProviderError) as caught:
                executor.execute(request, single_attempt=True)
            assert caught.value is initial
        else:
            assert executor.execute(request, single_attempt=True) == initial
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


def test_original_response_cas_distinguishes_true_from_one_and_preserves_returned_repair(tmp_path):
    store = _store(tmp_path)
    original = {**_bad("invalid_json"), "original_marker": True}
    repaired = {"structured_output_valid": True, "claims": []}
    changed = {**original, "original_marker": 1}

    def change_response_then_return():
        with store.connect() as db:
            db.execute("UPDATE model_request_journal SET candidate_json=?,candidate_digest=?",
                       (json.dumps(changed), digest(changed)))
        return repaired

    provider = OfflineWire([original, change_response_then_return])
    result = _execute(store, provider)
    assert result["_output_repair"]["state"] == "pending"
    assert result["_output_repair"]["code"] == "RECOVERY_MODEL_REQUEST_COMMIT_CONTRACT_INVALID"
    assert len(provider.wires) == 2
    assert json.loads(_request_row(store)["candidate_json"]) == changed
    saved = json.loads(_action_row(store)["result_json"])["candidate"]
    assert saved["original_digest"] == digest(original)
    assert saved["original_result"]["original_marker"] is True
    assert saved["result"] == repaired


def test_publication_failure_revalidates_paid_repair_without_another_wire(tmp_path):
    store = _store(tmp_path)
    original = _bad("invalid_json")
    repaired = {"structured_output_valid": True, "claims": []}
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_output_commit BEFORE UPDATE ON model_request_journal
                    WHEN OLD.state='candidate' AND NEW.candidate_digest != OLD.candidate_digest
                    BEGIN SELECT RAISE(ABORT, 'fixture_write_failure'); END""")
    provider = OfflineWire([original, repaired])
    result = _execute(store, provider)
    assert result["_output_repair"]["state"] == "pending"
    assert result["_output_repair"]["code"] == "MODEL_REPAIR_LOCAL_PROCESSING_FAILED"
    assert json.loads(_request_row(store)["candidate_json"]) == original
    action = _action_row(store)
    assert action["state"] == "validating"
    assert json.loads(action["result_json"])["candidate"]["result"] == repaired
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_output_commit")
    assert _execute(store, provider) == repaired
    assert len(provider.wires) == 2
    assert json.loads(_action_row(store)["usage_json"])["calls"] == 1


def test_concurrent_same_response_repair_submits_only_one_additional_wire(tmp_path):
    store, started, release = _store(tmp_path), Event(), Event()
    repaired = {"structured_output_valid": True, "claims": []}
    outcomes = []

    def block_response():
        started.set()
        assert release.wait(10)
        return repaired

    provider = OfflineWire([_bad("invalid_json"), block_response])

    def first():
        try:
            outcomes.append(_execute(store, provider))
        except BaseException as error:
            outcomes.append(error)

    worker = Thread(target=first)
    worker.start()
    try:
        assert started.wait(10)
        racing = _execute(store, provider)
        assert racing["_output_repair"] == {
            "state": "pending", "code": "MODEL_REQUEST_IN_PROGRESS",
        }
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and outcomes == [repaired]
    assert len(provider.wires) == 2


@pytest.mark.parametrize("claim_kind", ["supported", "wrong_value", "missing_evidence"])
def test_protocol_repair_still_requires_the_real_research_evidence_validator(tmp_path, claim_kind):
    store = _store(tmp_path)
    journal = _journal(store)
    registry = _registry(("analysis", ()))
    generation = journal.prepare(registry, "frozen-material")["analysis"]
    claim = deepcopy(_verified_demo_revenue_claim())
    if claim_kind == "wrong_value":
        claim["value"] = 9_999_999_999.0
        claim["text"] = "Annual revenue was 9,999,999,999 USD."
    elif claim_kind == "missing_evidence":
        claim["evidence_ids"] = ["fact:nonexistent"]
    repaired = {"structured_output_valid": True, "claims": [claim]}
    provider = OfflineWire([_bad("invalid_json"), repaired])
    records = build_fact_evidence(demo_facts())
    record_map = {item["evidence_id"]: item for item in records}

    def produce(_spec, _inputs, _dependencies):
        with journal.bind("analysis", generation):
            return ModelStageExecutor(provider, ImmediateScheduler(), request_journal=journal).execute(
                ModelStageRequest("analysis", "complete evidence rules", json.dumps(records)))

    def verify(_spec, candidate, _inputs):
        proof = verify_agent_output(candidate, set(record_map), "en", record_map)
        return StageValidation(proof["passed"], candidate if proof["passed"] else {},
                               "completed_verified" if proof["passed"] else "failed_verification",
                               tuple(proof["issues"]), proof)

    executor = StageExecutor({"handler": produce}, {"validator": verify},
                            candidate_verifier=DurableStageVerifier(store, "journal-run",
                                request_generations={"analysis": generation}))
    result = executor.execute(registry, {"frozen_records": records})["analysis"]
    assert len(provider.wires) == 2 and result.candidate == repaired
    assert result.has_candidate and result.candidate_digest == digest(repaired)
    assert result.validation.passed is (claim_kind == "supported")
    if claim_kind != "supported":
        assert result.validation.trusted_output == {}
        assert result.validation.issues
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM report_revisions").fetchone()[0] == 0
