"""A public rebuild retains native-host diagnostics and the original report."""
import hashlib
import time
import pytest

from pypdf import PdfWriter

from openthesis.domain import Company, FilingDocument, ResearchArtifact, ResearchRun, RunStatus, utc_now_iso
from openthesis.service import AppService
from test_official_financial_preparation import OfflineOfficialSource


@pytest.mark.parametrize("code", ["PDF_WORKER_START_FAILED", "DOCUMENT_CONTEXT_TIMEOUT", "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE"])
def test_public_rebuild_retains_host_action_without_rewriting_trusted_history(tmp_path, monkeypatch, code):
    original = tmp_path / "original.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with original.open("wb") as stream:
        writer.write(stream)
    raw = original.read_bytes()
    company = Company("CN_A:SZSE:002594", "002594.SZ", "Fixture", market="CN_A",
        security_id="CN_A:SZSE:002594", reporting_currency="CNY")
    filing = FilingDocument("native-doc", company.cik, "native-accession", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-28", "2025年度报告", "https://example.test/original.pdf",
        local_path=str(original), content_hash=hashlib.sha256(raw).hexdigest())
    adapter = OfflineOfficialSource(company, filing)
    monkeypatch.setattr("openthesis.financial_ingestion._run_pdfium_isolated",
                        lambda *_args, **_kwargs: (None, "PDFIUM_WORKER_START_FAILED"))
    if code == "PDF_WORKER_START_FAILED":
        # The context prepass runs before financial-ingestion's bounded parser,
        # so inject the typed host failure at that module boundary as well.
        monkeypatch.setattr("openthesis.financial_sources.pdf_document_units._run_pdfium_isolated",
                            lambda *_args, **_kwargs: (None, "PDFIUM_WORKER_START_FAILED"))
    if code != "PDF_WORKER_START_FAILED":
        from openthesis.financial_sources.pdf_document_units import PdfDocumentContext
        monkeypatch.setattr("openthesis.financial_sources.pdf_document_units.read_document_context",
                            lambda *_args, **_kwargs: PdfDocumentContext(diagnostic_code=code))
        monkeypatch.setattr("openthesis.financial_ingestion.read_document_context",
                            lambda *_args, **_kwargs: PdfDocumentContext(diagnostic_code=code))
        # Inject at the completed native-worker boundary. A subprocess does
        # not inherit Python monkeypatches from its launching interpreter.
        monkeypatch.setattr("openthesis.financial_ingestion._parse_local_pdfs_bounded",
                            lambda *_args, **_kwargs: {filing.document_id: ([], [], code)})
    providers = []

    def no_model(config):
        providers.append(config)
        raise AssertionError("financial-only repair cannot create a model provider")

    service = AppService(tmp_path / "data", provider_factory=no_model)
    monkeypatch.setattr(service._disclosure_service, "adapter_for", lambda _company: adapter)
    run = ResearchRun("native-rebuild", company, "test", "test", "1", "none", "",
                      utc_now_iso(), status=RunStatus.PARTIAL)
    service.storage.save_company(company)
    service.storage.save_run(run)
    service.storage.save_artifact(ResearchArtifact("original-body", run.run_id, "research-report",
        "Original report", {"report": {"thesis": "Preserve this original analysis verbatim."}}))
    prior = service.storage.get_artifacts(run.run_id)

    def original_analysis_is_unchanged(storage):
        current = storage.get_artifacts(run.run_id)
        by_id = {item["artifact_id"]: item for item in current}
        assert all(by_id[item["artifact_id"]] == item for item in prior)
        # Recovery receipts/candidates may be appended for audit. They must
        # not replace or create a trusted research report after this failure.
        assert [item for item in current if item["artifact_type"] == "research-report"] == prior
    started = service.start_financial_retry(run.run_id, force=True, confirmed=True)
    deadline = time.monotonic() + 8
    status = started
    while not status["attempt_complete"]:
        assert time.monotonic() < deadline, "isolated-host failure did not reach an actionable state"
        time.sleep(.01)
        status = service.get_research_status(started["job_id"])

    assert providers == []
    # The document-context prepass normalizes the raw worker-start failure to
    # its safe, user-facing local-unavailable diagnostic.
    expected_error_code = ("DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE"
                           if code == "PDF_WORKER_START_FAILED" else code)
    assert status["error_code"] == expected_error_code
    assert status["recovery_outcome"]["state"] == "needs_action"
    expected_action = "repair_local_pdf_runtime" if code in {
        "PDF_WORKER_START_FAILED", "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE",
    } else "restore_local_worker_or_enable_authorized_cloud"
    assert status["recovery_outcome"]["next_actions"] == [expected_action]
    assert status["operation_result"]["recovery_outcome"] == status["recovery_outcome"]
    assert not status["research_complete"] and status["action_required"]
    assert "字段未通过一致性" not in status["message"]
    assert original.read_bytes() == raw
    original_analysis_is_unchanged(service.storage)
    assert service.storage.get_facts(company.security_id) == []

    restarted = AppService(service.storage.data_dir, provider_factory=no_model)
    saved = restarted.get_research_status(started["job_id"])
    assert saved["error_code"] == expected_error_code
    assert saved["recovery_outcome"] == status["recovery_outcome"]
    original_analysis_is_unchanged(restarted.storage)
    assert providers == []
