from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from openthesis.domain import Company, ResearchRun
from openthesis.research_execution.durable import DurableStageVerifier
from openthesis.research_execution.executor import StageExecutor, StageResult, StageValidation
from openthesis.research_execution.registry import StageRegistry, StageSpec
from openthesis.resilience.contracts import candidate_fingerprint, digest
from openthesis.storage import Storage


RUN_ID = "durable-stage-verifier-test-run"
STAGE_INPUT = digest({"source": "frozen-input"})
EXECUTION_FINGERPRINT = "test-execution-fingerprint"


@pytest.fixture
def storage() -> Storage:
    scratch = Path(__file__).resolve().parents[1] / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="durable-stage-verifier-", dir=scratch) as directory:
        store = Storage(Path(directory))
        company = Company("0000000001", "TEST", "Durable Stage Test Co.")
        store.save_company(company)
        store.save_run(ResearchRun(
            run_id=RUN_ID,
            company=company,
            workflow_id="test-workflow",
            research_pack_id="test-pack",
            research_pack_version="1",
            provider_id="offline-test",
            model_id="offline-test",
            data_as_of="2026-10-04T00:00:00+00:00",
        ))
        yield store


def _spec() -> StageSpec:
    return StageSpec(
        "financial-analyst", "1", "produce", "verify", output_contract="analysis-v1"
    )


def _result(
    spec: StageSpec,
    stage_input: str,
    candidate: dict,
    fingerprint: str,
    validation: StageValidation,
    *,
    error_code: str = "",
) -> StageResult:
    return StageResult(
        spec.stage_id,
        spec.identity,
        stage_input,
        candidate,
        validation,
        digest(candidate),
        error_code,
        execution_fingerprint=fingerprint,
        has_candidate=True,
    )


def _no_stage_checkpoint_artifact(store: Storage) -> None:
    with store.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE run_id=? AND artifact_type='stage-checkpoint'",
            (RUN_ID,),
        ).fetchone()[0]
    assert count == 0


def test_verified_candidate_proof_and_stage_artifact_commit_together(storage: Storage):
    spec = _spec()
    candidate = {"claim": "supported", "evidence": ["source:1"]}
    verifier = DurableStageVerifier(storage, RUN_ID)

    result = verifier(
        spec,
        STAGE_INPUT,
        candidate,
        EXECUTION_FINGERPRINT,
        lambda value: _result(
            spec,
            STAGE_INPUT,
            value,
            EXECUTION_FINGERPRINT,
            StageValidation(True, {"claim": "supported"}, "completed_verified"),
        ),
    )

    assert result.validation.passed
    assert result.validation.trusted_output == {"claim": "supported"}
    with storage.connect() as db:
        plan = db.execute("SELECT state FROM recovery_plans").fetchone()
        action = db.execute(
            "SELECT state,result_json,validation_json FROM recovery_actions"
        ).fetchone()
        artifacts = db.execute(
            "SELECT artifact_id,artifact_type,title,payload_json FROM artifacts WHERE run_id=?",
            (RUN_ID,),
        ).fetchall()

    assert plan["state"] == "committed"
    assert action["state"] == "committed"
    recovery_candidate = json.loads(action["result_json"])
    candidate_envelope = recovery_candidate["candidate"]
    proof = json.loads(action["validation_json"])
    assert candidate_envelope["result"]["candidate"] == candidate
    assert proof["passed"] is True
    assert proof["candidate_sha256"] == candidate_fingerprint(
        recovery_candidate["candidate"],
        recovery_candidate["artifact_ids"],
        recovery_candidate["identity"],
    )
    assert len(artifacts) == 1
    assert artifacts[0]["artifact_type"] == "stage-checkpoint"
    assert artifacts[0]["title"] == spec.stage_id
    checkpoint = json.loads(artifacts[0]["payload_json"])
    assert checkpoint["result"]["candidate"] == candidate
    assert checkpoint["result"]["validation"]["passed"] is True


def test_saved_candidate_revalidation_does_not_repeat_producer_after_verifier_error(storage: Storage):
    spec = _spec()
    registry = StageRegistry((spec,))
    producer_calls = 0
    validator_calls = 0

    def produce(_spec, _inputs, _dependencies):
        nonlocal producer_calls
        producer_calls += 1
        return {"claim": "already-produced"}

    def validate(_spec, candidate, _inputs):
        nonlocal validator_calls
        validator_calls += 1
        if validator_calls == 1:
            raise RuntimeError("temporary local verifier failure")
        return StageValidation(True, {"claim": candidate["claim"]}, "completed_verified")

    first = StageExecutor(
        {"produce": produce},
        {"verify": validate},
        candidate_verifier=DurableStageVerifier(storage, RUN_ID),
    ).execute(registry, {"source": "same"}, execution_fingerprint=EXECUTION_FINGERPRINT)
    assert first[spec.stage_id].error_code == "STAGE_VERIFIER_FAILED"
    assert first[spec.stage_id].has_candidate
    assert producer_calls == 1

    second = StageExecutor(
        {"produce": produce},
        {"verify": validate},
        candidate_verifier=DurableStageVerifier(storage, RUN_ID),
    ).execute(
        registry,
        {"source": "same"},
        previous=first,
        execution_fingerprint=EXECUTION_FINGERPRINT,
    )

    assert second[spec.stage_id].validation.passed
    assert producer_calls == 1
    assert validator_calls == 2
    with storage.connect() as db:
        saved = db.execute("SELECT result_json FROM recovery_actions").fetchone()
    assert json.loads(saved["result_json"])["candidate"]["result"]["candidate"] == {
        "claim": "already-produced"
    }


