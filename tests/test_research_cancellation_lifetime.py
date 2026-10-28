"""Public cancellation cannot advertise quiescence while a journal still writes."""
import threading
import time
from unittest.mock import patch

from openthesis.application_services import ResearchComparisonCancelled
from openthesis.domain import RunStatus
from openthesis.service import AppService


def test_cancel_is_immediate_but_terminal_status_waits_for_request_journal(tmp_path):
    entered, release, returned = (threading.Event() for _ in range(3))
    provider_threads = []
    calls = []

    class Provider:
        def generate(self, *_args, **_kwargs):
            provider_threads.append(threading.current_thread())
            calls.append("submitted")
            entered.set()
            assert release.wait(5), "test provider was not released"
            returned.set()
            return {"claims": []}

    service = AppService(tmp_path / "research", provider_factory=lambda _config: Provider())
    started = service.start_research({
        "mode": "demo",
        "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
    })
    try:
        assert entered.wait(3)
        before = time.monotonic()
        acknowledgement = service.cancel_research(started["job_id"])
        assert time.monotonic() - before < 0.5
        assert acknowledgement["state"] == "cancelling"
        assert acknowledgement["cancel_requested"]
        # Allow the coordinator's polling interval to elapse while the actual
        # submitted call remains blocked. A terminal status here is false.
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            status = service.get_research_status(started["job_id"])
            assert status["state"] == "cancelling"
            assert not status["attempt_complete"]
            time.sleep(0.01)
        assert not returned.is_set()
        assert calls == ["submitted"]
        release.set()
        deadline = time.monotonic() + 3
        while True:
            status = service.get_research_status(started["job_id"])
            if status["state"] in {"completed", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline, "request journal did not settle"
            time.sleep(0.01)
        assert status["state"] == "cancelled"
        assert status["attempt_complete"] and not status["research_complete"]
        assert returned.is_set()
        with service.storage.connect() as db:
            rows = db.execute("SELECT state FROM model_request_journal").fetchall()
        assert rows and all(row["state"] != "running" for row in rows)
        assert calls == ["submitted"], "cancellation must not start another call"
        # Windows refuses this rename while a SQLite connection owns the file.
        original = service.storage.db_path
        renamed = original.with_name("closed.db")
        original.rename(renamed)
        renamed.rename(original)
    finally:
        release.set()
        for thread in provider_threads:
            thread.join(3)


def test_public_comparison_cancel_keeps_primary_report_quality(tmp_path):
    service = AppService(tmp_path / "primary", provider_factory=lambda _config: None)

    def stop_comparison(request, **_options):
        primary = request.existing_run
        primary.status = RunStatus.COMPLETED
        service.storage.save_run(primary)
        raise ResearchComparisonCancelled(primary, 1)

    with patch.object(service._research_orchestrator, "start", side_effect=stop_comparison):
        started = service.start_research({
            "mode": "demo",
            "model": {"configured_model_id": "test.primary", "configuration_version": 1, "role": "primary"},
        })
        deadline = time.monotonic() + 3
        while True:
            status = service.get_research_status(started["job_id"])
            if status["attempt_complete"]:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert status["state"] == "cancelled"
    assert status["research_complete"], "cancelling additive work is not cancelling completed research"
    stored = service.storage.get_run(status["run_id"])
    assert stored["status"] == "completed"
    assert "主模型研究已保留" in status["message"]

