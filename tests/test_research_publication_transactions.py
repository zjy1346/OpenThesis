"""SQLite transaction boundaries for final research publication.

These tests exercise the repository contract only.  They do not claim that a
report is substantively correct: the completed case supplies the explicit
verification/readiness proof required by the storage collaboration seam.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus
from openthesis.research_continuity import StageAttempt, StageOutcome
from openthesis.storage import Storage
from openthesis.storage_research_publication import ResearchPublicationConflict


def _run(run_id: str = "publication-run", *, status: RunStatus = RunStatus.RUNNING) -> ResearchRun:
    run = ResearchRun(
        run_id, DEMO_COMPANY, "workflow", "pack", "1", "gateway", "model-1",
        "2026-10-07", status,
    )
    run.errors = ["original-error"]
    run.model_configuration = {"configuration_version": "cfg-1", "api_key": "test-only"}
    run.research_configuration = {"history_years": 5}
    run.data_snapshot = {"facts_generation": "generation-1"}
    return run


def _report(run: ResearchRun, suffix: str, *, artifact_id: str | None = None) -> ResearchArtifact:
    return ResearchArtifact(
        artifact_id or f"{run.run_id}:report:{suffix}", run.run_id, "research-report",
        "Research report", {
            "report": {"claims": [{"text": suffix}]},
            "verification": {"passed": True},
            "readiness": {"complete": True},
        }, model_id=run.model_id, agent_id="synthesis",
    )


def _attempt(run: ResearchRun, *, stage: str = "final-synthesis") -> StageAttempt:
    return StageAttempt(run.run_id, stage, StageOutcome.COMPLETE, attempt=1)


def _thesis_factory(run: ResearchRun):
    def factory(thesis: dict) -> ResearchArtifact:
        return ResearchArtifact(
            f"{run.run_id}:thesis-snapshot:{thesis['thesis_version_id']}",
            run.run_id, "thesis-snapshot", "Thesis snapshot",
            {"thesis": thesis["content"]}, model_id=run.model_id, agent_id="thesis",
        )

    return factory


def _fixture(tmp_path, *, status: RunStatus = RunStatus.RUNNING, prior: bool = False):
    store = Storage(tmp_path / "store")
    store.save_company(DEMO_COMPANY)
    run = _run(status=status)
    store.save_run(run)
    if prior:
        store.save_artifact(_report(run, "previous"))
    return store, run


def _state(store: Storage, run_id: str) -> dict:
    with store.connect() as db:
        def rows(sql: str, params=()):
            return [tuple(row) for row in db.execute(sql, params).fetchall()]

        return {
            "run": rows(
                "SELECT payload_json,status,completed_at FROM research_runs WHERE run_id=?",
                (run_id,),
            ),
            "artifacts": rows(
                "SELECT artifact_id,artifact_type,payload_json FROM artifacts "
                "WHERE run_id=? ORDER BY artifact_id", (run_id,),
            ),
            "revisions": rows(
                "SELECT revision_id,generation,payload_json,content_sha256 FROM report_revisions "
                "WHERE run_id=? ORDER BY generation", (run_id,),
            ),
            "latest": rows(
                "SELECT revision_id,generation FROM latest_report_revisions WHERE run_id=?",
                (run_id,),
            ),
            "contexts": rows(
                "SELECT revision_id,context_json,context_sha256 FROM report_revision_contexts "
                "WHERE revision_id IN (SELECT revision_id FROM report_revisions WHERE run_id=?) "
                "ORDER BY revision_id", (run_id,),
            ),
            "attempts": rows(
                "SELECT stage,attempt,outcome,error_code,message,diagnostics_json "
                "FROM research_stage_attempts WHERE run_id=? ORDER BY attempt_id", (run_id,),
            ),
            "thesis": rows(
                "SELECT thesis_version_id,content_json,run_id,created_by FROM thesis_versions "
                "WHERE run_id=? ORDER BY thesis_version_id", (run_id,),
            ),
        }


def _publish(store, run, snapshot, *, artifact=None, status=RunStatus.COMPLETED,
             cancel_check=lambda: False, thesis=False):
    artifact = artifact or _report(run, "published")
    return store.publish_research_result(
        snapshot, artifact, _attempt(run), status=status,
        completed_at="2026-10-07T12:00:00+00:00", cancel_check=cancel_check,
        thesis_content={"direction": "hold"} if thesis else None,
        thesis_created_by="research", thesis_artifact_factory=_thesis_factory(run) if thesis else None,
    )


def _add_stage(store: Storage, run: ResearchRun, stage: str, generation: int, identity: str):
    with store.connect() as db:
        db.execute(
            "INSERT INTO model_stage_request_heads(run_id,stage_id,generation,identity_digest) "
            "VALUES(?,?,?,?)", (run.run_id, stage, generation, identity),
        )


def test_capture_and_publish_preserves_unknown_extensions_and_errors(tmp_path):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload_json FROM research_runs WHERE run_id=?", (run.run_id,)
        ).fetchone()[0])
        payload["future_extension"] = {"version": 9, "opaque": [True, "x"]}
        payload["errors"] = ["old-error", "new-error"]
        db.execute(
            "UPDATE research_runs SET payload_json=? WHERE run_id=?",
            (json.dumps(payload, ensure_ascii=False), run.run_id),
        )
    saved = _publish(store, run, snapshot)
    assert saved["future_extension"] == {"version": 9, "opaque": [True, "x"]}
    assert saved["errors"] == ["old-error", "new-error"]
    assert store.get_run(run.run_id)["status"] == RunStatus.COMPLETED.value


def test_report_revision_change_rejects_and_keeps_new_head(tmp_path):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    store.save_artifact(_report(run, "newer"))
    before = _state(store, run.run_id)
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_REVISION_CONFLICT"):
        _publish(store, run, snapshot)
    assert _state(store, run.run_id) == before


def test_model_configuration_change_rejects_without_overwriting_new_input(tmp_path):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload_json FROM research_runs WHERE run_id=?", (run.run_id,)
        ).fetchone()[0])
        payload["model_configuration"] = {"configuration_version": "cfg-new", "provider": "new"}
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                   (json.dumps(payload), run.run_id))
    before = _state(store, run.run_id)
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_INPUT_CONFLICT"):
        _publish(store, run, snapshot)
    assert _state(store, run.run_id) == before
    persisted = json.loads(store.get_run(run.run_id)["payload_json"])
    assert persisted["model_configuration"]["configuration_version"] == "cfg-new"


def test_stage_generation_change_rejects(tmp_path):
    store, run = _fixture(tmp_path)
    _add_stage(store, run, "final-synthesis", 1, "identity-old")
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        db.execute("UPDATE model_stage_request_heads SET generation=2,identity_digest=? "
                   "WHERE run_id=? AND stage_id=?", ("identity-new", run.run_id, "final-synthesis"))
    before = _state(store, run.run_id)
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_STAGE_CONFLICT"):
        _publish(store, run, snapshot)
    assert _state(store, run.run_id) == before


def test_bind_stage_heads_accepts_only_explicit_prepared_generation(tmp_path):
    store, run = _fixture(tmp_path)
    _add_stage(store, run, "final-synthesis", 1, "identity-old")
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        db.execute("UPDATE model_stage_request_heads SET generation=2,identity_digest=? "
                   "WHERE run_id=? AND stage_id=?", ("identity-new", run.run_id, "final-synthesis"))
    rebound = store.bind_research_publication_stages(snapshot, {"final-synthesis": 2})
    assert rebound.stage_heads == (("final-synthesis", 2, "identity-new"),)


def test_bind_stage_heads_rejects_unlisted_changed_stage(tmp_path):
    store, run = _fixture(tmp_path)
    _add_stage(store, run, "final-synthesis", 1, "final")
    _add_stage(store, run, "growth", 1, "growth")
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        db.execute("UPDATE model_stage_request_heads SET generation=2 WHERE run_id=? AND stage_id=?",
                   (run.run_id, "growth"))
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_STAGE_CONFLICT"):
        store.bind_research_publication_stages(snapshot, {"final-synthesis": 1})


def test_bind_stage_heads_rejects_identity_change_without_generation_change(tmp_path):
    store, run = _fixture(tmp_path)
    _add_stage(store, run, "final-synthesis", 1, "identity-old")
    snapshot = store.capture_research_publication(run)
    with store.connect() as db:
        db.execute("UPDATE model_stage_request_heads SET identity_digest=? "
                   "WHERE run_id=? AND stage_id=?", ("identity-new", run.run_id, "final-synthesis"))
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_STAGE_CONFLICT"):
        store.bind_research_publication_stages(snapshot, {"final-synthesis": 1})


def test_cancel_after_report_insert_rolls_back_everything(tmp_path):
    store, run = _fixture(tmp_path, prior=True)
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    cancelled = Event()
    original = store._save_artifact_db

    def save_then_cancel(db, artifact):
        original(db, artifact)
        cancelled.set()

    store._save_artifact_db = save_then_cancel
    try:
        with pytest.raises(CancelledError, match="cancelled"):
            _publish(store, run, snapshot, cancel_check=cancelled.is_set)
    finally:
        store._save_artifact_db = original
    assert _state(store, run.run_id) == before


def test_stage_attempt_trigger_rolls_back_report_and_run(tmp_path):
    store, run = _fixture(tmp_path, prior=True)
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_publication_attempt BEFORE INSERT ON research_stage_attempts "
                   "BEGIN SELECT RAISE(ABORT, 'INJECTED_ATTEMPT_FAILURE'); END")
    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_ATTEMPT_FAILURE"):
        _publish(store, run, snapshot)
    assert _state(store, run.run_id) == before


def test_thesis_version_trigger_rolls_back_report_and_attempt(tmp_path):
    store, run = _fixture(tmp_path, prior=True)
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_thesis_version BEFORE INSERT ON thesis_versions "
                   "BEGIN SELECT RAISE(ABORT, 'INJECTED_THESIS_FAILURE'); END")
    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_THESIS_FAILURE"):
        _publish(store, run, snapshot, thesis=True)
    assert _state(store, run.run_id) == before


def test_thesis_snapshot_artifact_trigger_rolls_back_everything(tmp_path):
    store, run = _fixture(tmp_path, prior=True)
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_thesis_artifact BEFORE INSERT ON artifacts "
                   "WHEN NEW.artifact_type='thesis-snapshot' "
                   "BEGIN SELECT RAISE(ABORT, 'INJECTED_THESIS_ARTIFACT_FAILURE'); END")
    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_THESIS_ARTIFACT_FAILURE"):
        _publish(store, run, snapshot, thesis=True)
    assert _state(store, run.run_id) == before


def test_success_publishes_report_run_attempt_thesis_snapshot_and_context(tmp_path):
    store, run = _fixture(tmp_path, prior=True)
    old_revision = store.latest_report_revision(run.run_id)
    assert old_revision is not None
    snapshot = store.capture_research_publication(run)
    saved = _publish(store, run, snapshot, thesis=True)
    assert saved["status"] == RunStatus.COMPLETED.value
    assert store.get_run(run.run_id)["status"] == RunStatus.COMPLETED.value
    assert len(store.get_artifacts(run.run_id)) == 3
    assert len(store.continuity_snapshot(run.run_id)["attempts"]) == 1
    assert len(store.list_thesis_versions(DEMO_COMPANY.cik)) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM report_revisions WHERE run_id=?", (run.run_id,)).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM report_revision_contexts WHERE revision_id IN "
                          "(SELECT revision_id FROM report_revisions WHERE run_id=?)", (run.run_id,)).fetchone()[0] == 2
    assert store.latest_report_revision(run.run_id)["generation"] == 2
    assert store.latest_report_revision(run.run_id)["revision_id"] != old_revision["revision_id"]


def test_completed_requires_verification_and_readiness_proof(tmp_path):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    invalid = ResearchArtifact(
        "invalid-completed", run.run_id, "research-report", "Report", {"report": {}},
        model_id=run.model_id, agent_id="synthesis",
    )
    before = _state(store, run.run_id)
    with pytest.raises(ValueError, match="RESEARCH_PUBLICATION_RESULT_INVALID"):
        _publish(store, run, snapshot, artifact=invalid)
    assert _state(store, run.run_id) == before


@pytest.mark.parametrize("field", ["verification", "readiness"])
def test_completed_rejects_integer_one_as_boolean_proof(tmp_path, field):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    proof = {
        "verification": {"passed": True},
        "readiness": {"complete": True},
        "report": {"claims": [{"text": "not a quality assertion"}]},
    }
    proof[field] = {"passed" if field == "verification" else "complete": 1}
    invalid = ResearchArtifact(
        f"integer-proof-{field}", run.run_id, "research-report", "Report", proof,
        model_id=run.model_id, agent_id="synthesis",
    )
    before = _state(store, run.run_id)
    with pytest.raises(ValueError, match="RESEARCH_PUBLICATION_RESULT_INVALID"):
        _publish(store, run, snapshot, artifact=invalid)
    assert _state(store, run.run_id) == before


def test_cancelled_current_run_rejects_publication(tmp_path):
    store, run = _fixture(tmp_path, status=RunStatus.CANCELLED)
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    with pytest.raises(CancelledError, match="cancelled"):
        _publish(store, run, snapshot)
    assert _state(store, run.run_id) == before


def test_reusing_old_report_body_after_new_head_is_rejected(tmp_path):
    store, run = _fixture(tmp_path)
    old = _report(run, "body-a")
    store.save_artifact(old)
    store.save_artifact(_report(run, "body-b"))
    snapshot = store.capture_research_publication(run)
    before = _state(store, run.run_id)
    with pytest.raises(ResearchPublicationConflict, match="RESEARCH_PUBLICATION_REVISION_CONFLICT"):
        _publish(store, run, snapshot, artifact=old)
    assert _state(store, run.run_id) == before


def test_two_publishers_same_snapshot_allow_at_most_one_new_head(tmp_path):
    store, run = _fixture(tmp_path)
    snapshot = store.capture_research_publication(run)
    barrier = Barrier(2)

    def publish_from_worker(suffix: str):
        barrier.wait(timeout=5)
        return _publish(store, run, snapshot, artifact=_report(run, suffix))

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="publication-test") as pool:
        futures = [pool.submit(publish_from_worker, "worker-a"),
                   pool.submit(publish_from_worker, "worker-b")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("ok", future.result(timeout=10)))
            except ResearchPublicationConflict as exc:
                outcomes.append(("conflict", exc.code))
    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("conflict") == 1
    assert store.latest_report_revision(run.run_id)["generation"] == 1
    assert len(store.get_artifacts(run.run_id)) == 1
    assert len(store.continuity_snapshot(run.run_id)["attempts"]) == 1

