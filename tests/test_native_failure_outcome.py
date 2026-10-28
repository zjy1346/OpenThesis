"""Host isolation failures must not masquerade as financial inconsistency."""
import hashlib
from types import SimpleNamespace

from pypdf import PdfWriter

from openthesis.application.financial_outcomes import preparation_outcome
from openthesis.domain import Company, FilingDocument
from openthesis.financial_ingestion import FinancialIngestionEngine
from openthesis.financial_sources.pdf import FinancialPdfSource


def test_denied_native_index_retains_the_typed_host_failure(tmp_path, monkeypatch):
    path = tmp_path / "original.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    raw = path.read_bytes()
    company = Company("CN_A:SZSE:002594", "002594.SZ", "Fixture", market="CN_A",
                      security_id="CN_A:SZSE:002594", reporting_currency="CNY")
    filing = FilingDocument("native-doc", company.cik, "native-accession", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-28", "2025年度报告",
        "https://example.test/original.pdf", local_path=str(path),
        content_hash=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr("openthesis.financial_ingestion._run_pdfium_isolated",
                        lambda *_args, **_kwargs: (None, "PDFIUM_WORKER_START_FAILED"))
    monkeypatch.setattr("openthesis.financial_sources.pdf_document_units._run_pdfium_isolated",
                        lambda *_args, **_kwargs: (None, "PDFIUM_WORKER_START_FAILED"))
    collection = FinancialPdfSource(FinancialIngestionEngine(
        checkpoint_dir=tmp_path / "checkpoints", max_workers=1)).collect_candidate_batches(company, (filing,))
    assert len(collection.source_failures) == 1
    assert collection.source_failures[0].code == "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE"
    assert collection.source_failures[0].document_id == filing.document_id
    assert path.read_bytes() == raw
    assert all(not batch.candidates for batch in collection.batches_by_document[filing.document_id])


def blocked_view(*, rejected=()):
    return SimpleNamespace(model_ready=False,
        canonical_dataset=SimpleNamespace(validations=()),
        readiness=SimpleNamespace(level=SimpleNamespace(value="blocked"), actions=()),
        quarantined_facts=tuple(rejected))


def test_host_failure_has_an_action_without_fabricating_a_financial_violation():
    outcome = preparation_outcome(blocked_view(), None, ["native-doc:PDF_WORKER_START_FAILED"])
    assert outcome.state.value == "needs_action"
    assert outcome.diagnostic_code == "PDF_WORKER_START_FAILED"
    assert outcome.next_actions == ("repair_local_pdf_runtime",)


def test_actual_unit_conflict_keeps_integrity_priority_over_host_failure():
    outcome = preparation_outcome(blocked_view(rejected=(object(),)), None,
                                  ["native-doc:PDF_WORKER_START_FAILED"])
    assert outcome.state.value == "blocked_integrity"
    assert outcome.diagnostic_code == "FILING_DATA_QUALITY_FAILED"
