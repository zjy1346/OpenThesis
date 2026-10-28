from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import threading
import time
from types import SimpleNamespace

import pytest

import openthesis.financial_ingestion as financial_ingestion
from openthesis.domain import Company, FilingDocument
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    FilingManifest,
    _parse_local_pdfs_bounded,
)


_PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _source(tmp_path, *, cik: str = "0000000001", document_id: str = "filing-1"):
    path = tmp_path / "fixture.pdf"
    path.write_bytes(_PDF_BYTES)
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    company = Company(
        cik,
        f"FIX{cik[-1]}",
        f"Fixture {cik[-1]}",
        "NYSE",
        f"issuer-{cik}",
        "US",
        f"US:NYSE:FIX{cik[-1]}",
        "USD",
        "USD",
        "US_GAAP",
    )
    filing = FilingDocument(
        document_id=document_id,
        company_cik=cik,
        accession_number=f"{cik}-25-000001",
        form_type="10-K",
        fiscal_period="FY",
        period_end="2025-12-31",
        filed_at="2026-02-01T00:00:00+00:00",
        primary_document=f"{document_id}.pdf",
        source_url=f"https://example.test/{document_id}.pdf",
        local_path=str(path),
        content_hash=content_hash,
    )
    manifest = FilingManifest(
        filing.document_id,
        filing.accession_number,
        filing.source_url,
        filing.primary_document,
        filing.form_type,
        filing.fiscal_period,
        filing.period_end,
        filing.revision,
        filing.supersedes_document_id,
        filing.content_hash,
    )
    return company, filing, manifest


class _ControlledProducer:
    """A bounded fake for the existing producer seam, not the PDF parser."""

    def __init__(self, expected_calls: int = 1, *, inspect_orphan: bool = False):
        self.expected_calls = expected_calls
        self.inspect_orphan = inspect_orphan
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []
        self.errors: list[BaseException] = []
        self.entered = threading.Event()
        self.expected_entered = threading.Event()
        self.probe = threading.Event()
        self.probed = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.orphaned: bool | None = None
        self._finished_calls = 0

    def __call__(self, engine, company, filings, manifests, *, cancel_check=None, **_kwargs):
        filing = filings[0]
        manifest = manifests[filing.document_id]
        with self.lock:
            self.calls.append((company.cik, manifest.original_revision))
            if len(self.calls) >= self.expected_calls:
                self.expected_entered.set()
        self.entered.set()
        try:
            if self.inspect_orphan:
                if not self.probe.wait(3.0):
                    raise TimeoutError("test did not signal producer orphan probe")
                self.orphaned = bool(cancel_check and cancel_check())
                self.probed.set()
                if self.orphaned:
                    return {filing.document_id: ([], [], "pdf_parse_cancelled")}
            if not self.release.wait(3.0):
                raise TimeoutError("test did not release the controlled producer")
            return {filing.document_id: ([], [], None)}
        except BaseException as exc:
            with self.lock:
                self.errors.append(exc)
            raise
        finally:
            with self.lock:
                self._finished_calls += 1
                if self._finished_calls >= self.expected_calls:
                    self.finished.set()


class _Call:
    def __init__(self, function):
        self.result: list[dict[str, tuple[list, list, str | None]]] = []
        self.errors: list[BaseException] = []
        self.done = threading.Event()

        def run():
            try:
                self.result.append(function())
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


@pytest.fixture
def singleflight_runtime(monkeypatch):
    monkeypatch.setattr(financial_ingestion, "_PDF_FLIGHTS", {})
    producer_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="singleflight-test")
    monkeypatch.setattr(financial_ingestion, "_PDF_PRODUCERS", producer_pool)
    try:
        yield
    finally:
        producer_pool.shutdown(wait=True, cancel_futures=True)


def _observe_two_participants(monkeypatch):
    joined = threading.Event()
    created = []
    actual_flight = financial_ingestion._PdfParseFlight

    class _Participants(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if len(self) >= 2:
                joined.set()

    class _ObservedFlight(actual_flight):
        def __init__(self, event):
            super().__init__(event)
            self.participants = _Participants()
            created.append(self)

    monkeypatch.setattr(financial_ingestion, "_PdfParseFlight", _ObservedFlight)
    return joined, created


def _call(engine, company, filing, manifest, *, cancel_check=None, deadline=None):
    return _parse_local_pdfs_bounded(
        engine,
        company,
        [filing],
        {filing.document_id: manifest},
        cancel_check=cancel_check,
        max_workers=1,
        parse_timeout_seconds=4.0,
        batch_timeout_seconds=5.0,
        deadline=deadline,
    )


def _finish(calls, producer):
    producer.probe.set()
    producer.release.set()
    for call in calls:
        call.thread.join(6.0)


def _assert_call_finished(call):
    assert call.done.wait(2.0), "bounded report caller did not finish"
    call.thread.join(2.0)
    assert not call.errors
    assert len(call.result) == 1
    return call.result[0]


def test_identical_pdf_callers_share_one_producer(tmp_path, monkeypatch, singleflight_runtime):
    company, filing, manifest = _source(tmp_path)
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer()
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    two_joined, _flights = _observe_two_participants(monkeypatch)
    first = _Call(lambda: _call(engine, company, filing, manifest))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company, filing, manifest))
        assert two_joined.wait(2.0), "second identical caller did not join the active flight"
        producer.release.set()
        first_result = _assert_call_finished(first)
        second_result = _assert_call_finished(second)
        assert len(producer.calls) == 1
        assert first_result == second_result == {filing.document_id: ([], [], None)}
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)


