from __future__ import annotations

import time
from types import SimpleNamespace

from openthesis.domain import Company, ResearchRun, RunStatus, utc_now_iso
from openthesis.service import AppService, FinancialRetryResult


def _capture_progress_callback(tmp_path, monkeypatch, progress):
    company = Company("0000000001", "ACME", "Fixture Corp")
    service = AppService(tmp_path / "data")
    authorized_hashes = []

    def capture_capabilities(*_args, **kwargs):
        authorized_hashes.append(kwargs["authorized_filing_hashes"])
        return (), None, None

    monkeypatch.setattr(service, "_financial_recognition_capabilities", capture_capabilities)
    _request, on_progress = service._capture_financial_repair_request(
        company, {}, progress=progress, run_id="progress-bridge-run")
    return service, on_progress, authorized_hashes


def test_public_financial_retry_keeps_page_and_validation_details(tmp_path, monkeypatch):
    company = Company("0000000001", "ACME", "Fixture Corp")
    service = AppService(tmp_path / "data")
    service.storage.save_company(company)
    service.storage.save_run(ResearchRun(
        run_id="progress-bridge-run", company=company, workflow_id="test",
        research_pack_id="test", research_pack_version="1", provider_id="unused",
        model_id="unused", data_as_of=utc_now_iso(), status=RunStatus.PARTIAL,
    ))
    monkeypatch.setattr(service, "_financial_recognition_capabilities",
        lambda *_args, **_kwargs: ((), None, None))
    monkeypatch.setattr(service, "get_report", lambda *_args, **_kwargs: {"financial_status": {}})

    page_detail = {
        "filing_id": "accession-2025",
        "label": "Fixture annual filing",
        "status": "parsing",
        "accession_number": "accession-2025",
        "source_hash": "a" * 64,
        "current_page": 7,
        "completed_page_count": 2,
        "candidate_page_count": 5,
        "window_index": 1,
        "window_total": 2,
        "last_product_at": "2026-10-27T12:34:56+08:00",
    }
    validation_detail = {
        "filing_id": "accession-2025",
        "label": "Fixture annual filing",
        "status": "completed",
        "processing_state": "completed",
        "accession_number": "accession-2025",
        "source_hash": "a" * 64,
    }

    class FakeRepairer:
        def repair(self, request, *, progress, cancel_check):
            assert request.run_id == "progress-bridge-run"
            progress("filing-page", 2, 5, page_detail)
            progress("filing-validation", 5, 5, validation_detail)
            return FinancialRetryResult(mode="retry", status="succeeded")

    service._financial_repairer = FakeRepairer()
    started = service.start_financial_retry("progress-bridge-run")
    deadline = time.monotonic() + 5
    status = started
    while not status.get("attempt_complete"):
        assert time.monotonic() < deadline, f"financial retry did not finish: {status}"
        time.sleep(0.01)
        status = service.get_research_status(started["job_id"])

    assert status["state"] == "completed"
    assert set(status["filing_states"]) == {"accession-2025"}
    filing = status["filing_states"]["accession-2025"]
    assert filing["current_page"] == 7
    assert filing["page_index"] == 2
    assert filing["page_total"] == 5
    assert filing["completed_page_count"] == 2
    assert filing["candidate_page_count"] == 5
    assert filing["window_index"] == 1
    assert filing["window_total"] == 2
    assert filing["last_product_at"] == "2026-10-27T12:34:56+08:00"
    assert filing["status"] == "completed"
    assert filing["processing_state"] == "completed"
    assert filing["waiting_type"] is None


def test_progress_bridge_keeps_legacy_three_argument_callbacks(tmp_path, monkeypatch):
    received = []

    def progress(stage, current, total):
        received.append((stage, current, total))

    _service, on_progress, _authorized_hashes = _capture_progress_callback(
        tmp_path, monkeypatch, progress)
    on_progress("filing-parse", 2, 5, {"filing_id": "accession-2025"})

    assert received == [("filing-parse", 2, 5)]


def test_progress_bridge_does_not_retry_a_four_argument_callback_type_error(tmp_path, monkeypatch):
    received = []
    detail = {"filing_id": "accession-2025"}

    def progress(stage, current, total, event_detail=None):
        received.append((stage, current, total, event_detail))
        raise TypeError("callback body failed")

    _service, on_progress, _authorized_hashes = _capture_progress_callback(
        tmp_path, monkeypatch, progress)

    try:
        on_progress("filing-parse", 2, 5, detail)
    except TypeError as error:
        assert str(error) == "callback body failed"
    else:
        raise AssertionError("callback body TypeError must propagate")

    assert received == [("filing-parse", 2, 5, detail)]


def test_progress_bridge_keeps_acquisition_hash_capture_semantics(tmp_path, monkeypatch):
    received = []
    progress = lambda *args: received.append(args)
    _service, on_progress, authorized_hashes = _capture_progress_callback(
        tmp_path, monkeypatch, progress)
    captured = SimpleNamespace(content_hash="f" * 64)

    on_progress("filing-acquisition-complete", 1, 1, [captured])

    assert authorized_hashes == [{"f" * 64}]
    assert received == []
