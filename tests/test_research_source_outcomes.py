"""First-run source failures retain the same typed recovery as repair runs."""
import json
import time
from dataclasses import replace

import pytest

from openthesis.domain import ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from openthesis.financial_sources import recovery as official_source_recovery
from openthesis.market_data import MarketDataError
from openthesis.service import AppService, _financial_status
from test_official_financial_preparation import setup_preparer


@pytest.mark.parametrize(("code", "state", "action", "attempts"), [
    ("FILING_FORMAT_UNSUPPORTED", "needs_action", "provide_compatible_official_source", 1),
    ("FILING_ACCESS_DENIED", "needs_action", "configure_official_source", 1),
    ("FILING_STATUS_UNVERIFIED", "needs_action", "inspect_official_disclosure_status", 1),
    ("FILING_CONTENT_HASH_MISMATCH", "blocked_integrity", "repair_or_replace_conflicting_official_evidence", 1),
    ("FILING_FETCH_FAILED", "waiting_external", "retry_discovery", 3),
    ("RECOVERY_INCIDENT_BUDGET_EXHAUSTED", "exhausted", "extend_recovery_budget", 0),
    ("OFFICIAL_SOURCE_INTERNAL_FAILED", "failed_internal", "export_diagnostic_and_retry_with_new_plan", 1),
    ("PDF_WORKER_START_FAILED", "needs_action", "repair_local_pdf_runtime", 1),
])
def test_first_research_retains_source_outcome_action_and_history_after_restart(
    tmp_path, monkeypatch, code, state, action, attempts,
):
    company, _filing, adapter, _preparer, storage = setup_preparer(tmp_path)
    calls, providers = [], []

    def fail_discovery(*_args, **_kwargs):
        calls.append(code)
        raise MarketDataError("untrusted provider prose https://secret.invalid/private", code=code)

    adapter.list_annual_filings = fail_discovery
    if code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED":
        real_limits = official_source_recovery.RecoveryLimits

        def exhausted_initial_budget(*args, **kwargs):
            return replace(real_limits(*args, **kwargs), calls=0)

        monkeypatch.setattr(official_source_recovery, "RecoveryLimits", exhausted_initial_budget)

    def no_provider(config):
        providers.append(config)
        raise AssertionError("source failure must not enter a model")

    if code == "PDF_WORKER_START_FAILED":
        # This case simulates a runtime failure observed by the current build.
        monkeypatch.setattr("openthesis.pdf_worker.current_runtime_failure", lambda: True)

    previous = ResearchRun("prior-trusted-research", company, "test", "test", "1", "none", "",
        utc_now_iso(), status=RunStatus.PARTIAL)
    storage.save_company(company)
    storage.save_run(previous)
    artifact = ResearchArtifact("prior-trusted-body", previous.run_id, "research-report", "Original research",
        {"report": {"thesis": "Original research body remains available without a new source."}})
    storage.save_artifact(artifact)
    prior = storage.get_artifacts(previous.run_id)
    service = AppService(storage.data_dir, sec_client_factory=lambda *_args: adapter,
        provider_factory=no_provider)
    service.update_preferences({"sec_contact_profile": "personal", "sec_contact_email": "offline@example.test"})
    started = service.start_research({"mode": "company", "company": company.to_dict(),
        "annual_history_years": 2, "download_filings": True})
    deadline = time.monotonic() + 6
    status = started
    while not status["attempt_complete"]:
        assert time.monotonic() < deadline, "source failure never reached a durable terminal result"
        time.sleep(.01)
        status = service.get_research_status(started["job_id"])

    # The company-specific official directory response satisfies the required
    # identity preflight; only then does the original injected source failure
    # occur. Restart/status reads must not repeat identity discovery.
    assert adapter.identity_search_calls == 1
    assert len(calls) == attempts and providers == []
    assert adapter.facts_calls == adapter.download_calls == 0
    assert status["error_code"] == code and status["action_required"]
    assert not status["research_complete"]
    recovery = status.get("recovery_outcome")
    assert recovery and recovery["state"] == state
    assert recovery["diagnostic_code"] == code and list(recovery["next_actions"]) == [action]
    report = service.get_report(status["run_id"])
    assert report["financial_status"]["next_action"] == action
    assert report["financial_status"]["last_error"] == code
    assert report["financial_status"]["retryable"] is (state == "waiting_external")
    assert report["continuity"]["latest"]["diagnostics"]["recovery_outcome"]["state"] == state
    assert "secret.invalid" not in json.dumps((status, report), ensure_ascii=False)
    assert service.storage.get_facts(company.cik) == []
    assert service.storage.get_artifacts(previous.run_id) == prior

    # Reading the durable result cannot discover again, consume a model, or
    # reset the source incident after replacing the process-local service.
    restarted = AppService(storage.data_dir, sec_client_factory=lambda *_args: adapter,
        provider_factory=no_provider)
    saved = restarted.get_research_status(started["job_id"])
    assert saved["recovery_outcome"]["state"] == state
    assert restarted.get_report(status["run_id"])["financial_status"]["next_action"] == action
    assert adapter.identity_search_calls == 1
    assert len(calls) == attempts and providers == []
    assert restarted.storage.get_artifacts(previous.run_id) == prior

    if code == "PDF_WORKER_START_FAILED":
        # A historical runtime label is not evidence that the newly running
        # executable still needs component repair; it must allow one local retry.
        monkeypatch.setattr("openthesis.pdf_worker.current_runtime_failure", lambda: False)
        historical = restarted.get_report(status["run_id"])["financial_status"]
        assert historical["runtime_repair_required"] is False
        assert historical["next_action"] == "retry_local_parse"


@pytest.mark.parametrize("code", [
    "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION",
    "PDFIUM_WORKER_TIMEOUT",
    "FILING_LOCAL_PARSE_FAILED",
])
def test_parse_failures_are_reported_as_extraction_not_validation(tmp_path, code):
    company, filing, _adapter, _preparer, storage = setup_preparer(tmp_path)
    storage.save_filings([filing])
    storage.save_financial_recovery_case(
        run_id="parse-failure",
        company_cik=company.cik,
        accession_number=filing.accession_number,
        document_hash=filing.content_hash,
        stage="filing-parse",
        error_code=code,
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud",
    )

    status = _financial_status(
        storage,
        company.to_dict(),
        {"status": "partial", "research_configuration": {"annual_history_years": 2}},
        run_id="parse-failure",
        continuity_snapshot={},
    )

    assert status["failure_layer"] == "extraction"


def test_filing_validation_failure_remains_in_validation_layer(tmp_path):
    company, filing, _adapter, _preparer, storage = setup_preparer(tmp_path)
    storage.save_filings([filing])
    storage.save_financial_recovery_case(
        run_id="validation-failure",
        company_cik=company.cik,
        accession_number=filing.accession_number,
        document_hash=filing.content_hash,
        stage="filing-validation",
        error_code="FILING_DATA_QUALITY_FAILED",
        next_action="retry_saved_candidate_validation_or_enable_authorized_cloud",
    )

    status = _financial_status(
        storage,
        company.to_dict(),
        {"status": "partial", "research_configuration": {"annual_history_years": 2}},
        run_id="validation-failure",
        continuity_snapshot={},
    )

    assert status["failure_layer"] == "validation"
