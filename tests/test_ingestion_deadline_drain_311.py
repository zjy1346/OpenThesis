from collections import deque
from hashlib import sha256
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import EvidenceRef, FilingDocument, FinancialFact
import openthesis.financial_ingestion as ingestion


class _FakeQueue:
    def __init__(self):
        self.items = deque()
        self.closed = False

    def put(self, item):
        self.items.append(item)

    def get_nowait(self):
        if not self.items:
            raise ingestion.queue.Empty
        return self.items.popleft()

    def get(self, timeout=None):
        return self.get_nowait()

    def close(self):
        self.closed = True

    def join_thread(self):
        pass


class _FakeProcess:
    def __init__(self, target, args, *, publish_messages, alive):
        self.target = target
        self.args = args
        self.publish_messages = publish_messages
        self.alive = alive
        self.started = False
        self.terminated = False
        self.killed = False

    def start(self):
        self.started = True
        if self.publish_messages:
            key, _company, _filing, _manifest, _candidate_pages, result_queue = self.args
            result_queue.put(("filing-index", key, frozenset({7}), None))
            result_queue.put(("filing-result", (key, FACTS, REFS, None)))

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


class _FakeContext:
    def __init__(self, *, publish_messages, alive):
        self.publish_messages = publish_messages
        self.alive = alive
        self.queues = []
        self.processes = []

    def Queue(self, maxsize=0):
        result_queue = _FakeQueue()
        self.queues.append(result_queue)
        return result_queue

    def Process(self, target, args, name=None):
        process = _FakeProcess(
            target,
            args,
            publish_messages=self.publish_messages,
            alive=self.alive,
        )
        process.name = name
        self.processes.append(process)
        return process


def _fixture(tmp_path, document_id):
    source = tmp_path / f"{document_id}.pdf"
    source.write_bytes(b"scheduler regression fixture")
    filing = FilingDocument(
        document_id,
        DEMO_COMPANY.cik,
        "annual-2025",
        "10-K",
        "FY",
        "2025-12-31",
        "2026-02-01",
        source.name,
        "https://example.test/annual.pdf",
        local_path=str(source),
        content_hash=sha256(source.read_bytes()).hexdigest(),
    )
    return filing, ingestion._manifest_for(filing)


FACTS = [FinancialFact(**demo_facts()[0])]
REFS = [EvidenceRef(
    "fact:cutoff",
    "doc:cutoff",
    "https://example.test/annual.pdf",
    "Official financial row",
    "page:7",
    "Original amount",
    "2026-02-01",
)]


def _cutoff_clock_values():
    # Deadline creation, admission checks, worker deadline, and actual start
    # all occur at t=0; the first collector check is exactly at t=0.1.
    return [0.0, 0.0, 0.0, 0.0, 0.0, 0.1]


def test_isolated_scheduler_drains_index_then_ready_result_at_batch_cutoff(tmp_path, monkeypatch):
    events = []

    def progress(stage, current, total, detail):
        events.append((stage, current, total, detail))

    context = _FakeContext(publish_messages=True, alive=False)
    values = iter(_cutoff_clock_values())
    last = 0.1

    def monotonic():
        nonlocal last
        last = next(values, last)
        return last

    monkeypatch.setattr(ingestion.mp, "get_context", lambda _method: context)
    monkeypatch.setattr(ingestion.time, "monotonic", monotonic)
    filing, manifest = _fixture(tmp_path, "doc:cutoff")
    queued_filing, queued_manifest = _fixture(tmp_path, "doc:queued-cutoff")
    key = f"hash:{filing.content_hash}:source:cutoff"
    # Use distinct scheduler identities because both lightweight fixtures
    # otherwise contain the same bytes and production deduplicates by hash.
    queued_key = f"queued:{queued_filing.document_id}"
    results = {}

    ingestion._parse_local_pdfs_isolated(
        ingestion.FinancialIngestionEngine(),
        DEMO_COMPANY,
        [(key, filing, manifest), (queued_key, queued_filing, queued_manifest)],
        results=results,
        cache_keys={},
        pre_errors={},
        progress=progress,
        cancel_check=None,
        max_workers=1,
        parse_timeout_seconds=2,
        batch_timeout_seconds=0.1,
        worker_entry=lambda *_args: None,
    )

    assert results[key] == (FACTS, REFS, None)
    assert results[queued_key] == ([], [], "pdf_batch_timeout")
    assert [event[0] for event in events].count("filing-index") == 1
    assert len(context.processes) == 1
    assert context.processes[0].started
    assert not context.processes[0].terminated


