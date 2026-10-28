"""Native availability is not evidence that a printed period is absent."""
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from openthesis.financial_ingestion import FinancialIngestionEngine, _manifest_for
from openthesis.financial_sources.pdf_document_units import PdfDocumentContext, read_document_context
from test_original_cell_mapping_303 import source
from test_native_failure_outcome import blocked_view
from openthesis.application.financial_outcomes import preparation_outcome
from openthesis.financial_sources.pdf import FinancialPdfSource
from openthesis.service import _research_data_message


@pytest.mark.parametrize("worker_code,expected", [
    ("PDFIUM_WORKER_TIMEOUT", "DOCUMENT_CONTEXT_TIMEOUT"),
    ("PDFIUM_WORKER_START_FAILED", "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE"),
])
def test_document_context_keeps_a_controlled_native_failure(worker_code, expected):
    with patch("openthesis.financial_sources.pdf_document_units.cached_page_texts", return_value=None), \
            patch("openthesis.financial_sources.pdf_document_units._run_pdfium_isolated", return_value=(None, worker_code)):
        context = read_document_context("does-not-read-fixture.pdf", timeout_seconds=1)
    assert context.diagnostic_code == expected
    assert not context.page_texts


def test_failed_prepass_is_not_reported_as_missing_printed_statement_period(tmp_path):
    company, filing, _mapping = source(tmp_path)
    @dataclass(frozen=True)
    class FailedContext(PdfDocumentContext):
        diagnostic_code: str = "DOCUMENT_CONTEXT_TIMEOUT"
    context = FailedContext()
    with patch("openthesis.financial_ingestion.read_document_context", return_value=context):
        facts, _refs, diagnostics = FinancialIngestionEngine().parse_local_pdf_resumable(
            company, filing, _manifest_for(filing), candidate_pages=frozenset({1}))
    assert not facts
    assert "DOCUMENT_CONTEXT_TIMEOUT" in diagnostics
    assert "PERIOD_DECLARATION_NOT_FOUND" not in diagnostics


@pytest.mark.parametrize("code,expected_action", [
    ("DOCUMENT_CONTEXT_TIMEOUT", "restore_local_worker_or_enable_authorized_cloud"),
    ("DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE", "repair_local_pdf_runtime"),
])
def test_context_failure_survives_collection_and_public_outcome(tmp_path, code, expected_action):
    company, filing, _mapping = source(tmp_path)
    engine = FinancialIngestionEngine()
    with patch("openthesis.financial_sources.pdf_document_units.read_document_context",
               return_value=PdfDocumentContext(diagnostic_code=code)), \
            patch("openthesis.financial_ingestion._parse_local_pdfs_bounded",
                  return_value={filing.document_id: ([], [], code)}):
        collection = FinancialPdfSource(engine).collect_candidate_batches(company, (filing,))
    assert collection.source_failures[0].code == code
    assert not any(item.endswith("PERIOD_DECLARATION_NOT_FOUND") for item in collection.diagnostics)
    outcome = preparation_outcome(blocked_view(), None, collection.diagnostics)
    assert outcome.state.value == "needs_action" and outcome.diagnostic_code == code
    assert outcome.next_actions == (expected_action,)
    # Real rejected cells still take precedence over an unavailable worker.
    conflict = preparation_outcome(blocked_view(rejected=(object(),)), None, collection.diagnostics)
    assert conflict.state.value == "blocked_integrity"
    for language in ("zh-CN", "zh-Hant", "en"):
        message = _research_data_message(code, language)
        assert message and code not in message
        assert "字段未通过一致性" not in message
