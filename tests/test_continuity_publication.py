"""Transactional publication boundaries for visible continuity notices."""

from __future__ import annotations

import json

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from openthesis.research_continuity import (
    ResearchContinuityCoordinator,
    StageAttempt,
    StageOutcome,
)
from openthesis.storage import Storage, StorageCorruptionError


def _run(run_id: str = "continuity-publication-run") -> ResearchRun:
    return ResearchRun(
        run_id=run_id,
        company=DEMO_COMPANY,
        workflow_id="continuity-publication-test",
        research_pack_id="test-pack",
        research_pack_version="1",
        provider_id="none",
        model_id="",
        data_as_of=utc_now_iso(),
    )


def _fixture(tmp_path, run_id: str = "continuity-publication-run"):
    storage = Storage(tmp_path / "store")
    storage.save_company(DEMO_COMPANY)
    coordinator = ResearchContinuityCoordinator(storage)
    run = coordinator.start(_run(run_id))
    storage.save_artifact(
        ResearchArtifact(
            artifact_id=f"{run_id}:base-report",
            run_id=run_id,
            artifact_type="research-report",
            title="Stable report",
            content={"mode": "synthesized", "report": {"thesis": "stable"}},
        )
    )
    return storage, coordinator, run


def _snapshot(storage: Storage, run_id: str) -> dict:
    with storage.connect() as db:
        run = db.execute(
            "SELECT payload_json,status,started_at,completed_at FROM research_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        artifacts = db.execute(
            "SELECT artifact_id,run_id,artifact_type,title,payload_json,model_id,agent_id,created_at "
            "FROM artifacts WHERE run_id=? ORDER BY artifact_id",
            (run_id,),
        ).fetchall()
        attempts = db.execute(
            "SELECT attempt_id,run_id,stage,attempt,outcome,error_code,message,diagnostics_json,created_at,action_id "
            "FROM research_stage_attempts WHERE run_id=? ORDER BY attempt_id",
            (run_id,),
        ).fetchall()
        revisions = db.execute(
            "SELECT revision_id,run_id,parent_revision_id,generation,payload_json,content_sha256,created_at "
            "FROM report_revisions WHERE run_id=? ORDER BY generation",
            (run_id,),
        ).fetchall()
        latest = db.execute(
            "SELECT run_id,revision_id,generation FROM latest_report_revisions WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return {
        "run": tuple(run) if run is not None else None,
        "artifacts": [tuple(row) for row in artifacts],
        "attempts": [tuple(row) for row in attempts],
        "revisions": [tuple(row) for row in revisions],
        "latest": tuple(latest) if latest is not None else None,
    }


def _abort_on_stage_insert(storage: Storage) -> None:
    with storage.connect() as db:
        db.execute(
            """CREATE TRIGGER injected_continuity_stage_failure
               BEFORE INSERT ON research_stage_attempts
               BEGIN SELECT RAISE(ABORT, 'INJECTED_CONTINUITY_FAILURE'); END"""
        )


def _abort_on_run_update(storage: Storage) -> None:
    with storage.connect() as db:
        db.execute(
            """CREATE TRIGGER injected_continuity_run_failure
               BEFORE UPDATE ON research_runs
               BEGIN SELECT RAISE(ABORT, 'INJECTED_CONTINUITY_FAILURE'); END"""
        )


def _preserve(coordinator, run):
    return coordinator.preserve_visible_result(
        run,
        stage="growth-opportunities",
        outcome=StageOutcome.WAITING_RETRYABLE,
        error_code="INJECTED_CONTINUITY_FAILURE",
        message="Retry the failed stage",
        retryable=True,
    )


