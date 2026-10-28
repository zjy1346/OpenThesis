"""Lifecycle updates must not publish partial state or overwrite newer inputs."""
import json

import pytest

import test_continuity_publication as publication_fixture
from openthesis.demo import DEMO_COMPANY
from openthesis.disclosure_coverage import DisclosureCoveragePlanner
from openthesis.domain import RunStatus
from openthesis.research_continuity import ResearchContinuityCoordinator
from openthesis.storage import Storage


def test_start_failure_never_publishes_run_or_mutates_caller(tmp_path):
    store = Storage(tmp_path / "start")
    store.save_company(DEMO_COMPANY)
    publication_fixture._abort_on_stage_insert(store)
    run = publication_fixture._run("start-atomic")
    before = run.to_dict()
    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        ResearchContinuityCoordinator(store).start(run)
    assert store.get_run(run.run_id) is None
    assert run.to_dict() == before


def test_cancel_stage_failure_rolls_back_run_and_preserves_memory(tmp_path):
    store, coordinator, run = publication_fixture._fixture(tmp_path)
    before = publication_fixture._snapshot(store, run.run_id)
    memory = run.to_dict()
    publication_fixture._abort_on_stage_insert(store)
    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        coordinator.cancel(run, stage="financial-analysis", message="Research cancelled")
    assert publication_fixture._snapshot(store, run.run_id) == before
    assert run.to_dict() == memory


def test_coverage_stage_failure_rolls_back_metadata_and_preserves_memory(tmp_path):
    store, coordinator, run = publication_fixture._fixture(tmp_path)
    before = publication_fixture._snapshot(store, run.run_id)
    memory = run.to_dict()
    coverage = DisclosureCoveragePlanner().evaluate([], requested_count=3, authority="SEC")
    publication_fixture._abort_on_stage_insert(store)
    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        coordinator.record_disclosure_coverage(run, coverage)
    assert publication_fixture._snapshot(store, run.run_id) == before
    assert run.to_dict() == memory


@pytest.mark.parametrize("operation", ["cancel", "coverage"])
def test_lifecycle_patch_keeps_newer_inputs_and_unknown_extensions(tmp_path, operation):
    store, coordinator, run = publication_fixture._fixture(tmp_path)
    updated = run.to_dict()
    updated.update(model_configuration={"model": "newer"},
                   market_snapshot={"price": 101, "currency": "USD"},
                   data_snapshot={"financial_generations": {"filing": "new-generation"}},
                   future_extension={"kept": True})
    with store.connect() as db:
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                   (json.dumps(updated), run.run_id))
    if operation == "cancel":
        coordinator.cancel(run, stage="financial-analysis", message="Research cancelled")
    else:
        coverage = DisclosureCoveragePlanner().evaluate([], requested_count=3, authority="SEC")
        coordinator.record_disclosure_coverage(run, coverage)
    stored = json.loads(store.get_run(run.run_id)["payload_json"])
    assert stored["model_configuration"] == updated["model_configuration"]
    assert stored["market_snapshot"] == updated["market_snapshot"]
    assert stored["data_snapshot"]["financial_generations"] == {"filing": "new-generation"}
    assert stored["future_extension"] == {"kept": True}
    if operation == "coverage":
        assert stored["data_snapshot"]["disclosure_coverage"] == coverage.to_dict()
        assert run.data_snapshot == stored["data_snapshot"]
    else:
        assert run.status is RunStatus.CANCELLED


def test_late_cancel_keeps_committed_run_and_report_revision(tmp_path):
    store, coordinator, run = publication_fixture._fixture(tmp_path)
    run.status = RunStatus.COMPLETED
    run.completed_at = "2026-10-06T12:00:00+00:00"
    store.save_run(run)
    before = publication_fixture._snapshot(store, run.run_id)
    coordinator.cancel(run, stage="completed", message="Late cancel")
    stored = store.get_run(run.run_id)
    assert stored["status"] == "completed" and run.status is RunStatus.COMPLETED
    assert stored["completed_at"] == "2026-10-06T12:00:00+00:00"
    assert publication_fixture._snapshot(store, run.run_id)["revisions"] == before["revisions"]


def test_start_cannot_reset_existing_committed_identity(tmp_path):
    store, coordinator, run = publication_fixture._fixture(tmp_path)
    run.status = RunStatus.COMPLETED
    store.save_run(run)
    before = publication_fixture._snapshot(store, run.run_id)
    with pytest.raises(RuntimeError, match="RESEARCH_RUN_ALREADY_STARTED"):
        coordinator.start(publication_fixture._run(run.run_id))
    assert publication_fixture._snapshot(store, run.run_id) == before