def test_cancelling_one_waiter_keeps_needed_producer_for_second(tmp_path, monkeypatch, singleflight_runtime):
    company, filing, manifest = _source(tmp_path)
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer(inspect_orphan=True)
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    two_joined, _flights = _observe_two_participants(monkeypatch)
    cancel_first = threading.Event()
    first = _Call(lambda: _call(engine, company, filing, manifest, cancel_check=cancel_first.is_set))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company, filing, manifest))
        assert two_joined.wait(2.0), "second identical caller did not join the active flight"

        cancel_first.set()
        started = time.perf_counter()
        first_result = _assert_call_finished(first)
        assert time.perf_counter() - started < 1.0
        assert first_result[filing.document_id][2] == "pdf_parse_cancelled"
        assert not second.done.is_set()

        producer.probe.set()
        assert producer.probed.wait(2.0)
        assert producer.orphaned is False
        producer.release.set()
        second_result = _assert_call_finished(second)
        assert second_result == {filing.document_id: ([], [], None)}
        assert len(producer.calls) == 1
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)


def test_all_cancelled_participants_make_producer_exit_as_orphan(tmp_path, monkeypatch, singleflight_runtime):
    company, filing, manifest = _source(tmp_path)
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer(inspect_orphan=True)
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    two_joined, flights = _observe_two_participants(monkeypatch)
    cancel_first = threading.Event()
    cancel_second = threading.Event()
    first = _Call(lambda: _call(engine, company, filing, manifest, cancel_check=cancel_first.is_set))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company, filing, manifest, cancel_check=cancel_second.is_set))
        assert two_joined.wait(2.0), "second identical caller did not join the active flight"

        cancel_first.set()
        cancel_second.set()
        assert _assert_call_finished(first)[filing.document_id][2] == "pdf_parse_cancelled"
        assert _assert_call_finished(second)[filing.document_id][2] == "pdf_parse_cancelled"
        producer.probe.set()
        assert producer.probed.wait(2.0)
        assert producer.orphaned is True
        assert producer.finished.wait(2.0)
        assert len(flights) == 1
        assert flights[0].event.wait(2.0), "orphaned producer did not complete its flight"
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)


def test_different_issuers_do_not_share_pdf_producer(tmp_path, monkeypatch, singleflight_runtime):
    company_a, filing_a, manifest_a = _source(tmp_path, cik="0000000001", document_id="filing-a")
    company_b, filing_b, manifest_b = _source(tmp_path, cik="0000000002", document_id="filing-b")
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer(expected_calls=2)
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    first = _Call(lambda: _call(engine, company_a, filing_a, manifest_a))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company_b, filing_b, manifest_b))
        assert producer.expected_entered.wait(2.0), "different issuer joined a foreign producer"
        producer.release.set()
        assert _assert_call_finished(first)[filing_a.document_id][2] is None
        assert _assert_call_finished(second)[filing_b.document_id][2] is None
        assert len(producer.calls) == 2
        assert {cik for cik, _revision in producer.calls} == {company_a.cik, company_b.cik}
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)


def test_different_manifest_provenance_does_not_share_pdf_producer(
    tmp_path, monkeypatch, singleflight_runtime
):
    company, filing, manifest = _source(tmp_path)
    alternate_manifest = replace(manifest, original_revision=filing.revision)
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer(expected_calls=2)
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    first = _Call(lambda: _call(engine, company, filing, manifest))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company, filing, alternate_manifest))
        assert producer.expected_entered.wait(2.0), "different manifest joined a foreign producer"
        producer.release.set()
        assert _assert_call_finished(first)[filing.document_id][2] is None
        assert _assert_call_finished(second)[filing.document_id][2] is None
        assert len(producer.calls) == 2
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)


class _ManualClock:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = 0.0

    def monotonic(self):
        with self.lock:
            return self.value

    def set(self, value: float):
        with self.lock:
            self.value = value


def test_one_waiter_deadline_does_not_cancel_other_participant(
    tmp_path, monkeypatch, singleflight_runtime
):
    company, filing, manifest = _source(tmp_path)
    engine = FinancialIngestionEngine()
    producer = _ControlledProducer(inspect_orphan=True)
    monkeypatch.setattr(financial_ingestion, "_parse_local_pdfs_bounded_impl", producer)
    clock = _ManualClock()
    monkeypatch.setattr(financial_ingestion, "time", SimpleNamespace(monotonic=clock.monotonic))
    two_joined, _flights = _observe_two_participants(monkeypatch)
    first = _Call(lambda: _call(engine, company, filing, manifest, deadline=1.0))
    second = None
    try:
        assert producer.entered.wait(2.0)
        second = _Call(lambda: _call(engine, company, filing, manifest))
        assert two_joined.wait(2.0), "second identical caller did not join the active flight"

        clock.set(2.0)
        first_result = _assert_call_finished(first)
        assert first_result[filing.document_id][2] == "pdf_batch_budget_exhausted_resumable"
        assert not second.done.is_set()

        producer.probe.set()
        assert producer.probed.wait(2.0)
        assert producer.orphaned is False
        producer.release.set()
        second_result = _assert_call_finished(second)
        assert second_result == {filing.document_id: ([], [], None)}
        assert len(producer.calls) == 1
        assert not producer.errors
    finally:
        _finish([item for item in (first, second) if item is not None], producer)
