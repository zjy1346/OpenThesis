"""Source-bound faults retain progress without changing financial admission."""
from hashlib import sha256
from unittest.mock import patch

from pypdf import PdfWriter

from openthesis.financial_sources.pdf_document_units import read_document_context, runtime_receipt_path
from openthesis.research_recovery import ResearchRecoveryPlanner


def test_corrupt_original_is_not_called_a_financial_conflict(tmp_path):
    source = tmp_path / '损坏 with spaces.pdf'
    source.write_bytes(b'%PDF-1.7\nnot a valid PDF')
    context = read_document_context(str(source), checkpoint_dir=tmp_path / 'cache', source_hash=sha256(source.read_bytes()).hexdigest())
    assert context.diagnostic_code == 'PDF_SOURCE_INVALID'
    assert context.worker_diagnostics[-1]['backend'] == 'pypdf'
    assert context.worker_diagnostics[-1]['phase'] == 'open'


def test_blank_native_document_needs_ocr_without_any_upload(tmp_path):
    source = tmp_path / 'scanned.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open('wb') as handle:
        writer.write(handle)
    context = read_document_context(str(source), checkpoint_dir=tmp_path / 'cache', source_hash=sha256(source.read_bytes()).hexdigest())
    assert context.diagnostic_code == 'DOCUMENT_CONTEXT_OCR_REQUIRED'
    assert context.completed_pages == 1
    assert context.worker_diagnostics[-1]['artifact_received']


def test_window_failure_retains_previous_successful_text_without_invented_units(tmp_path):
    values = [(32, None), (tuple('first window text' for _ in range(16)), None),
              (None, 'PDF_TEXT_EXTRACTION_FAILED'), (None, 'PDF_TEXT_EXTRACTION_FAILED')]
    with patch('openthesis.financial_sources.pdf_document_units._run_pdfium_isolated', side_effect=values):
        first = read_document_context('not-opened.pdf', checkpoint_dir=tmp_path, source_hash='source')
    assert first.completed_pages == 16 and first.diagnostic_code == 'PDF_TEXT_EXTRACTION_FAILED'
    assert not first.page_texts  # No partial-document unit/period conclusions.
    progress = []
    with patch('openthesis.financial_sources.pdf_document_units._run_pdfium_isolated', side_effect=[
        (32, None), (tuple('second window text' for _ in range(16)), None),
    ]) as worker:
        second = read_document_context('not-opened.pdf', checkpoint_dir=tmp_path, source_hash='source',
                                       progress=lambda *row: progress.append(row))
    assert not second.diagnostic_code and second.completed_pages == 32
    assert worker.call_count == 2 and worker.call_args.args[2] == 16
    assert len(progress) == 2 and progress[-1][0] == 2
    assert runtime_receipt_path(tmp_path, 'source').exists()


def test_financial_repair_admits_same_run_resume_but_empty_diagnostics_do_not():
    planner = ResearchRecoveryPlanner()
    summary = {'artifact_id':'repair:summary', 'artifact_type':'deterministic-financial-summary',
        'content':{'company':{'cik':'fixture'}, 'research_ready':True, 'evidence':[{'evidence_id':'fact:verified'}]}}
    assert planner.plan([summary]).target == 'model-stages'
    assert 'financial-analyst' in planner.plan([summary]).stages
    summary['content']['research_ready'] = False
    assert not planner.plan([summary]).available


def test_verified_business_stage_is_not_repeated_after_financial_repair():
    planner = ResearchRecoveryPlanner()
    items = [{'artifact_id':'repair:summary', 'artifact_type':'deterministic-financial-summary',
        'content':{'company':{'cik':'fixture'}, 'research_ready':True, 'evidence':[{'evidence_id':'fact:verified'}]}},
        {'artifact_id':'business', 'artifact_type':'agent-analysis', 'agent_id':'business-analyst',
         'content':{'trusted_state':'completed_verified', 'analysis':'Preserve this completed role'}}]
    plan = planner.plan(items)
    assert 'business-analyst' not in plan.stages and 'financial-analyst' in plan.stages


def test_runtime_receipt_export_drops_local_paths_and_unknown_payloads():
    from openthesis.service import _safe_pdf_runtime_receipt
    receipt = _safe_pdf_runtime_receipt({'source_hash':'hash', 'private_path':'C:/private/secret.pdf',
        'workers':[{'phase':'import', 'backend':'pdfium', 'exception_type':'ModuleNotFoundError',
                    'message':'Bearer secret', 'elapsed_seconds':.1, 'raw_error_code':'PDF_BACKEND_IMPORT_FAILED'}]})
    assert receipt['workers'][0]['phase'] == 'import'
    assert 'private' not in str(receipt) and 'secret' not in str(receipt)
