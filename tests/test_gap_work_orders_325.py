"""Durable progress and smaller native work, not repeated user clicks."""
import threading

import openthesis.financial_checkpoint as checkpoint
import openthesis.financial_ingestion as ingestion
from test_pdf_singleflight_310 import _source


def test_timed_out_window_splits_and_keeps_context_in_one_operation(tmp_path, monkeypatch):
    key = checkpoint.CheckpointKey("source", "parser", "rules")
    store = checkpoint.WindowCheckpointStore(tmp_path)
    calls = []

    def invoke(worker, window, timeout, context, cancelled):
        calls.append((window.pages, dict(context)))
        if len(window.pages) > 1:
            raise TimeoutError("native unit timed out")
        return (), (), {"last_page": window.pages[-1]}

    monkeypatch.setattr(checkpoint, "_invoke_worker", invoke)
    result = checkpoint.run_checkpointed_windows(store, key,
        (checkpoint.PdfWindow(0, (1, 2, 3, 4)),), lambda window: (),
        timeout_seconds=1., max_retries=0, adaptive=True)
    assert [item.window.pages for item in result] == [(1, 2, 3, 4)]
    assert [pages for pages, _ in calls] == [(1, 2, 3, 4), (1, 2), (1,), (2,), (3, 4), (3,), (4,)]
    assert calls[3][1]["last_page"] == 1
    assert calls[-1][1]["last_page"] == 3
    identity = store.load_work(key, result[0].window)['identity']
    assert identity['source_hash'] == 'source' and identity['backend_version'] == 'parser'
    before = len(calls)
    assert checkpoint.run_checkpointed_windows(store, key,
        (checkpoint.PdfWindow(0, (1, 2, 3, 4)),), lambda window: (),
        timeout_seconds=1., adaptive=True)
    assert len(calls) == before


def test_subwindow_artifacts_count_as_progress_and_resume_without_replay(tmp_path, monkeypatch):
    key = checkpoint.CheckpointKey("source", "parser", "rules")
    store = checkpoint.WindowCheckpointStore(tmp_path)
    calls = []
    cancelled = [False]

    def invoke(worker, window, timeout, context, cancel):
        calls.append(window.pages)
        if len(window.pages) > 1:
            raise TimeoutError()
        if window.pages == (1,):
            cancelled[0] = True
        return (), (), {"last_page": window.pages[-1]}

    monkeypatch.setattr(checkpoint, "_invoke_worker", invoke)
    windows = (checkpoint.PdfWindow(0, (1, 2)),)
    first = checkpoint.run_checkpointed_windows(store, key, windows, lambda window: (),
        cancel_check=lambda: cancelled[0], max_retries=0, adaptive=True)
    assert first == ()  # An incomplete table must not become an accepted full window.
    assert store.work_progress(key)["ready_items"] == 1
    cancelled[0] = False
    second = checkpoint.run_checkpointed_windows(store, key, windows, lambda window: (),
        max_retries=0, adaptive=True)
    assert len(second) == 1
    assert calls.count((1,)) == 1
    assert calls.count((1, 2)) == 1  # A split strategy is durable, not retried unchanged.


def test_producer_accepts_new_artifact_progress_without_page_count_change(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path / "checkpoint")
    flight = ingestion._PdfParseFlight(threading.Event())
    flight.participants[object()] = (None, None)
    calls = []

    def parse(*args, **kwargs):
        calls.append(1)
        engine._document_execution[filing.document_id] = {
            "completed_window_count": 0, "context_completed_pages": 0,
            "work_progress": {"artifacts": [str(i) for i in range(len(calls))]}}
        return {filing.document_id: ([], [], "pdf_document_budget_exhausted_resumable" if len(calls) < 3 else None)}

    monkeypatch.setattr(ingestion, "_parse_local_pdfs_bounded_impl", parse)
    ingestion._shared_pdf_producer(engine, company, filing, manifest, flight, "key", .1, .1)
    assert len(calls) == 3
    assert flight.result[2] is None


def test_default_public_engine_uses_resumable_windows_without_opt_in(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine()
    called = []

    def resume(*args, **kwargs):
        called.append(engine._checkpoint_dir)
        return [], [], ()

    def old_whole_document(*args, **kwargs):
        raise AssertionError("whole-document deadline is not an adaptive route")

    monkeypatch.setattr(engine, "parse_local_pdf_resumable", resume)
    monkeypatch.setattr(ingestion, "_parse_local_pdfs_isolated", old_whole_document)
    result = ingestion._parse_local_pdfs_bounded_impl(engine, company, [filing],
        {filing.document_id: manifest}, _shared_producer=True)
    assert result[filing.document_id][2] is None
    assert called and called[0].is_dir()


def test_new_smaller_inputs_continue_once_even_when_slice_ends_before_first_child(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path)
    flight = ingestion._PdfParseFlight(threading.Event())
    flight.participants[object()] = (None, None)
    calls = []
    def parse(*args, **kwargs):
        calls.append(1)
        engine._document_execution[filing.document_id] = {'work_progress':{
            'artifacts':[], 'pending_inputs':['smaller-pages-1-4','smaller-pages-5-8']}}
        return {filing.document_id:([], [], 'pdf_document_budget_exhausted_resumable')}
    monkeypatch.setattr(ingestion, '_parse_local_pdfs_bounded_impl', parse)
    ingestion._shared_pdf_producer(engine, company, filing, manifest, flight, 'key', .1, .1)
    assert len(calls) == 2  # A new strategy gets its next slice, unchanged input cannot loop.
    assert flight.result[2] == 'PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION'


def test_native_timeout_with_durable_smaller_input_does_not_escape_automatic_loop(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path)
    flight = ingestion._PdfParseFlight(threading.Event())
    flight.participants[object()] = (None, None)
    calls = []
    def parse(*args, **kwargs):
        calls.append(1)
        engine._document_execution[filing.document_id] = {'work_progress':{
            'artifacts':[], 'pending_inputs':['new-smaller-window']}}
        return {filing.document_id:([], [], 'PDFIUM_WORKER_TIMEOUT' if len(calls) == 1 else None)}
    monkeypatch.setattr(ingestion, '_parse_local_pdfs_bounded_impl', parse)
    ingestion._shared_pdf_producer(engine, company, filing, manifest, flight, 'key', .1, .1)
    assert len(calls) == 2 and flight.result[2] is None
