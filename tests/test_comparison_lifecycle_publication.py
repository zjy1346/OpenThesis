"""Peer diagnostics are additive; stale peers cannot rewrite primary inputs."""
import json
from unittest.mock import patch

import pytest

import test_continuity_publication as fixtures
from openthesis.application.research_requests import ResearchStartRequest
from openthesis.application_services import ResearchOrchestrator, ResearchComparisonCancelled
from openthesis.domain import RunStatus
from openthesis.providers import ModelConfig


def existing_revision(store, run):
    """The fixture's real save_artifact already publishes the report revision."""
    payload = next(item["content"] for item in store.get_artifacts(run.run_id)
                   if item["artifact_type"] == "research-report")
    revision = store.latest_report_revision(run.run_id)
    assert revision is not None and revision["payload"] == payload
    assert revision["generation"] == 1
    return revision


@pytest.mark.parametrize("cancelled", [False, True])
def test_peer_diagnostic_and_stage_are_atomic_and_do_not_mutate_on_failure(tmp_path, cancelled):
    store, continuity, run = fixtures._fixture(tmp_path)
    run.status = RunStatus.COMPLETED
    store.save_run(run)
    existing_revision(store, run)
    persisted, memory = fixtures._snapshot(store, run.run_id), run.to_dict()
    fixtures._abort_on_stage_insert(store)
    with pytest.raises(Exception, match="INJECTED_CONTINUITY_FAILURE"):
        continuity.record_comparison_result(run, comparison_index=1,
            error_code="RESEARCH_CANCELLED" if cancelled else "MODEL_RATE_LIMITED",
            model_id="configured-peer", retryable=True, cancelled=cancelled)
    assert fixtures._snapshot(store, run.run_id) == persisted
    assert run.to_dict() == memory


@pytest.mark.parametrize("cancelled", [False, True])
def test_stale_peer_diagnostic_preserves_completed_primary_metadata_and_revision(tmp_path, cancelled):
    store, continuity, run = fixtures._fixture(tmp_path)
    run.status = RunStatus.COMPLETED
    run.completed_at = "2026-10-06T14:00:00+00:00"
    store.save_run(run)
    seeded = existing_revision(store, run)
    current = run.to_dict()
    current.update(research_configuration={"model": "newer-config"},
        market_snapshot={"price": 200}, data_snapshot={"financial_generations": {"f1": "g2"}},
        future_extension={"kept": True})
    with store.connect() as db:
        db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                   (json.dumps(current), run.run_id))
    before = fixtures._snapshot(store, run.run_id)
    revision = before["revisions"]
    assert revision and before["latest"] == (run.run_id, seeded["revision_id"], 1)
    continuity.record_comparison_result(run, comparison_index=1,
        error_code="RESEARCH_CANCELLED" if cancelled else "MODEL_RATE_LIMITED",
        model_id="configured-peer", retryable=True, cancelled=cancelled)
    stored = json.loads(store.get_run(run.run_id)["payload_json"])
    assert run.status is RunStatus.COMPLETED
    assert stored["status"] == "completed" and stored["completed_at"] == run.completed_at
    for key in ("research_configuration", "market_snapshot", "future_extension", "data_snapshot"):
        assert stored[key] == current[key]
    assert fixtures._snapshot(store, run.run_id)["revisions"] == revision
    assert fixtures._snapshot(store, run.run_id)["latest"] == before["latest"]
    stages = store.continuity_snapshot(run.run_id)["attempts"]
    assert stages[-1]["stage"] == "comparison:1"
    assert stages[-1]["outcome"] == ("cancelled" if cancelled else "waiting_retryable")


@pytest.mark.parametrize("cancelled", [False, True])
def test_orchestrator_peer_failure_uses_atomic_continuity_not_full_run_replacement(tmp_path, cancelled):
    store, _, primary = fixtures._fixture(tmp_path)
    primary.status = RunStatus.COMPLETED
    primary.completed_at = "2026-10-06T14:00:00+00:00"
    store.save_run(primary)
    seeded = existing_revision(store, primary)
    refreshed = primary.to_dict()
    refreshed["future_extension"] = {"must_survive": True}
    refreshed["data_snapshot"] = {"financial_generations": {"f1": "g2"}}
    request = ResearchStartRequest(primary.company, [], "pack",
        (ModelConfig("primary"), ModelConfig("peer")), existing_run=primary)
    callbacks = []

    def factory(config):
        if config.configured_model_id == "peer":
            with store.connect() as db:
                db.execute("UPDATE research_runs SET payload_json=? WHERE run_id=?",
                           (json.dumps(refreshed), primary.run_id))
            if cancelled:
                from openthesis.research import ResearchCancelled
                raise ResearchCancelled()
            raise RuntimeError("not persisted private-token")
        return object()

    class Workflow:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **_kwargs):
            return primary

    with patch("openthesis.application_services.ResearchWorkflow", Workflow):
        if cancelled:
            with pytest.raises(ResearchComparisonCancelled):
                ResearchOrchestrator(store, factory).start(request)
        else:
            assert ResearchOrchestrator(store, factory).start(
                request, comparison_failure=callbacks.append) is primary
    current = json.loads(store.get_run(primary.run_id)["payload_json"])
    assert current["status"] == "completed"
    assert current["future_extension"] == refreshed["future_extension"]
    assert current["data_snapshot"] == refreshed["data_snapshot"]
    assert store.latest_report_revision(primary.run_id)["revision_id"] == seeded["revision_id"]
    assert "private-token" not in str(current) + str(store.continuity_snapshot(primary.run_id))
    assert len(callbacks) == (0 if cancelled else 1)
    assert store.continuity_snapshot(primary.run_id)["latest"]["stage"] == "comparison:1"
