"""Deadline/cancel regression at the real financial vision collection seam."""
from concurrent.futures import CancelledError
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openthesis.financial_contracts import CandidateBatch
from openthesis.financial_sources import VisionCandidateExtractor, _prefetch_vision_batches
from test_financial_pipeline import _filing, _subject


def test_blocked_adapter_does_not_turn_timeout_into_shutdown_wait():
    release = threading.Event()
    entered = threading.Event()
    completed = threading.Event()
    output = {}

    class Blocked:
        cancel_check = None

        def extract(self, subject, filing):
            entered.set()
            release.wait(2)
            return CandidateBatch(filing, diagnostics=("late-provider-result",))

    def collect():
        output["result"] = _prefetch_vision_batches(
            _subject(), (_filing(),), Blocked(), timeout_seconds=.03,
        )
        completed.set()

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert entered.wait(1)
        assert completed.wait(.4), "deadline must not wait for the blocked worker shutdown"
        assert output["result"][_filing().document_id].diagnostics == ("vision_timeout",)
    finally:
        release.set()
        thread.join(3)
    assert "late-provider-result" not in output["result"][_filing().document_id].diagnostics


def test_cancel_wakes_collector_before_timeout_even_if_adapter_ignores_it():
    release, entered, cancelled, completed = (threading.Event() for _ in range(4))
    errors = []

    class Blocked:
        cancel_check = cancelled.is_set

        def extract(self, subject, filing):
            entered.set()
            release.wait(2)
            return CandidateBatch(filing)

    def collect():
        try:
            _prefetch_vision_batches(_subject(), (_filing(),), Blocked(), timeout_seconds=2)
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert entered.wait(1)
        cancelled.set()
        assert completed.wait(.4), "cancellation must be polled while futures are pending"
    finally:
        release.set()
        thread.join(3)
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)


def test_vision_adapter_receives_remaining_budget_after_page_preparation():
    now = [100.]
    seen = []
    page = SimpleNamespace(original_page=1)
    config = SimpleNamespace(timeout_seconds=60)

    def prepare(*_args, **kwargs):
        assert kwargs["deadline"] == 110.
        now[0] += 4
        return (page,)

    class Adapter:
        def extract(self, *args, **kwargs):
            seen.append(args[3].timeout_seconds)
            return SimpleNamespace(facts=(), evidence=(), diagnostics=(), error_code=None)

    extractor = VisionCandidateExtractor(None, Adapter(), config, deadline=110.)
    with patch("openthesis.financial_sources.time.monotonic", side_effect=lambda: now[0]), \
         patch("openthesis.financial_ingestion._vision_failed_pages", side_effect=prepare):
        extractor.extract(_subject(), _filing())
    assert seen == [6.]
    assert config.timeout_seconds == 60


def test_expired_queued_document_never_prepares_pages_or_calls_provider():
    extractor = VisionCandidateExtractor(None, object(), SimpleNamespace(timeout_seconds=60),
                                         deadline=time.monotonic() - 1)
    with patch("openthesis.financial_ingestion._vision_failed_pages") as prepare:
        result = extractor.extract(_subject(), _filing())
    prepare.assert_not_called()
    assert result.diagnostics == ("vision_timeout",)


def test_fast_sibling_survives_blocked_first_document():
    release = threading.Event()
    first = _filing()
    second = replace(first, document_id="second", accession_number="second")

    class Adapter:
        cancel_check = None

        def extract(self, subject, filing):
            if filing.document_id == first.document_id:
                release.wait(2)
            return CandidateBatch(filing, diagnostics=("completed",))

    # Safety release is independent of the code being tested, so the red
    # revision is finite and preserves its raw failure rather than hanging QA.
    timer = threading.Timer(.6, release.set)
    timer.start()
    started = time.monotonic()
    try:
        result = _prefetch_vision_batches(_subject(), (first, second), Adapter(), timeout_seconds=.08)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        timer.cancel()
    assert elapsed < .45
    assert result[first.document_id].diagnostics == ("vision_timeout",)
    assert result[second.document_id].diagnostics == ("completed",)


def test_public_pipeline_does_not_reset_cloud_budget_after_local_collection():
    from openthesis.financial_contracts import CompilationPolicy, FinancialDataset, GapStageKind
    from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
    from test_financial_pipeline import RecordingCollector, RecordingCompiler, _fact
    from openthesis.financial_contracts import FactCandidate

    now = [100.]
    filing = _filing()
    fact = _fact(filing)
    dataset = FinancialDataset((filing,), (fact,), (), (), (), (), {}, (), False, research_facts=(fact,))
    seen = []

    class Collector(RecordingCollector):
        def collect_candidate_batches(self, subject, filings, **kwargs):
            assert kwargs["deadline"] == 110.
            now[0] += 4
            return super().collect_candidate_batches(subject, filings, **kwargs)

    config = SimpleNamespace(enabled=True, consent=True, timeout_seconds=60, validate=lambda: None)

    def collect(subject, filings, extractor, *, timeout_seconds, deadline):
        seen.append((timeout_seconds, deadline, extractor.deadline, now[0]))
        return {filing.document_id: CandidateBatch(filing, diagnostics=("vision_timeout",))}

    snapshot = FinancialResearchSnapshot(_subject(), (filing,), vision_config=config,
        vision_fallback=object(), task_deadline=110., authorized_source_stages=frozenset({GapStageKind.MINERU}))
    with patch("openthesis.financial_pipeline.time.monotonic", side_effect=lambda: now[0]), \
         patch("openthesis.financial_pipeline._prefetch_vision_batches", side_effect=collect):
        result = FinancialPipeline(Collector(FactCandidate(fact)), RecordingCompiler(dataset)).prepare(
            snapshot, CompilationPolicy(required_concepts=frozenset({"revenue"})))
    assert seen == [(60., 110., 110., 104.)]
    assert result.facts[0].value == 100
