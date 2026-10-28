"""Regression coverage for late research progress after cancellation."""

import threading
import time
from unittest.mock import patch

from openthesis.research import ResearchCancelled
from openthesis.service import AppService


def test_late_progress_cannot_restore_cancelled_job_to_running(tmp_path):
    progress_started = threading.Event()
    release_progress = threading.Event()
    observed_states = []
    service = AppService(tmp_path / "research", provider_factory=lambda _config: None)
    job_id = ""

    def report_progress(_request, *, progress, **_options):
        event = {
            "message": "working",
            "percent": 12,
            "model_index": 0,
            "comparison_count": 0,
            "model_percent": 12,
        }
        progress(type("Progress", (), event)())
        progress_started.set()
        assert release_progress.wait(5), "test progress callback was not released"
        progress(type("Progress", (), {**event, "message": "late update"})())
        observed_states.append(service.get_research_status(job_id)["state"])
        raise ResearchCancelled()

    with patch.object(service._research_orchestrator, "start", side_effect=report_progress):
        started = service.start_research({
            "mode": "demo",
            "model": {
                "configured_model_id": "test.primary",
                "configuration_version": 1,
                "role": "primary",
            },
        })
        job_id = started["job_id"]
        try:
            assert progress_started.wait(3), "research did not report initial progress"
            acknowledgement = service.cancel_research(job_id)
            assert acknowledgement["state"] == "cancelling"
            assert acknowledgement["cancel_requested"]
            release_progress.set()
            deadline = time.monotonic() + 3
            while True:
                status = service.get_research_status(job_id)
                if status["attempt_complete"]:
                    break
                assert time.monotonic() < deadline, "cancelled research did not settle"
                time.sleep(0.01)
            assert observed_states == ["cancelling"]
            assert status["state"] == "cancelled"
        finally:
            release_progress.set()
