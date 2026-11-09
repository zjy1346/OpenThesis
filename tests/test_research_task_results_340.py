"""Task list and detail expose only the result access backed by durable state."""
import time

from openthesis.service import AppService


def test_task_result_link_distinguishes_saved_report_from_run_identity(tmp_path):
    service = AppService(tmp_path / "store")
    try:
        service.storage.set_research_queue_paused(True)
        ready = service.start_research({"mode": "demo", "client_request_id": "saved-result-fixture"})
        abandoned = service.start_research({"mode": "demo", "client_request_id": "no-result-fixture"})
        assert service.get_research_status(ready["job_id"])["result_access"] == "none"
        assert service.cancel_research(abandoned["job_id"])["state"] == "cancelled"
        service.continue_research_queue()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if service.get_research_status(ready["job_id"])["state"] == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("the local demo did not finish")

        results = {row["job_id"]: row for row in service.list_research_tasks(states=["completed", "cancelled"])["items"]}
        for accepted, expected in [(ready, "saved"), (abandoned, "recovery")]:
            row = results[accepted["job_id"]]
            assert row["result_access"] == expected
            assert service.get_research_status(row["job_id"])["result_access"] == expected
            assert row["research_complete"] is False
        assert service.get_report(ready["run_id"])["report_read_state"] == "partial"
    finally:
        assert service.shutdown_research_tasks(timeout=5)["safe_to_exit"]