def test_stage_attempt_failure_is_atomic_and_keeps_run_artifacts_and_revision(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    before_object = run.to_dict()
    before = _snapshot(storage, run.run_id)
    _abort_on_stage_insert(storage)

    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        _preserve(coordinator, run)

    assert run.to_dict() == before_object
    assert _snapshot(storage, run.run_id) == before


def test_run_update_failure_is_atomic_and_does_not_mutate_in_memory_run(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    before_object = run.to_dict()
    before = _snapshot(storage, run.run_id)
    _abort_on_run_update(storage)

    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        _preserve(coordinator, run)

    assert run.to_dict() == before_object
    assert _snapshot(storage, run.run_id) == before


def test_stale_run_object_cannot_overwrite_new_database_inputs(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    updated = run.to_dict()
    updated.update(
        {
            "model_configuration": {"model": "new-model", "configuration_version": 2},
            "market_snapshot": {"price": 123.45, "currency": "USD"},
            "data_snapshot": {"financial_generation": "generation-new"},
            "future_extension": {"kept": True},
        }
    )
    with storage.connect() as db:
        db.execute(
            "UPDATE research_runs SET payload_json=? WHERE run_id=?",
            (json.dumps(updated, ensure_ascii=False), run.run_id),
        )

    notice = _preserve(coordinator, run)
    stored = storage.get_run(run.run_id)
    payload = json.loads(stored["payload_json"])
    assert notice.artifact_type == "continuity-notice"
    assert stored["status"] == RunStatus.PARTIAL.value
    assert payload["model_configuration"] == updated["model_configuration"]
    assert payload["market_snapshot"] == updated["market_snapshot"]
    assert payload["data_snapshot"] == updated["data_snapshot"]
    assert payload["future_extension"] == {"kept": True}


def test_success_is_atomic_and_repeating_notice_is_idempotent(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    before = _snapshot(storage, run.run_id)

    first = _preserve(coordinator, run)
    after_first = _snapshot(storage, run.run_id)
    artifacts = storage.get_artifacts(run.run_id)
    notices = [item for item in artifacts if item["artifact_type"] == "continuity-notice"]
    assert first.artifact_type == "continuity-notice"
    assert len(notices) == 1
    assert len(after_first["attempts"]) == len(before["attempts"]) + 1
    assert after_first["revisions"] == before["revisions"]
    assert after_first["latest"] == before["latest"]
    assert storage.get_run(run.run_id)["status"] == RunStatus.PARTIAL.value

    _preserve(coordinator, run)
    artifacts_after_repeat = storage.get_artifacts(run.run_id)
    notices_after_repeat = [
        item for item in artifacts_after_repeat if item["artifact_type"] == "continuity-notice"
    ]
    assert len(notices_after_repeat) == 1
    assert _snapshot(storage, run.run_id)["revisions"] == before["revisions"]


@pytest.mark.parametrize(
    "terminal_status", [RunStatus.COMPLETED, RunStatus.CANCELLED],
    ids=["completed", "cancelled"],
)
def test_late_notice_does_not_downgrade_terminal_run_or_report_revision(
    tmp_path, terminal_status
):
    storage, coordinator, run = _fixture(tmp_path)
    completed_at = "2026-10-06T12:00:00+00:00"
    completed_payload = run.to_dict()
    completed_payload["status"] = terminal_status.value
    completed_payload["completed_at"] = completed_at
    with storage.connect() as db:
        db.execute(
            "UPDATE research_runs SET payload_json=?, status=?, completed_at=? WHERE run_id=?",
            (
                json.dumps(completed_payload, ensure_ascii=False),
            terminal_status.value,
                completed_at,
                run.run_id,
            ),
        )
    before = _snapshot(storage, run.run_id)

    _preserve(coordinator, run)

    stored = storage.get_run(run.run_id)
    assert stored["status"] == terminal_status.value
    assert stored["completed_at"] == completed_at
    assert run.status == terminal_status
    assert run.completed_at == completed_at
    assert _snapshot(storage, run.run_id)["revisions"] == before["revisions"]
    assert len([item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "continuity-notice"]) == 1


def test_invalid_continuity_identity_is_rejected_without_any_write(tmp_path):
    storage, _coordinator, run = _fixture(tmp_path)
    before = _snapshot(storage, run.run_id)
    attempt = StageAttempt(
        run_id=run.run_id,
        stage="growth-opportunities",
        outcome=StageOutcome.WAITING_RETRYABLE,
    )
    bad_artifact = ResearchArtifact(
        artifact_id="wrong:continuity-notice",
        run_id="wrong-run",
        artifact_type="continuity-notice",
        title="Wrong",
        content={},
    )
    with pytest.raises(ValueError, match="CONTINUITY_RESULT_INVALID"):
        storage.save_continuity_result(
            run.run_id, bad_artifact, attempt, message="bad", completed_at=utc_now_iso()
        )
    valid_artifact = ResearchArtifact(
        artifact_id=f"{run.run_id}:invalid-attempt",
        run_id=run.run_id,
        artifact_type="continuity-notice",
        title="Wrong attempt",
        content={},
    )
    wrong_attempt = StageAttempt(
        run_id="wrong-run",
        stage="growth-opportunities",
        outcome=StageOutcome.WAITING_RETRYABLE,
    )
    with pytest.raises(ValueError, match="CONTINUITY_RESULT_INVALID"):
        storage.save_continuity_result(
            run.run_id, valid_artifact, wrong_attempt,
            message="bad", completed_at=utc_now_iso()
        )
    assert _snapshot(storage, run.run_id) == before


@pytest.mark.parametrize("corruption", ["unknown_status", "payload_run_id"], ids=["status", "run-id"])
def test_corrupt_persisted_run_shape_is_rejected_without_any_write(tmp_path, corruption):
    storage, _coordinator, run = _fixture(tmp_path)
    with storage.connect() as db:
        row = db.execute(
            "SELECT payload_json,status FROM research_runs WHERE run_id=?", (run.run_id,)
        ).fetchone()
        payload = json.loads(row["payload_json"])
        if corruption == "unknown_status":
            payload["status"] = "not-a-run-status"
            db.execute(
                "UPDATE research_runs SET payload_json=?,status=? WHERE run_id=?",
                (json.dumps(payload, ensure_ascii=False), "not-a-run-status", run.run_id),
            )
        else:
            payload["run_id"] = "different-run"
            db.execute(
                "UPDATE research_runs SET payload_json=? WHERE run_id=?",
                (json.dumps(payload, ensure_ascii=False), run.run_id),
            )
    before = _snapshot(storage, run.run_id)
    attempt = StageAttempt(
        run_id=run.run_id,
        stage="growth-opportunities",
        outcome=StageOutcome.WAITING_RETRYABLE,
    )
    artifact = ResearchArtifact(
        artifact_id=f"{run.run_id}:corrupt-shape",
        run_id=run.run_id,
        artifact_type="continuity-notice",
        title="Corrupt shape",
        content={},
    )
    with pytest.raises(StorageCorruptionError, match="CONTINUITY_RUN_PAYLOAD_INVALID"):
        storage.save_continuity_result(
            run.run_id, artifact, attempt, message="must reject", completed_at=utc_now_iso()
        )
    assert _snapshot(storage, run.run_id) == before


def test_artifact_identity_distinguishes_boolean_from_number(tmp_path):
    storage, _coordinator, run = _fixture(tmp_path)
    original = ResearchArtifact(
        artifact_id=f"{run.run_id}:typed-artifact",
        run_id=run.run_id,
        artifact_type="diagnostic",
        title="Typed",
        content={"value": True},
    )
    storage.save_artifact(original)
    before = _snapshot(storage, run.run_id)
    conflicting = ResearchArtifact(
        artifact_id=original.artifact_id,
        run_id=run.run_id,
        artifact_type=original.artifact_type,
        title=original.title,
        content={"value": 1},
    )
    with pytest.raises(RuntimeError, match="ARTIFACT_IDENTITY_CONFLICT"):
        storage.save_artifact(conflicting)
    assert _snapshot(storage, run.run_id) == before


def test_public_artifact_save_treats_tuple_and_list_as_same_canonical_content(tmp_path):
    storage, _coordinator, run = _fixture(tmp_path)
    tuple_artifact = ResearchArtifact(
        artifact_id=f"{run.run_id}:tuple-list",
        run_id=run.run_id,
        artifact_type="diagnostic",
        title="Tuple/list",
        content={"values": (1, 2)},
    )
    storage.save_artifact(tuple_artifact)
    storage.save_artifact(
        ResearchArtifact(
            artifact_id=tuple_artifact.artifact_id,
            run_id=run.run_id,
            artifact_type="diagnostic",
            title="Tuple/list",
            content={"values": [1, 2]},
        )
    )
    artifacts = [item for item in storage.get_artifacts(run.run_id)
                 if item["artifact_id"] == tuple_artifact.artifact_id]
    assert len(artifacts) == 1
    assert artifacts[0]["content"] == {"values": [1, 2]}


def test_preserve_interruption_loads_current_metadata_and_keeps_unknown_extensions(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    updated = run.to_dict()
    updated.update(
        {
            "model_configuration": {"model": "current-model"},
            "market_snapshot": {"price": 10.5, "currency": "USD"},
            "data_snapshot": {"generation": "current-generation"},
            "future_extension": {"kept": "verbatim"},
        }
    )
    with storage.connect() as db:
        db.execute(
            "UPDATE research_runs SET payload_json=? WHERE run_id=?",
            (json.dumps(updated, ensure_ascii=False), run.run_id),
        )

    loaded = coordinator.preserve_interruption(
        run.run_id,
        stage="filing-download",
        outcome=StageOutcome.WAITING_RETRYABLE,
        error_code="PROCESS_INTERRUPTED",
        message="Resume after interruption",
    )
    assert loaded is not None
    assert loaded.model_configuration == updated["model_configuration"]
    assert loaded.market_snapshot == updated["market_snapshot"]
    assert loaded.data_snapshot == updated["data_snapshot"]
    stored = storage.get_run(run.run_id)
    payload = json.loads(stored["payload_json"])
    assert payload["future_extension"] == updated["future_extension"]


def test_preserve_interruption_rejects_mismatched_or_missing_run_without_report(tmp_path):
    storage, coordinator, run = _fixture(tmp_path)
    before = _snapshot(storage, run.run_id)
    with pytest.raises(ValueError, match="CONTINUITY_RUN_IDENTITY_CONFLICT"):
        coordinator.preserve_interruption(
            "other-run",
            run=run,
            stage="filing-download",
            outcome=StageOutcome.WAITING_RETRYABLE,
            error_code="PROCESS_INTERRUPTED",
            message="Resume after interruption",
        )
    assert _snapshot(storage, run.run_id) == before
    assert coordinator.preserve_interruption(
        "missing-run",
        stage="filing-download",
        outcome=StageOutcome.WAITING_RETRYABLE,
        error_code="PROCESS_INTERRUPTED",
        message="Resume after interruption",
    ) is None
    assert _snapshot(storage, run.run_id) == before
