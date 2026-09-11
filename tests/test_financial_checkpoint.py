from __future__ import annotations

import tempfile
import time
import unittest
from threading import Event, Thread
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_ingestion import (
    FilingManifest,
    FinancialIngestionEngine,
    PdfTableContext,
    _PeriodColumn,
    _checkpoint_context_from_dict,
    _checkpoint_context_to_dict,
)
from openthesis.financial_checkpoint import (
    CheckpointKey,
    PdfWindow,
    WindowCheckpoint,
    WindowCheckpointStore,
    plan_page_windows,
    run_checkpointed_windows,
)


def _hanging_window_worker(window: PdfWindow, context: dict[str, object]):
    del window, context
    time.sleep(5)
    return (), (), {}


def _context_window_worker(window: PdfWindow, context: dict[str, object]):
    return (), (), {"last_page": window.pages[-1], "prior": context.get("last_page", 0)}


class FinancialCheckpointTests(unittest.TestCase):
    def test_incomplete_candidate_index_fails_open_to_all_document_windows(self) -> None:
        """An incomplete fast index must not turn a real PDF into zero work."""
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "unindexed.pdf"
            pdf_path.write_bytes(b"fixture-pdf")
            company = Company("fixture", "FIX", "Fixture", market="US")
            filing = FilingDocument(
                "fixture-unindexed", company.cik, "fixture-1", "10-K", "FY",
                "2025-12-31", "2026-01-01", "fixture.pdf",
                "https://example.test/f.pdf", local_path=str(pdf_path),
            )
            manifest = FilingManifest(
                filing.document_id, filing.accession_number, filing.source_url,
                filing.primary_document, filing.form_type, filing.fiscal_period,
                filing.period_end, filing.revision, filing.supersedes_document_id,
                "",
            )
            seen_windows = []

            def run_stub(_store, _key, windows, _worker, **_kwargs):
                seen_windows.extend(windows)
                return ()

            engine = FinancialIngestionEngine(checkpoint_dir=Path(directory) / "cp")
            with patch("openthesis.financial_ingestion._candidate_financial_pages", return_value=None), \
                    patch("openthesis.financial_ingestion._pdf_page_count", return_value=10), \
                    patch("openthesis.financial_checkpoint.run_checkpointed_windows", side_effect=run_stub):
                _facts, _refs, diagnostics = engine.parse_local_pdf_resumable(
                    company, filing, manifest, window_size=4,
                )

            self.assertEqual([page for window in seen_windows for page in window.pages], list(range(1, 11)))
            self.assertNotIn("pdf_candidate_pages_unavailable", diagnostics)

    def test_identity_digest_separates_checkpoint_namespace(self) -> None:
        first = CheckpointKey("same", "parser", "rules", identity_digest="issuer-a")
        second = CheckpointKey("same", "parser", "rules", identity_digest="issuer-b")
        self.assertNotEqual(first.digest, second.digest)
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            store.save(WindowCheckpoint(first, PdfWindow(0, (1,)), (), (), {}))
            self.assertIsNone(store.load(second, 0, expected_pages=(1,)))

    def test_picklable_worker_timeout_is_hard_terminated(self) -> None:
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            completed = run_checkpointed_windows(
                WindowCheckpointStore(directory),
                CheckpointKey("timeout", "parser", "rules"),
                (PdfWindow(0, (1,)),),
                _hanging_window_worker,
                max_retries=0,
                timeout_seconds=0.1,
            )
        self.assertEqual(completed, ())
        self.assertLess(time.monotonic() - started, 3.0)

    def test_context_is_carried_only_from_successful_prior_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = run_checkpointed_windows(
                WindowCheckpointStore(directory),
                CheckpointKey("context", "parser", "rules"),
                (PdfWindow(0, (1,)), PdfWindow(1, (2,))),
                _context_window_worker,
            )
        self.assertEqual(completed[1].context["prior"], 1)

    def test_non_picklable_timeout_seam_still_receives_context(self) -> None:
        seen: list[dict[str, object]] = []

        def worker(window: PdfWindow, context: dict[str, object]):
            seen.append(dict(context))
            return (), (), {"last_page": window.pages[-1]}

        with tempfile.TemporaryDirectory() as directory:
            completed = run_checkpointed_windows(
                WindowCheckpointStore(directory),
                CheckpointKey("closure", "parser", "rules"),
                (PdfWindow(0, (1,)), PdfWindow(1, (2,))),
                worker,
                timeout_seconds=0.1,
            )
        self.assertEqual(len(completed), 2)
        self.assertEqual(seen[1]["last_page"], 1)

    def test_engine_resumable_route_uses_disk_windows_and_second_engine_hits_them(self) -> None:
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "fixture.pdf"
            writer = PdfWriter()
            for _ in range(220):
                writer.add_blank_page(width=200, height=200)
            # Keep the page graph tiny while storing padding in an unreferenced
            # stream object; readers can resolve pages without scanning it.
            padding = DecodedStreamObject()
            padding.set_data(b"x" * (30 * 1024 * 1024))
            writer._add_object(padding)
            with pdf_path.open("wb") as handle:
                writer.write(handle)
            self.assertGreater(pdf_path.stat().st_size, 30 * 1024 * 1024)
            company = Company("fixture", "FIX", "Fixture", market="US")
            filing = FilingDocument(
                "fixture-doc", company.cik, "fixture-1", "10-K", "FY",
                "2025-12-31", "2026-01-01", "fixture.pdf", "https://example.test/f.pdf",
                local_path=str(pdf_path),
            )
            manifest = FilingManifest(
                filing.document_id, filing.accession_number, filing.source_url,
                filing.primary_document, filing.form_type, filing.fiscal_period,
                filing.period_end, filing.revision, filing.supersedes_document_id,
                "", 
            )
            checkpoint_dir = Path(directory) / "checkpoints"
            engine = FinancialIngestionEngine(
                checkpoint_dir=checkpoint_dir, parse_timeout_seconds=10
            )
            first = engine.parse_local_pdf_resumable(
                company, filing, manifest,
                candidate_pages=frozenset((*range(1, 25), 220)), window_size=8,
            )
            self.assertEqual(first[2], ())
            self.assertEqual(len(list(checkpoint_dir.glob("*.json"))), 4)
            second_progress: list[str] = []
            second = FinancialIngestionEngine(
                checkpoint_dir=checkpoint_dir, parse_timeout_seconds=10
            ).parse_local_pdf_resumable(
                company, filing, manifest,
                candidate_pages=frozenset((*range(1, 25), 220)), window_size=8,
                progress=lambda current, total, status: second_progress.append(status),
            )
            self.assertEqual(second[2], ())
            self.assertEqual(second_progress, ["cache-hit"] * 4)

    def test_cancel_terminates_a_running_picklable_window(self) -> None:
        cancelled = Event()
        result: list[object] = []

        def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                result.extend(run_checkpointed_windows(
                    WindowCheckpointStore(directory),
                    CheckpointKey("cancel", "parser", "rules"),
                    (PdfWindow(0, (1,)),),
                    _hanging_window_worker,
                    max_retries=0,
                    timeout_seconds=10,
                    cancel_check=cancelled.is_set,
                ))

        thread = Thread(target=run)
        thread.start()
        time.sleep(0.2)
        cancelled.set()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [])

    def test_window_plan_covers_all_candidate_pages_deterministically(self) -> None:
        windows = plan_page_windows(220, range(1, 221), window_size=8)
        self.assertEqual(windows[0], PdfWindow(0, tuple(range(1, 9))))
        self.assertEqual(windows[-1].pages, (217, 218, 219, 220))
        self.assertEqual(
            [page for window in windows for page in window.pages], list(range(1, 221))
        )

    def test_large_document_window_plan_has_no_size_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.pdf"
            with path.open("wb") as handle:
                handle.seek(30 * 1024 * 1024)
                handle.write(b"x")
            self.assertGreater(path.stat().st_size, 30 * 1024 * 1024)
            windows = plan_page_windows(220, range(1, 221), window_size=8)
            self.assertEqual(sum(len(window.pages) for window in windows), 220)

    def test_checkpoint_roundtrip_preserves_fact_evidence_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            key = CheckpointKey("document-hash", "financial-ingestion-ast-v6", "rules-v1", "window-v1")
            fact = FinancialFact(
                "fact-1", "fixture", "revenue", "Revenue", 10.0, "CNY", 2025,
                "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31", "2026-01-01",
                "acc-1", "https://example.test/filing.pdf", source_page=4,
                raw_text="Revenue 10", unit_provenance="explicit",
            )
            evidence = EvidenceRef(
                "fact:fact-1", "doc-1", fact.source_url, "Revenue", "page:4",
                fact.raw_text, fact.filed_at, bbox=(1.0, 2.0, 3.0, 4.0),
            )
            checkpoint = WindowCheckpoint(
                key, PdfWindow(0, (4, 5)), (fact,), (evidence,),
                {"statement": "income_statement", "scope": "consolidated"},
            )
            store.save(checkpoint)
            restored = store.load(key, 0)
            self.assertIsNotNone(restored)
            self.assertIsInstance(restored.facts[0], FinancialFact)
            self.assertIsInstance(restored.evidence[0], EvidenceRef)
            self.assertEqual(restored.facts[0].unit_provenance, "explicit")
            self.assertEqual(restored.context["statement"], "income_statement")

    def test_corrupt_or_wrong_version_checkpoint_is_a_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            key = CheckpointKey("hash", "parser-v1", "rules-v1", "window-v1")
            checkpoint = WindowCheckpoint(key, PdfWindow(0, (1,)), (), (), {})
            store.save(checkpoint)
            path = store.path_for(key, 0)
            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(store.load(key, 0))

    def test_worker_restart_resumes_after_last_successful_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            windows = plan_page_windows(24, range(1, 25), window_size=8)
            calls: list[int] = []
            failed_once = {1}

            def worker(window: PdfWindow):
                calls.append(window.index)
                if window.index in failed_once:
                    failed_once.remove(window.index)
                    raise RuntimeError("worker crash")
                return (), (), {"last_page": window.pages[-1]}

            completed = run_checkpointed_windows(
                store, CheckpointKey("hash", "parser-v1", "rules-v1", "window-v1"),
                windows, worker, max_retries=1,
            )
            self.assertEqual([item.window.index for item in completed], [0, 1, 2])
            self.assertEqual(calls, [0, 1, 1, 2])

    def test_failed_window_stops_context_dependent_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            windows = plan_page_windows(24, range(1, 25), window_size=8)
            calls: list[int] = []

            def worker(window: PdfWindow):
                calls.append(window.index)
                if window.index == 1:
                    raise RuntimeError("unrecoverable")
                return (), (), {}

            completed = run_checkpointed_windows(
                store, CheckpointKey("hash-stop", "parser-v1", "rules-v1", "window-v1"),
                windows, worker, max_retries=1,
            )
            self.assertEqual([item.window.index for item in completed], [0])
            self.assertEqual(calls, [0, 1, 1])

    def test_second_run_resumes_failed_window_and_suffix_without_repeating_cache_hit(self) -> None:
        """A failed boundary is retried, while prior windows stay cached."""
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(Path(directory))
            windows = plan_page_windows(32, range(1, 33), window_size=8)
            calls: list[int] = []
            fail_once = True

            def worker(window: PdfWindow):
                nonlocal fail_once
                calls.append(window.index)
                if window.index == 1 and fail_once:
                    fail_once = False
                    raise RuntimeError("unrecoverable on first run")
                return (), (), {"last_page": window.pages[-1]}

            first = run_checkpointed_windows(
                store, CheckpointKey("resume", "parser", "rules"), windows,
                worker, max_retries=0,
            )
            self.assertEqual([item.window.index for item in first], [0])
            second_status: list[str] = []
            second = run_checkpointed_windows(
                store, CheckpointKey("resume", "parser", "rules"), windows,
                worker, max_retries=0,
                progress=lambda _current, _total, status: second_status.append(status),
            )
            self.assertEqual([item.window.index for item in second], [0, 1, 2, 3])
            self.assertEqual(calls, [0, 1, 1, 2, 3])
            self.assertEqual(second_status, ["cache-hit", "checkpointed", "checkpointed", "checkpointed"])

    def test_pdf_table_context_roundtrip_preserves_all_fields_for_next_window(self) -> None:
        context = PdfTableContext(
            "income_statement", "consolidated", 1000.0, "CNY", True,
            (_PeriodColumn(2025, 120.0, 100.0, 140.0, "CNY", 1000.0),
             _PeriodColumn(2024, 240.0, 220.0, 260.0, "CNY", 1000.0)),
            17, 1, "explicit",
        )
        restored = _checkpoint_context_from_dict(_checkpoint_context_to_dict(context))
        self.assertEqual(restored, context)

        seen: list[dict[str, object]] = []

        def worker(window: PdfWindow, prior: dict[str, object]):
            seen.append(dict(prior))
            return (), (), _checkpoint_context_to_dict(context)

        with tempfile.TemporaryDirectory() as directory:
            run_checkpointed_windows(
                WindowCheckpointStore(Path(directory)),
                CheckpointKey("context-full", "parser", "rules"),
                (PdfWindow(0, (17,)), PdfWindow(1, (18,))), worker,
            )
        self.assertEqual(seen[0], {})
        self.assertEqual(seen[1]["statement"], "income_statement")
        self.assertEqual(seen[1]["scope"], "consolidated")
        self.assertEqual(seen[1]["multiplier"], 1000.0)
        self.assertEqual(seen[1]["currency"], "CNY")
        self.assertTrue(seen[1]["unit_explicit"])
        self.assertEqual(seen[1]["unit_provenance"], "explicit")
        self.assertEqual(seen[1]["last_page"], 17)


if __name__ == "__main__":
    unittest.main()
