"""RPC/job completion cannot overwrite the application's recovery outcome."""
import pytest

from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchRun, RunStatus
from openthesis.research_continuity import StageAttempt, StageOutcome
from openthesis.service import AppService, _ResearchJob


@pytest.mark.parametrize("outcome", [StageOutcome.NEEDS_ACTION, StageOutcome.WAITING_EXTERNAL,
                                     StageOutcome.EXHAUSTED])
def test_completed_job_preserves_last_application_recovery_diagnostic(tmp_path, outcome):
    service = AppService(tmp_path / "store")
    service.storage.save_company(DEMO_COMPANY)
    run = ResearchRun("primary", DEMO_COMPANY, "wf", "pack", "1", "fake", "primary",
                      "2026-10-06", RunStatus.COMPLETED)
    service.storage.save_run(run)
    job = _ResearchJob("test-job", run=run, run_id=run.run_id)

    def execute(_request, **_notifications):
        service.storage.save_run_stage_result(run.run_id,
            StageAttempt(run.run_id, "comparison:1", outcome,
                         error_code="COMPARISON_INPUT_CONFLICT"))
        return run

    service._research_orchestrator.start = execute
    service._run_research(job, {"mode": "demo"})
    assert job.state == "completed", "completion of job execution is distinct from report quality"
    stored = service.storage.continuity_snapshot(run.run_id)
    assert len(stored["attempts"]) == 1
    assert stored["latest"]["stage"] == "comparison:1"
    assert stored["latest"]["outcome"] == outcome.value
    assert stored["latest"]["error_code"] == "COMPARISON_INPUT_CONFLICT"
    assert service.storage.get_run(run.run_id)["status"] == "completed"