def test_snapshot_generation_change_during_verification_blocks_trusted_commit(storage: Storage):
    spec = _spec()
    candidate = {"claim": "must-not-commit"}

    def change_generation(_candidate):
        with storage.connect() as db:
            db.execute(
                "UPDATE stage_verification_snapshots SET generation=generation+1 "
                "WHERE run_id=? AND target=?",
                (RUN_ID, "stage-verification:" + spec.stage_id),
            )
        return _result(
            spec,
            STAGE_INPUT,
            candidate,
            EXECUTION_FINGERPRINT,
            StageValidation(True, {"claim": "must-not-commit"}, "completed_verified"),
        )

    result = DurableStageVerifier(storage, RUN_ID)(
        spec, STAGE_INPUT, candidate, EXECUTION_FINGERPRINT, change_generation
    )

    assert not result.validation.passed
    assert not result.validation.trusted_output
    assert result.error_code == "RECOVERY_PLAN_STALE"
    _no_stage_checkpoint_artifact(storage)
    with storage.connect() as db:
        generation = db.execute(
            "SELECT generation FROM stage_verification_snapshots WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()[0]
        states = [row[0] for row in db.execute("SELECT state FROM recovery_plans")]
    assert generation == 2
    assert "committed" not in states


def test_completed_partial_candidate_is_never_committed(storage: Storage):
    spec = _spec()
    candidate = {"claim": "partial"}
    result = DurableStageVerifier(storage, RUN_ID)(
        spec,
        STAGE_INPUT,
        candidate,
        EXECUTION_FINGERPRINT,
        lambda value: _result(
            spec,
            STAGE_INPUT,
            value,
            EXECUTION_FINGERPRINT,
            StageValidation(False, {"claim": "partial"}, "completed_partial"),
        ),
    )

    assert result.validation.state == "completed_partial"
    assert result.usable
    assert not result.validation.passed
    _no_stage_checkpoint_artifact(storage)
    with storage.connect() as db:
        plan_states = [row[0] for row in db.execute("SELECT state FROM recovery_plans")]
        action_states = [row[0] for row in db.execute("SELECT state FROM recovery_actions")]
    assert "committed" not in plan_states
    assert "committed" not in action_states


class SimulatedProcessInterruption(BaseException):
    pass


def test_interrupted_validation_resumes_saved_candidate_after_lease_expiry(storage: Storage):
    spec = _spec()
    candidate = {"claim": "resume-exact-candidate"}
    verifier = DurableStageVerifier(storage, RUN_ID)

    def interrupted(_candidate):
        raise SimulatedProcessInterruption()

    with pytest.raises(SimulatedProcessInterruption):
        verifier(spec, STAGE_INPUT, candidate, EXECUTION_FINGERPRINT, interrupted)

    with storage.connect() as db:
        action = db.execute("SELECT state,result_json FROM recovery_actions").fetchone()
        lease = db.execute(
            "SELECT plan_id FROM recovery_target_leases WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()
        db.execute(
            "UPDATE recovery_target_leases SET lease_until=? WHERE run_id=?",
            (time.time() - 1, RUN_ID),
        )
    assert action["state"] == "validating"
    assert json.loads(action["result_json"])["candidate"]["result"]["candidate"] == candidate
    assert lease is not None

    resumed = verifier(
        spec,
        STAGE_INPUT,
        candidate,
        EXECUTION_FINGERPRINT,
        lambda value: _result(
            spec,
            STAGE_INPUT,
            value,
            EXECUTION_FINGERPRINT,
            StageValidation(True, {"claim": "resume-exact-candidate"}, "completed_verified"),
        ),
    )

    assert resumed.validation.passed
    with storage.connect() as db:
        action = db.execute("SELECT result_json,validation_json,state FROM recovery_actions").fetchone()
        artifacts = db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE run_id=? AND artifact_type='stage-checkpoint'",
            (RUN_ID,),
        ).fetchone()[0]
    assert action["state"] == "committed"
    assert json.loads(action["result_json"])["candidate"]["result"]["candidate"] == candidate
    assert json.loads(action["validation_json"])["passed"] is True
    assert artifacts == 1


def test_changing_candidate_for_same_input_keeps_shared_incident_budget(storage: Storage):
    spec = _spec()
    verifier = DurableStageVerifier(storage, RUN_ID)
    seen = []

    def reject(value):
        seen.append(value["candidate_number"])
        return _result(
            spec,
            STAGE_INPUT,
            value,
            EXECUTION_FINGERPRINT,
            StageValidation(False, state="failed_verification", issues=("not_verified",)),
        )

    for number in range(8):
        result = verifier(
            spec,
            STAGE_INPUT,
            {"candidate_number": number},
            EXECUTION_FINGERPRINT,
            reject,
        )
        assert not result.validation.passed

    with storage.connect() as db:
        incident = db.execute(
            "SELECT incident_id,action_count FROM recovery_incidents"
        ).fetchone()
    assert incident["action_count"] == 8

    exhausted = verifier(
        spec,
        STAGE_INPUT,
        {"candidate_number": 8},
        EXECUTION_FINGERPRINT,
        reject,
    )

    assert exhausted.error_code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"
    assert seen == list(range(8))
    with storage.connect() as db:
        final = db.execute(
            "SELECT incident_id,action_count FROM recovery_incidents"
        ).fetchone()
        snapshots = db.execute(
            "SELECT generation FROM stage_verification_snapshots WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()
    assert final["incident_id"] == incident["incident_id"]
    assert final["action_count"] == 8
    assert snapshots["generation"] == 9
    _no_stage_checkpoint_artifact(storage)
