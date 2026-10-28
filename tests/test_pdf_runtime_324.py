"""Frozen-runtime boundaries: real spawn, structured IPC and page-count fallback."""
import os
import time
from unittest.mock import patch

from openthesis.pdf_worker import run_pdf_worker, pdf_phase
from openthesis.financial_sources.pdf_document_units import read_document_context


def _success():
    pdf_phase('extract', backend='test', version='1')
    return 'retained'


def _bad_import():
    pdf_phase('import')
    raise ModuleNotFoundError('sensitive C:/private/key.pdf', name='pypdfium2_raw')


def _exit():
    os._exit(17)


def _slow():
    time.sleep(10)


def _slow_cleanup():
    from multiprocessing.util import Finalize
    Finalize(None, time.sleep, args=(.8,), exitpriority=0)
    return 'already-delivered'


def test_delivered_result_is_not_rejected_by_half_second_cleanup_race():
    diagnostics = []
    assert run_pdf_worker(_slow_cleanup, diagnostic_callback=diagnostics.append) == ('already-delivered', None)
    assert diagnostics[0]['artifact_received'] and diagnostics[0]['exit_code'] == 0


def test_real_spawn_records_ready_result_and_clean_exit():
    diagnostics = []
    assert run_pdf_worker(_success, diagnostic_callback=diagnostics.append) == ('retained', None)
    item = diagnostics[0]
    assert item['ready_received'] and item['artifact_received'] and item['exit_code'] == 0
    assert item['backend'] == 'test' and item['phase'] == 'serialize'


def test_import_failure_is_safe_and_not_financial_quality():
    diagnostics = []
    assert run_pdf_worker(_bad_import, diagnostic_callback=diagnostics.append)[1] == 'PDF_BACKEND_IMPORT_FAILED'
    assert diagnostics[0]['phase'] == 'import'
    assert diagnostics[0]['module'] == 'pypdfium2_raw'
    assert 'private' not in str(diagnostics)


def test_abnormal_exit_preserves_exit_code():
    diagnostics = []
    assert run_pdf_worker(_exit, diagnostic_callback=diagnostics.append)[1] == 'PDFIUM_WORKER_EXIT'
    assert diagnostics[0]['exit_code'] == 17
    assert not diagnostics[0]['artifact_received']


def test_deadline_terminates_worker_without_native_coordinator_fallback():
    assert run_pdf_worker(_slow, timeout_seconds=.2)[1] == 'PDFIUM_WORKER_TIMEOUT'


def test_page_count_fallback_is_reachable_from_entry(tmp_path):
    with patch('openthesis.financial_sources.pdf_document_units._run_pdfium_isolated', side_effect=[
        (None, 'PDF_BACKEND_IMPORT_FAILED'), (2, None), (('人民币元 2025年', '营业收入'), None),
    ]) as isolated:
        context = read_document_context('not-opened.pdf', checkpoint_dir=tmp_path, source_hash='hash')
    assert not context.diagnostic_code and context.completed_pages == 2
    assert isolated.call_args_list[1].args[2] is True
    assert isolated.call_args_list[2].args[-1] is True


def test_spawn_failure_does_not_try_other_parser_in_same_broken_runtime(tmp_path):
    with patch('openthesis.financial_sources.pdf_document_units._run_pdfium_isolated', return_value=(None, 'PDFIUM_WORKER_START_FAILED')) as isolated:
        context = read_document_context('not-opened.pdf', checkpoint_dir=tmp_path, source_hash='hash')
    assert context.diagnostic_code == 'DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE'
    assert isolated.call_count == 1


def test_table_timeout_preserves_runtime_receipt_and_timeout_contract():
    import pytest
    from openthesis.financial_checkpoint import _invoke_worker, PdfWindow
    receipt = {'raw_error_code': 'PDFIUM_WORKER_TIMEOUT', 'phase': 'extract',
        'backend': 'pdfplumber', 'ready_received': True, 'artifact_received': False}
    def timed_out(*_args, **kwargs):
        kwargs['diagnostic_callback'](receipt)
        return None, 'PDFIUM_WORKER_TIMEOUT'
    with patch('openthesis.pdf_worker.run_pdf_worker', side_effect=timed_out):
        with pytest.raises(TimeoutError) as failure:
            _invoke_worker(_success, PdfWindow(0, (1,)), 1.)
    assert failure.value.code == 'PDFIUM_WORKER_TIMEOUT'
    assert failure.value.diagnostic == receipt
