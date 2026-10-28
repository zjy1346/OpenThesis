"""Scheduling exhaustion continues only after durable progress, not heartbeat."""
import threading
from types import SimpleNamespace

import openthesis.financial_ingestion as ingestion
from test_pdf_singleflight_310 import _source


def test_producer_requeues_changed_checkpoint_until_complete(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path / "checkpoints")
    flight = ingestion._PdfParseFlight(threading.Event())
    flight.participants[object()] = (None, None)
    calls = []
    def parse(*args, **kwargs):
        calls.append(1)
        engine._document_execution[filing.document_id] = {"completed_window_count": len(calls)}
        return {filing.document_id: ([], [], "pdf_document_budget_exhausted_resumable" if len(calls) < 3 else None)}
    monkeypatch.setattr(ingestion, "_parse_local_pdfs_bounded_impl", parse)
    ingestion._shared_pdf_producer(engine, company, filing, manifest, flight, "key", .1, .1)
    assert len(calls) == 3
    assert flight.result[2] is None


def test_no_progress_slice_stops_locally_without_resetting_other_documents(tmp_path, monkeypatch):
    company, filing, manifest = _source(tmp_path)
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path / "checkpoints")
    flight = ingestion._PdfParseFlight(threading.Event())
    flight.participants[object()] = (None, None)
    calls = []
    def parse(*args, **kwargs):
        calls.append(1)
        engine._document_execution[filing.document_id] = {"completed_window_count": 0}
        return {filing.document_id: ([], [], "pdf_document_budget_exhausted_resumable")}
    monkeypatch.setattr(ingestion, "_parse_local_pdfs_bounded_impl", parse)
    ingestion._shared_pdf_producer(engine, company, filing, manifest, flight, "key", .1, .1)
    assert len(calls) == 1
    assert flight.result[2] == "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION"


def test_slice_expiry_does_not_kill_an_active_bounded_window(tmp_path, monkeypatch):
    import openthesis.financial_checkpoint as checkpoint
    key = checkpoint.CheckpointKey("original-hash", "parser-v1", "rules-v1")
    windows = (checkpoint.PdfWindow(0, (1,)), checkpoint.PdfWindow(1, (2,)))
    observed = []
    clock = [0.0]
    monkeypatch.setattr(checkpoint, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def invoke(worker, window, timeout, context, cancelled):
        observed.append((window.index, timeout))
        clock[0] += .03
        return (), (), {"last_page": window.pages[-1]}
    monkeypatch.setattr(checkpoint, "_invoke_worker", invoke)
    store = checkpoint.WindowCheckpointStore(tmp_path)
    first = checkpoint.run_checkpointed_windows(store, key, windows, lambda window: (),
        deadline=.015, timeout_seconds=1., scheduling_slice=True)
    assert [item.window.index for item in first] == [0]
    assert observed == [(0, 1.)]
    second = checkpoint.run_checkpointed_windows(store, key, windows, lambda window: (),
        deadline=.1, timeout_seconds=1., scheduling_slice=True)
    assert [item.window.index for item in second] == [0, 1]
    assert observed == [(0, 1.), (1, 1.)]


def test_pipeline_does_not_turn_scheduler_slice_into_whole_task_deadline():
    from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
    from openthesis.financial_contracts import CompilationPolicy, FactCandidate
    from test_financial_pipeline import RecordingCollector, _subject, _filing, _fact
    filing = _filing()
    collector = RecordingCollector(FactCandidate(_fact(filing), extractor="structured"))
    collector._batch_timeout_seconds = .001
    FinancialPipeline(collector).prepare(FinancialResearchSnapshot(_subject(), (filing,)),
        CompilationPolicy(reporting_currency="CNY"))
    assert collector.calls[0][2]["deadline"] is None


def test_pipeline_preserves_explicit_task_deadline_without_slice_clamping():
    from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
    from openthesis.financial_contracts import CompilationPolicy, FactCandidate
    from test_financial_pipeline import RecordingCollector, _subject, _filing, _fact
    filing = _filing()
    collector = RecordingCollector(FactCandidate(_fact(filing), extractor="structured"))
    collector._batch_timeout_seconds = .001
    explicit = ingestion.time.monotonic() + 3600
    FinancialPipeline(collector).prepare(
        FinancialResearchSnapshot(_subject(), (filing,), task_deadline=explicit),
        CompilationPolicy(reporting_currency="CNY"))
    assert collector.calls[0][2]["deadline"] == explicit
