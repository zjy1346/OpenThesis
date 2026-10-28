"""Export saved quality safely and refresh elapsed warnings without new wires."""
import json
from unittest.mock import patch

from openthesis.service import AppService, _ResearchJob
from test_assertion_retention_312 import setup


def test_saved_quality_export_does_not_include_report_bodies_or_source_quotes(tmp_path):
    journal = setup(tmp_path)
    payload = {"report": {"executive_summary": "BODY_PRIVATE_SENTINEL"},
        "readiness": {"state": "partial", "complete": False, "missing_sections": ["financial_quality", "BODY_PRIVATE_SENTINEL"]},
        "verification": {"passed": False, "verified_claim_count": 3, "unsupported_fact_count": 1,
            "claims": [{"raw_text": "SOURCE_PRIVATE_SENTINEL"}]}}
    with journal.store.connect() as db:
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)", ("final", journal.run_id, "research-report", "report",
            json.dumps(payload), "fixture", "fixture", "2026-10-24T00:00:00Z"))
    result = journal.store.research_diagnostics(journal.run_id)
    quality = result["quality"]["saved_report_result"]
    assert quality["state"] == "partial" and quality["complete"] is False
    assert quality["verification_passed"] is False
    assert quality["verified_claim_count"] == 3 and quality["unsupported_fact_count"] == 1
    assert quality["missing_sections"] == ["financial_quality"]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert result["quality"]["assessment"] == "not_reassessed_by_export"


def test_status_time_warning_refreshes_cached_card_and_revision_without_dispatch(tmp_path):
    service = AppService(tmp_path)
    job = _ResearchJob("warning", state="running", run_id="fixture")
    service._jobs[job.job_id] = job
    stale = {"revision": "same", "calls": [{"request_id": "saved", "call_plan": {"time_warning": False}}]}
    with patch.object(service.storage, "model_call_progress", return_value=stale), patch.object(
            service.storage, "model_call_plan", side_effect=[{"time_warning": False}, {"time_warning": True}]):
        first = service.get_research_status(job.job_id)
        second = service.get_research_status(job.job_id)
    assert first["status_revision"].endswith(":w0")
    assert second["status_revision"].endswith(":w1")
    assert second["model_calls"][0]["call_plan"]["time_warning"] is True
