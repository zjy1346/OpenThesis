import json
import threading
from unittest.mock import patch

from test_assertion_retention_312 import setup
from openthesis.service import AppService, _ResearchJob


def test_warm_status_does_not_rebuild_or_decode_history(tmp_path):
    journal = setup(tmp_path)
    storage = journal.store
    storage.model_call_status(journal.run_id)
    with patch.object(storage, "_build_model_call_status", side_effect=AssertionError("full rebuild")):
        assert storage.model_call_status(journal.run_id) == []


def test_new_receipt_only_reads_changed_request_and_keeps_previous(tmp_path):
    journal = setup(tmp_path)
    storage = journal.store
    def insert(request_id):
        with storage.connect() as db:
            db.execute("""INSERT INTO model_request_journal(request_id,run_id,stage_id,generation,input_digest,
                configuration_fingerprint,state,owner_id,lease_until,created_at,updated_at,purpose)
                VALUES(?,?,'business',1,'input','config','running',?,10,1,2,'generation')""",
                (request_id, journal.run_id, request_id + ':owner'))
    insert("first")
    assert len(storage.model_call_status(journal.run_id)) == 1
    insert("second")
    with patch.object(storage, "_build_model_call_status", wraps=storage._build_model_call_status) as rebuild:
        calls = storage.model_call_status(journal.run_id)
    assert {call["request_id"] for call in calls} == {"first", "second"}
    assert rebuild.call_args.kwargs["request_ids"] == {"second"}
    assert all("candidate" not in json.dumps(call) for call in calls)


def test_status_storage_wait_cannot_block_cancel_job_lock(tmp_path):
    service = AppService(tmp_path)
    job = _ResearchJob("job", state="running")
    service._jobs[job.job_id] = job
    entered, release = threading.Event(), threading.Event()
    def slow(_run):
        entered.set()
        assert release.wait(2)
        return {"revision": "1", "calls": []}
    results = []
    with patch.object(service.storage, "model_call_progress", side_effect=slow):
        worker = threading.Thread(target=lambda: results.append(service.get_research_status("job")))
        worker.start()
        assert entered.wait(1)
        try:
            assert service.cancel_research("job")["state"] == "cancelling"
        finally:
            release.set()
            worker.join(2)
    assert results[0]["state"] == "cancelling", "late status must not overwrite a new cancellation"


def test_terminal_job_is_visible_only_after_its_durable_write_finishes(tmp_path):
    journal = setup(tmp_path)
    service = AppService(tmp_path)
    job = _ResearchJob("terminal-write", run_id=journal.run_id, state="running", stage="final-synthesis", percent=90)
    service._jobs[job.job_id] = job
    entered, release = threading.Event(), threading.Event()
    original = service.storage.save_research_job
    def delayed(snapshot):
        entered.set()
        assert release.wait(3)
        original(snapshot)
    with patch.object(service.storage, "save_research_job", side_effect=delayed):
        worker = threading.Thread(target=lambda: service._update_job(job, state="completed", percent=100))
        worker.start()
        assert entered.wait(1)
        try:
            observed = service.get_research_status(job.job_id)
            assert observed["state"] == "running", "completion must not race the worker's final database write"
            assert observed["attempt_complete"] is False
        finally:
            release.set()
            worker.join(3)
    assert not worker.is_alive()
    complete = service.get_research_status(job.job_id)
    assert complete["state"] == "completed"
    assert service.storage.get_research_job(job.job_id)["revision"] == complete["revision"]


def test_slow_report_projection_is_a_job_not_a_long_rpc(tmp_path):
    service = AppService(tmp_path)
    from openthesis.sidecar import JsonLineServer
    server = JsonLineServer(service)
    def dispatch(method, params):
        return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    entered, release = threading.Event(), threading.Event()
    def slow(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return {"run_id": "report-run", "markdown": "complete retained body"}
    with patch.object(service, "get_report", side_effect=slow):
        job = dispatch("rpc.start", {"method": "research.get_report", "params": {"run_id": "report-run"}})
        assert entered.wait(1)
        try:
            assert dispatch("rpc.result", {"job_id": job["job_id"]}) == {"state": "pending"}
            assert service.preferences()["ui_language"]
            assert dispatch("rpc.start", {"method": "research.get_report", "params": {"run_id": "report-run"}})["job_id"] == job["job_id"]
        finally:
            release.set()
            server._operations_executor.shutdown(wait=True)
        result = dispatch("rpc.result", {"job_id": job["job_id"]})
        assert result["response"]["result"]["markdown"] == "complete retained body"
        assert dispatch("rpc.result", {"job_id": job["job_id"]})["response"] == result["response"]