def test_unfinished_isolated_worker_keeps_original_batch_timeout_status_and_code(tmp_path, monkeypatch):
    events = []

    def progress(stage, current, total, detail):
        events.append((stage, current, total, detail))

    context = _FakeContext(publish_messages=False, alive=True)
    values = iter(_cutoff_clock_values())
    last = 0.1

    def monotonic():
        nonlocal last
        last = next(values, last)
        return last

    monkeypatch.setattr(ingestion.mp, "get_context", lambda _method: context)
    monkeypatch.setattr(ingestion.time, "monotonic", monotonic)
    filing, manifest = _fixture(tmp_path, "doc:unfinished")
    key = f"hash:{filing.content_hash}:source:cutoff"
    results = {}

    ingestion._parse_local_pdfs_isolated(
        ingestion.FinancialIngestionEngine(),
        DEMO_COMPANY,
        [(key, filing, manifest)],
        results=results,
        cache_keys={},
        pre_errors={},
        progress=progress,
        cancel_check=None,
        max_workers=1,
        parse_timeout_seconds=2,
        batch_timeout_seconds=0.1,
        worker_entry=lambda *_args: None,
    )

    assert results[key] == ([], [], "pdf_batch_timeout")
    timeout = [event[3] for event in events if event[0] == "filing-parse"][-1]
    assert timeout["status"] == "needs-action"
    assert timeout["error_code"] == "pdf_batch_timeout"
    assert context.processes[0].terminated


def test_thread_fallback_collects_future_that_completes_at_batch_cutoff(tmp_path, monkeypatch):
    filing, manifest = _fixture(tmp_path, "doc:thread-cutoff")
    engine = ingestion.FinancialIngestionEngine()
    engine._parse_cache_dir = None
    clock = [0.0]

    class ReadyFuture:
        def __init__(self, value):
            self.value = value

        def done(self):
            return True

        def cancelled(self):
            return False

        def cancel(self):
            return False

        def result(self):
            return self.value

    class ImmediateExecutor:
        def __init__(self, **_kwargs):
            self.shutdown_called = False

        def submit(self, function, *args):
            return ReadyFuture(function(*args))

        def shutdown(self, wait=True, cancel_futures=False):
            self.shutdown_called = True

    def wait_until_cutoff(futures, **_kwargs):
        clock[0] = 0.1
        return set(futures), set()

    monkeypatch.setattr(ingestion, "ThreadPoolExecutor", ImmediateExecutor)
    monkeypatch.setattr(ingestion, "wait", wait_until_cutoff)
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    result = ingestion._parse_local_pdfs_bounded_impl(
        engine,
        DEMO_COMPANY,
        [filing],
        {filing.document_id: manifest},
        parse=lambda *_args: (FACTS, REFS),
        max_workers=1,
        batch_timeout_seconds=0.1,
    )

    assert result[filing.document_id] == (FACTS, REFS, None)


def test_checkpoint_scheduler_harvests_future_that_finishes_just_after_wait_cutoff(tmp_path, monkeypatch):
    filing, manifest = _fixture(tmp_path, "doc:checkpoint-cutoff")
    engine = ingestion.FinancialIngestionEngine(checkpoint_dir=tmp_path / "checkpoints")
    engine._parse_cache_dir = None
    engine._file_sha256 = lambda *_args, **_kwargs: filing.content_hash
    engine.parse_local_pdf_resumable = lambda *_args, **_kwargs: (FACTS, REFS, ())
    clock = [0.0]

    class DeferredFuture:
        def __init__(self, value):
            self.value = value
            self.ready = False

        def done(self):
            return self.ready

        def cancelled(self):
            return False

        def cancel(self):
            return False

        def result(self):
            return self.value

    class DeferredExecutor:
        def __init__(self, **_kwargs):
            self.futures = []

        def submit(self, function, *args):
            future = DeferredFuture(function(*args))
            self.futures.append(future)
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            pass

    def wait_then_finish(futures, **_kwargs):
        ready = tuple(futures)
        clock[0] = 0.1
        for future in ready:
            future.ready = True
        # The completion races the wait's returned done set. The collector
        # must make one final nonblocking done() pass before declaring timeout.
        return set(), set()

    monkeypatch.setattr(ingestion, "ThreadPoolExecutor", DeferredExecutor)
    monkeypatch.setattr(ingestion, "wait", wait_then_finish)
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    result = ingestion._parse_local_pdfs_bounded_impl(
        engine,
        DEMO_COMPANY,
        [filing],
        {filing.document_id: manifest},
        max_workers=1,
        batch_timeout_seconds=0.1,
        _shared_producer=True,
    )

    assert result[filing.document_id] == (FACTS, REFS, None)

