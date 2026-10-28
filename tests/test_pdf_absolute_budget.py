from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from openthesis.domain import Company, FilingDocument
from openthesis.financial_checkpoint import (
    CheckpointKey,
    PdfWindow,
    WindowCheckpoint,
    WindowCheckpointStore,
    run_checkpointed_windows,
)
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    FilingManifest,
    _PdfParseFlight,
    _parse_local_pdfs_bounded,
    _resolve_pdf_parse_flights,
)


def _company() -> Company:
    return Company(
        "fixture-cik", "FIX", "Fixture", "NYSE", "fixture", "US",
        "US:NYSE:FIX", "USD", "USD", "US_GAAP",
    )


def _filing(path: Path, document_id: str = "fixture-1") -> FilingDocument:
    return FilingDocument(
        document_id=document_id,
        company_cik="fixture-cik",
        accession_number=document_id,
        form_type="10-K",
        fiscal_period="FY",
        period_end="2025-12-31",
        filed_at="2026-02-01T00:00:00+00:00",
        primary_document=f"{document_id}.pdf",
        source_url="https://example.test/fixture.pdf",
        local_path=str(path),
        content_hash="fixture-content",
    )


def _manifest(filing: FilingDocument) -> FilingManifest:
    return FilingManifest(
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


class PdfAbsoluteBudgetTests(unittest.TestCase):
    def test_batch_budget_returns_resumable_and_releases_running_parser(self):
        entered = threading.Event()
        release = threading.Event()
        parsed = threading.Event()
        progress: list[tuple] = []
        result_holder: list[dict] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.pdf"
            path.write_bytes(b"budget-fixture")
            filing = _filing(path)

            def parse(_path, _company, _filing, _manifest):
                entered.set()
                release.wait(2.0)
                parsed.set()
                return [], []

            collector = threading.Thread(
                target=lambda: result_holder.append(
                    _parse_local_pdfs_bounded(
                        FinancialIngestionEngine(),
                        _company(),
                        [filing],
                        {filing.document_id: _manifest(filing)},
                        parse=parse,
                        progress=lambda *event: progress.append(event),
                        batch_timeout_seconds=0.12,
                        parse_timeout_seconds=1.0,
                    )
                )
            )
            collector.start()
            self.assertTrue(entered.wait(1.0))
            collector.join(1.0)
            self.assertFalse(collector.is_alive())
            self.assertEqual(
                result_holder[0][filing.document_id][2],
                "pdf_batch_budget_exhausted_resumable",
            )
            before_release = list(progress)
            release.set()
            self.assertTrue(parsed.wait(1.0))
            collector.join(1.0)
            self.assertEqual(progress, before_release)

    def test_cancel_running_parser_returns_cancelled_without_late_collector_work(self):
        entered = threading.Event()
        release = threading.Event()
        parsed = threading.Event()
        cancel = threading.Event()
        progress: list[tuple] = []
        result_holder: list[dict] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.pdf"
            path.write_bytes(b"cancel-fixture")
            filing = _filing(path, "cancel-1")

            def parse(_path, _company, _filing, _manifest):
                entered.set()
                release.wait(2.0)
                parsed.set()
                return [], []

            collector = threading.Thread(
                target=lambda: result_holder.append(
                    _parse_local_pdfs_bounded(
                        FinancialIngestionEngine(),
                        _company(),
                        [filing],
                        {filing.document_id: _manifest(filing)},
                        parse=parse,
                        cancel_check=cancel.is_set,
                        progress=lambda *event: progress.append(event),
                        batch_timeout_seconds=5.0,
                        parse_timeout_seconds=1.0,
                    )
                )
            )
            collector.start()
            self.assertTrue(entered.wait(1.0))
            cancel.set()
            collector.join(1.0)
            self.assertFalse(collector.is_alive())
            self.assertEqual(result_holder[0][filing.document_id][2], "pdf_parse_cancelled")
            before_release = list(progress)
            release.set()
            self.assertTrue(parsed.wait(1.0))
            collector.join(1.0)
            self.assertEqual(progress, before_release)

    def test_singleflight_waiters_share_one_absolute_deadline(self):
        first = _PdfParseFlight(threading.Event())
        second = _PdfParseFlight(threading.Event())
        first_key, second_key = "first", "second"

        def resolve_first():
            time.sleep(0.04)
            first.result = ([], [], None)
            first.event.set()

        setter = threading.Thread(target=resolve_first)
        setter.start()
        started = time.monotonic()
        results: dict[str, tuple] = {}
        _resolve_pdf_parse_flights(
            {},
            {first_key: first, second_key: second},
            {},
            results,
            timeout_seconds=1.0,
            deadline=time.monotonic() + 0.14,
        )
        elapsed = time.monotonic() - started
        setter.join(1.0)
        self.assertEqual(results[first_key], ([], [], None))
        self.assertEqual(results[second_key][2], "pdf_singleflight_pending_resumable")
        self.assertLess(elapsed, 0.35)

    def test_checkpoint_deadline_keeps_completed_window_and_starts_no_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            key = CheckpointKey("budget", "parser", "rules")
            calls: list[int] = []

            def worker(window: PdfWindow):
                calls.append(window.index)
                if window.index == 0:
                    # The bounded fixture seam is synchronous when the
                    # callable is a closure.  It lets the first checkpoint
                    # commit after the absolute deadline, so the next window
                    # must be left resumable rather than started.
                    time.sleep(0.03)
                return (), (), {"last_page": window.pages[-1]}

            completed = run_checkpointed_windows(
                store,
                key,
                (PdfWindow(0, (1,)), PdfWindow(1, (2,))),
                worker,
                deadline=time.monotonic() + 0.01,
                progress=lambda *_event: None,
            )
            self.assertEqual(tuple(item.window.index for item in completed), (0,))
            self.assertEqual(calls, [0])
            self.assertIsNotNone(store.load(key, 0, expected_pages=(1,)))

    def test_checkpoint_normal_budget_completes_all_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            key = CheckpointKey("normal", "parser", "rules")
            windows = (PdfWindow(0, (1,)), PdfWindow(1, (2,)))
            statuses: list[str] = []

            def worker(window: PdfWindow):
                return (), (), {"last_page": window.pages[-1]}

            completed = run_checkpointed_windows(
                store,
                key,
                windows,
                worker,
                deadline=time.monotonic() + 1.0,
                progress=lambda _current, _total, status: statuses.append(status),
            )
            self.assertEqual(tuple(item.window.index for item in completed), (0, 1))
            self.assertEqual(statuses, ["checkpointed", "checkpointed"])


if __name__ == "__main__":
    unittest.main()
