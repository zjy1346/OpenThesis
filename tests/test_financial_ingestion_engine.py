from __future__ import annotations

import os
import hashlib
import sys
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    FinancialGroupValidation,
    build_financial_profile,
    InMemoryFinancialSource,
    PdfCellAST,
    PdfRowAST,
    _period_columns,
    _merge_visual_rows,
    _row_label_text,
    _is_summary_page,
    _fact_rank,
    _select_period_cell,
    _unit_scale,
    _explicit_unit_info,
    _page_sections,
    _known_label,
    _revenue_group_total_rows,
    _net_income_candidate_allowed,
    _attribution_context,
    _manifest_for,
    _period_start,
    _statement_context,
    _vision_failed_pages,
      _parse_local_pdfs_bounded,
      _candidate_financial_pages,
      _safe_pdf_worker_count,
)
from openthesis.financial_compiler import _prefetch_vision_batches
from openthesis.financials import calculate_interim_metrics
from openthesis.market_financials import FinancialValidation, ValidationStatus
from openthesis.vision_financials import VisionExtractionResult, VisionFallbackConfig, VisionPageRequest


CATL_SOURCE_URL = "https://static.cninfo.com.cn/finalpage/2026-03-10/1225002214.PDF"
CATL_EXPECTED = {
    "revenue": 423_701_834_000.0,
    "net_income": 72_201_282_000.0,
    "operating_cash_flow": 133_219_982_000.0,
    "assets": 974_827_544_000.0,
    "liabilities": 603_801_220_000.0,
    "equity": 337_107_747_000.0,
    "total_equity": 371_026_324_000.0,
    "reported_roe": 0.2491,
}


def _official_pdf(env_name: str, relative_path: str) -> str:
    override = os.environ.get(env_name)
    if override:
        return override
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return ""
    return str(Path(local_app_data) / "OpenThesis" / "filings" / relative_path)


def _spawn_scheduler_test_entry(key, _company, filing, _manifest, _candidate_pages, result_queue):
    """Picklable worker seam used only to exercise scheduler lifecycle."""

    marker = Path(filing.local_path + ".started")
    active = Path(filing.local_path + ".active")
    marker.write_text("started", encoding="utf-8")
    active.write_text("active", encoding="utf-8")
    if "slow" in filing.document_id or "block" in filing.document_id:
        time.sleep(10)
    result_queue.put(("filing-index", key, frozenset({1}), None))
    error = "fixture-failure" if "fail" in filing.document_id else None
    result_queue.put(("filing-result", (key, [], [], error)))
    active.unlink(missing_ok=True)
    Path(filing.local_path + ".done").write_text("done", encoding="utf-8")


def _acceptance_pdf(env_name: str, relative_path: str, acceptance_name: str) -> str:
    override = os.environ.get(env_name)
    if override:
        return override
    acceptance = Path("build") / "acceptance" / "hk-filings" / acceptance_name
    if acceptance.is_file():
        return str(acceptance)
    return _official_pdf(env_name, relative_path)


def _cn_acceptance_pdf(env_name: str, relative_path: str, acceptance_name: str) -> str:
    """Resolve an official CN annual PDF without embedding a user path."""
    override = os.environ.get(env_name)
    if override:
        return override
    acceptance = Path("build") / "acceptance" / "cn-filings" / acceptance_name
    if acceptance.is_file():
        return str(acceptance)
    return _official_pdf(env_name, relative_path)


def _company() -> Company:
    return Company("CN_A:SZSE:300750.SZ", "300750.SZ", "宁德时代", "SZSE", "CN:CATL", "CN_A", "CN_A:SZSE:300750.SZ", "CNY", "CNY", "CAS")


def _filing(document_id: str = "cninfo:1225002214", *, period: str = "FY", end: str = "2025-12-31", path: str = "") -> FilingDocument:
    return FilingDocument(document_id, _company().security_id, document_id.split(":")[-1], "ANNUAL_REPORT" if period == "FY" else "INTERIM_REPORT", period, end, "2026-03-09T16:00:00+00:00", "2025年年度报告" if period == "FY" else "2026年半年度报告", CATL_SOURCE_URL, local_path=path, content_hash="hash")


def _hk_company(symbol: str, name: str, issuer: str, standard: str) -> Company:
    return Company(f"HK:SEHK:{symbol}", symbol, name, "SEHK", issuer, "HK", f"HK:SEHK:{symbol}", "CNY", "CNY", standard)


def _hk_filing(company: Company, accession: str, end: str, filed: str, title: str, url: str, path: str) -> FilingDocument:
    return FilingDocument(
        f"hkex:{accession}", company.security_id, accession, "ANNUAL_REPORT", "FY", end,
        filed, title, url, local_path=path, content_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    )


def _fact(filing: FilingDocument, concept: str, value: float, *, scale: float = 1.0, scope: str = "consolidated", currency: str = "CNY") -> FinancialFact:
    statement = "cash_flow" if concept == "operating_cash_flow" else "balance_sheet" if concept in {"assets", "liabilities", "equity", "total_equity"} else "income_statement"
    return FinancialFact(f"{filing.document_id}:{concept}", _company().security_id, concept, concept, value, currency, int(filing.period_end[:4]), filing.fiscal_period, filing.form_type, None if statement == "balance_sheet" else f"{filing.period_end[:4]}-01-01", filing.period_end, filing.filed_at, filing.accession_number, filing.source_url, scope=scope, entity=_company().name, market="CN_A", statement=statement, period_start=None if statement == "balance_sheet" else f"{filing.period_end[:4]}-01-01", consolidated_scope=scope, currency=currency, unit_scale=scale, source_document=filing.primary_document, source_page=1, raw_text=f"{concept} {value}")


def _core(filing: FilingDocument, scale: float = 1.0) -> tuple[FinancialFact, ...]:
    values = {"revenue": 1000, "net_income": 100, "operating_cash_flow": 150, "assets": 1000, "liabilities": 600, "equity": 400, "total_equity": 400}
    return tuple(_fact(filing, key, value * scale, scale=scale) for key, value in values.items())


def _row(*cells: tuple[str, float, float]) -> PdfRowAST:
    """Small positioned-row fixture: (text, x0, x1)."""
    parsed = tuple(PdfCellAST(text, x0, 10, x1, 20) for text, x0, x1 in cells)
    return PdfRowAST(parsed, 10, (min(c.x0 for c in parsed), 10, max(c.x1 for c in parsed), 20))


def _formal_rows(title: str = "Consolidated balance sheet") -> tuple[PdfRowAST, ...]:
    return (
        _row((title, 10, 180)),
        _row(("2025", 100, 140), ("2024", 220, 260)),
        _row(("Revenue", 10, 80), ("100", 100, 140), ("90", 220, 260)),
    )


class FinancialIngestionEngineTests(unittest.TestCase):
    def test_financial_page_prepass_selects_titles_and_bounded_continuations(self) -> None:
        from unittest.mock import patch

        class Page:
            def __init__(self, text: str) -> None:
                self.text = text
            def extract_text(self) -> str:
                return self.text

        class Reader:
            pages = [
                Page("Narrative"),
                Page("CONSOLIDATED INCOME STATEMENT"),
                Page("continuation"),
                Page("continuation"),
                Page("continuation"),
                Page("Consolidated statement of cash flows"),
                Page("continuation"),
                Page("合并资产负债表"),
                Page("continuation"),
                Page("净资产收益率及每股收益 加权平均净资产收益率（%）"),
            ]

        with patch("pypdf.PdfReader", return_value=Reader()):
            selected = _candidate_financial_pages("annual.pdf")
        self.assertEqual(selected, frozenset({2, 3, 4, 5, 6, 7, 8, 9, 10}))

    def test_financial_page_prepass_uses_pdfium_and_closes_resources(self) -> None:
        class TextPage:
            def __init__(self, text: str) -> None:
                self.text = text
                self.closed = False
            def get_text_range(self) -> str:
                return self.text
            def close(self) -> None:
                self.closed = True

        class Page:
            def __init__(self, text: str) -> None:
                self.text_page = TextPage(text)
                self.closed = False
            def get_textpage(self) -> TextPage:
                return self.text_page
            def close(self) -> None:
                self.closed = True

        class Document:
            def __init__(self) -> None:
                self.pages = [Page(text) for text in (
                    "CONSOLIDATED INCOME STATEMENT", "continuation",
                    "CONSOLIDATED BALANCE SHEET", "CONSOLIDATED STATEMENT OF CASH FLOWS",
                    "continuation", "continuation", "narrative",
                )]
                self.closed = False
            def __len__(self) -> int:
                return len(self.pages)
            def __getitem__(self, index: int) -> Page:
                return self.pages[index]
            def close(self) -> None:
                self.closed = True

        document = Document()
        fake_pdfium = type("Pdfium", (), {"PdfDocument": lambda _path: document})
        with patch.dict(sys.modules, {"pypdfium2": fake_pdfium}), \
                patch("pypdf.PdfReader") as reader:
            selected = _candidate_financial_pages("annual.pdf")
        self.assertEqual(selected, frozenset({1, 2, 3, 4, 5, 6, 7}))
        self.assertTrue(document.closed)
        self.assertTrue(all(page.closed for page in document.pages))
        self.assertTrue(all(page.text_page.closed for page in document.pages))
        reader.assert_not_called()

    def test_financial_page_prepass_falls_back_when_pdfium_index_is_incomplete(self) -> None:
        class IncompleteDocument:
            def __len__(self) -> int:
                return 2
            def __getitem__(self, index: int):
                class Page:
                    def get_textpage(self):
                        return type("Text", (), {"get_text_range": lambda self: "CONSOLIDATED INCOME STATEMENT" if index == 0 else "CONSOLIDATED BALANCE SHEET", "close": lambda self: None})()
                    def close(self):
                        pass
                return Page()
            def close(self):
                pass

        class PdfPage:
            def __init__(self, text: str) -> None:
                self.text = text
            def extract_text(self) -> str:
                return self.text

        class PdfReader:
            pages = [PdfPage("CONSOLIDATED INCOME STATEMENT"), PdfPage("CONSOLIDATED BALANCE SHEET"), PdfPage("CONSOLIDATED STATEMENT OF CASH FLOWS")]

        fake_pdfium = type("Pdfium", (), {"PdfDocument": lambda _path: IncompleteDocument()})
        with patch.dict(sys.modules, {"pypdfium2": fake_pdfium}), \
                patch("pypdf.PdfReader", return_value=PdfReader()):
            selected = _candidate_financial_pages("annual.pdf")
        self.assertEqual(selected, frozenset({1, 2, 3}))

    def test_pdf_process_parallelism_falls_back_for_large_compressed_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            small = []
            for index in range(2):
                path = Path(directory) / f"small-{index}.pdf"
                path.write_bytes(b"x" * 1024)
                small.append(_filing(f"small-{index}", path=str(path)))
            large_path = Path(directory) / "large.pdf"
            with large_path.open("wb") as handle:
                handle.truncate(9 * 1024 * 1024)
            large = _filing("large", path=str(large_path))

            self.assertEqual(_safe_pdf_worker_count(small, requested=3), 2)
            # A large batch must not force every document into one global
            # worker.  Scheduling remains bounded, but the per-file budget
            # may still reduce the requested count.
            self.assertEqual(_safe_pdf_worker_count([small[0], large], requested=3), 2)

    def test_pdf_helper_streams_fast_document_before_slow_document(self) -> None:
        progress: list[tuple[str, int, int]] = []
        started: list[str] = []
        release_slow = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            filings = []
            for name in ("fast", "slow"):
                path = Path(directory) / f"{name}.pdf"
                path.write_bytes(name.encode())
                filing = _filing(name, path=str(path))
                filing.content_hash = name
                filings.append(filing)

            def parse(_path, _company, filing, _manifest):
                started.append(filing.document_id)
                if filing.document_id == "slow":
                    release_slow.wait(timeout=1)
                return [], []

            result_holder: list[dict] = []
            worker = threading.Thread(
                target=lambda: result_holder.append(_parse_local_pdfs_bounded(
                    FinancialIngestionEngine(), _company(), filings,
                    {item.document_id: _manifest_for(item) for item in filings},
                    parse=parse, progress=lambda *event: progress.append(event),
                    max_workers=2,
                )),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and ("filing-parse", 1, 2) not in progress:
                time.sleep(0.005)
            self.assertEqual(progress[0], ("cache-check", 1, 2))
            self.assertIn(("filing-parse", 1, 2), progress)
            release_slow.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual([item.document_id for item in filings], list(result_holder[0]))

    def test_isolated_scheduler_limits_processes_and_orders_index_before_result(self) -> None:
        events: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            filings = []
            for index in range(4):
                path = Path(directory) / f"spawn-{index}.pdf"
                path.write_bytes(f"fixture-{index}".encode())
                item = _filing(f"spawn-{index}", path=str(path))
                item.content_hash = ""
                filings.append(item)
            holder: list[dict] = []
            thread = threading.Thread(target=lambda: holder.append(_parse_local_pdfs_bounded(
                FinancialIngestionEngine(), _company(), filings,
                {item.document_id: _manifest_for(item) for item in filings},
                worker_entry=_spawn_scheduler_test_entry,
                progress=lambda *event: events.append(event), max_workers=2,
                parse_timeout_seconds=2,
            )))
            thread.start()
            peak = 0
            while thread.is_alive():
                peak = max(peak, len(list(Path(directory).glob("*.active"))))
                time.sleep(0.01)
            thread.join(timeout=1)
            result = holder[0]
            self.assertEqual(len(result), 4)
            self.assertLessEqual(peak, 2)
            stages = [event[0] for event in events]
            self.assertEqual(stages.count("cache-check"), 4)
            self.assertEqual(stages.count("filing-index"), 4)
            self.assertEqual(stages.count("filing-parse"), 4)
            self.assertEqual(stages[4], "filing-index")
            for item in filings:
                self.assertTrue(Path(item.local_path + ".done").is_file())

    def test_isolated_scheduler_timeout_and_cancel_terminate_report_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            slow_path = Path(directory) / "slow.pdf"
            slow_path.write_bytes(b"fixture")
            slow = _filing("slow", path=str(slow_path))
            slow.content_hash = ""
            started = time.monotonic()
            result = _parse_local_pdfs_bounded(
                FinancialIngestionEngine(), _company(), [slow],
                {slow.document_id: _manifest_for(slow)},
                worker_entry=_spawn_scheduler_test_entry, max_workers=1,
                parse_timeout_seconds=0.2,
            )
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result[slow.document_id][2], "pdf_parse_timeout")
            self.assertFalse(Path(slow.local_path + ".done").exists())

            cancel = threading.Event()
            blocked_path = Path(directory) / "block.pdf"
            blocked_path.write_bytes(b"fixture")
            blocked = _filing("block", path=str(blocked_path))
            blocked.content_hash = ""
            holder: list[dict] = []
            thread = threading.Thread(target=lambda: holder.append(_parse_local_pdfs_bounded(
                FinancialIngestionEngine(), _company(), [blocked],
                {blocked.document_id: _manifest_for(blocked)},
                worker_entry=_spawn_scheduler_test_entry, cancel_check=cancel.is_set,
                max_workers=1, parse_timeout_seconds=10,
            )))
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not Path(blocked.local_path + ".started").exists():
                time.sleep(0.01)
            cancel.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(Path(blocked.local_path + ".done").exists())

    def test_batch_watchdog_blocks_running_and_queued_reports_without_reducing_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            filings = []
            for document_id in ("slow-batch", "queued-batch"):
                path = Path(directory) / f"{document_id}.pdf"
                path.write_bytes(b"fixture")
                filing = _filing(document_id, path=str(path))
                filing.content_hash = ""
                filings.append(filing)
            started = time.monotonic()
            result = _parse_local_pdfs_bounded(
                FinancialIngestionEngine(),
                _company(),
                filings,
                {item.document_id: _manifest_for(item) for item in filings},
                worker_entry=_spawn_scheduler_test_entry,
                max_workers=1,
                parse_timeout_seconds=10,
                batch_timeout_seconds=0.2,
            )
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(set(result), {item.document_id for item in filings})
            self.assertTrue(
                all(value[2] == "pdf_batch_timeout" for value in result.values())
            )
            self.assertFalse(any(Path(item.local_path + ".done").exists() for item in filings))

    def test_parse_single_flight_failure_wakes_concurrent_waiter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "same.pdf"
            path.write_bytes(b"same-content")
            first = _filing("fail-one", path=str(path))
            second = _filing("fail-two", path=str(path))
            first.content_hash = second.content_hash = ""
            manifests = {
                first.document_id: _manifest_for(first),
                second.document_id: _manifest_for(second),
            }
            results: list[dict] = []
            threads = [
                threading.Thread(target=lambda item=item: results.append(_parse_local_pdfs_bounded(
                    FinancialIngestionEngine(), _company(), [item],
                    {item.document_id: manifests[item.document_id]},
                    worker_entry=_spawn_scheduler_test_entry, max_workers=1,
                    parse_timeout_seconds=2,
                )))
                for item in (first, second)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 2)
            self.assertTrue(all(next(iter(item.values()))[2] == "fixture-failure" for item in results))

    def test_pdf_parse_cache_reuses_success_and_invalidates_parser_version(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cached.pdf"
            path.write_bytes(b"stable-pdf")
            filing = _filing("cached", path=str(path))
            filing.content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            cache_dir = Path(directory) / "cache"
            engine = FinancialIngestionEngine(cache_dir=cache_dir)
            second_engine = FinancialIngestionEngine(cache_dir=cache_dir)

            def parse(self, _path, _company, item, _manifest, **_kwargs):
                calls.append(item.document_id)
                fact = _fact(item, "revenue", 100)
                return [fact], [self._evidence_for_fact(fact, item)]

            manifests = {filing.document_id: _manifest_for(filing)}
            with patch.object(FinancialIngestionEngine, "_parse_pdf_ast", parse), \
                    patch("openthesis.financial_ingestion._candidate_financial_pages", return_value=frozenset({1})):
                first = _parse_local_pdfs_bounded(engine, _company(), [filing], manifests)
                second = _parse_local_pdfs_bounded(second_engine, _company(), [filing], manifests)
                self.assertEqual(len(calls), 1)
                self.assertIn(filing.document_id, second)
                cached_facts, cached_refs, cached_error = second[filing.document_id]
                self.assertIsNone(cached_error)
                self.assertIsInstance(cached_facts[0], FinancialFact)
                self.assertIsInstance(cached_refs[0], EvidenceRef)
                with patch("openthesis.financial_ingestion._PDF_PARSER_VERSION", "new-parser"):
                    third = _parse_local_pdfs_bounded(second_engine, _company(), [filing], manifests)
            self.assertIn(filing.document_id, first)
            self.assertIn(filing.document_id, third)
            self.assertEqual(len(calls), 2)

    def test_default_pdf_pipeline_reports_cache_and_index_stages_before_all_files(self) -> None:
        events: list[tuple[str, int, int]] = []
        with tempfile.TemporaryDirectory() as directory:
            filings = []
            for name in ("fast-index", "slow-index"):
                path = Path(directory) / f"{name}.pdf"
                path.write_bytes(name.encode())
                filing = _filing(name, path=str(path))
                filing.content_hash = name
                filings.append(filing)

            def parse(self, _path, _company, _filing, _manifest, **_kwargs):
                return [], []

            def index(path: str):
                if "slow-index" in path:
                    time.sleep(0.1)
                return frozenset({1})

            with patch.object(FinancialIngestionEngine, "_parse_pdf_ast", parse), \
                    patch("openthesis.financial_ingestion._candidate_financial_pages", side_effect=index):
                _parse_local_pdfs_bounded(
                    FinancialIngestionEngine(), _company(), filings,
                    {item.document_id: _manifest_for(item) for item in filings},
                    progress=lambda *event: events.append(event), max_workers=2,
                )
        self.assertEqual(events[0], ("cache-check", 1, 2))
        self.assertIn(("filing-index", 1, 2), events)

    def test_ingest_parses_local_filings_with_bounded_parallelism_and_ordered_reuse(self) -> None:
        active = 0
        maximum = 0
        calls: list[str] = []
        lock = threading.Lock()

        with tempfile.TemporaryDirectory() as directory:
            paths = []
            filings = []
            for index in range(6):
                path = Path(directory) / f"report-{index}.pdf"
                path.write_bytes(f"pdf-{index}".encode())
                filing = _filing(f"parallel-{index}", path=str(path))
                filing.content_hash = f"hash-{index}"
                paths.append(path)
                filings.append(filing)
            duplicate = _filing("parallel-duplicate", path=str(paths[0]))
            duplicate.content_hash = filings[0].content_hash
            filings.append(duplicate)

            engine = FinancialIngestionEngine()

            def parse(path, company, filing, manifest):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    calls.append(filing.document_id)
                time.sleep(0.01)
                with lock:
                    active -= 1
                return [], []

            with patch.object(engine, "_parse_pdf_ast", side_effect=parse):
                dataset = engine.ingest(_company(), filings)

            self.assertLessEqual(maximum, 3)
            self.assertEqual(calls, [f"parallel-{index}" for index in range(6)])
            self.assertEqual(len(dataset.manifest), 7)

    def test_bounded_pdf_helper_skips_cancelled_unstarted_work(self) -> None:
        cancel = threading.Event()
        cancel.set()
        calls: list[str] = []
        filings = [_filing(f"cancel-{index}", path=f"cancel-{index}.pdf") for index in range(4)]

        def parse(path, company, filing, manifest):
            calls.append(filing.document_id)
            return [], []

        result = _parse_local_pdfs_bounded(
            FinancialIngestionEngine(), _company(), filings,
            {filing.document_id: _manifest_for(filing) for filing in filings},
            parse=parse, cancel_check=cancel.is_set,
        )
        self.assertEqual(result, {})
        self.assertEqual(calls, [])

    def test_bounded_pdf_helper_preserves_completed_result_when_cancel_cancels_queue(self) -> None:
        cancel = threading.Event()
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            filings = []
            for index in range(3):
                path = Path(directory) / f"cancel-queue-{index}.pdf"
                path.write_bytes(f"pdf-{index}".encode())
                filings.append(_filing(f"cancel-queue-{index}", path=str(path)))

            def parse(path, company, filing, manifest):
                calls.append(filing.document_id)
                cancel.set()
                return [], []

            result = _parse_local_pdfs_bounded(
                FinancialIngestionEngine(), _company(), filings,
                {filing.document_id: _manifest_for(filing) for filing in filings},
                parse=parse, cancel_check=cancel.is_set, max_workers=1,
            )
        self.assertIn(filings[0].document_id, result)
        self.assertEqual(calls, [filings[0].document_id])

    def test_pdf_prescan_falls_back_to_full_parse_when_statement_index_is_incomplete(self) -> None:
        class Page:
            def __init__(self, text: str):
                self.text = text
            def extract_text(self):
                return self.text

        class Reader:
            def __init__(self, _path):
                self.pages = [
                    Page("Consolidated Income Statement"),
                    Page("Consolidated Balance Sheet"),
                ]

        with patch("pypdf.PdfReader", Reader):
            self.assertIsNone(_candidate_financial_pages("not-a-real-pdf.pdf"))
    def test_vision_page_selection_excludes_parent_and_stops_at_next_statement(self) -> None:
        import tempfile
        from unittest.mock import patch

        class Page:
            def __init__(self, text: str):
                self.text = text
            def extract_text(self):
                return self.text

        pages = [
            Page("Contents: consolidated financial statements"),
            Page("Separate financial statements of parent company\nBalance Sheet"),
            Page("Consolidated balance sheet"),
            Page("Assets and liabilities (continued)"),
            Page("Consolidated statement of cash flows"),
            Page("Operating cash flows (continued)"),
        ]

        class Reader:
            def __init__(self, _path):
                self.pages = pages

        class Writer:
            def __init__(self):
                self.page = None
            def add_page(self, page):
                self.page = page
            def write(self, buffer):
                buffer.write(b"page")

        filing = _filing("cninfo:vision-boundary", path="placeholder.pdf")
        config = VisionFallbackConfig(enabled=True, consent=True, max_pages=20)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            actual = filing.__class__(**{**filing.to_dict(), "local_path": handle.name})
            with patch("pypdf.PdfReader", Reader), patch("pypdf.PdfWriter", Writer):
                selected = _vision_failed_pages(
                    actual, config, ("balance_sheet_core_missing",), ()
                )
        self.assertEqual([page.original_page for page in selected], [3, 4])

    def test_vision_page_selection_only_uploads_missing_statement_after_local_facts(self) -> None:
        class Page:
            def __init__(self, text: str):
                self.text = text
            def extract_text(self):
                return self.text

        pages = [
            Page("Consolidated income statement"),
            Page("Revenue continued"),
            Page("Consolidated balance sheet"),
            Page("Assets continued"),
            Page("Consolidated statement of cash flows"),
            Page("Cash flows continued"),
        ]

        class Reader:
            def __init__(self, _path):
                self.pages = pages

        class Writer:
            def add_page(self, _page):
                pass
            def write(self, buffer):
                buffer.write(b"page")

        filing = _filing("cninfo:vision-missing-cash", path="placeholder.pdf")
        existing = [fact for fact in _core(filing) if fact.statement != "cash_flow"]
        config = VisionFallbackConfig(enabled=True, consent=True, max_pages=20)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            actual = filing.__class__(**{**filing.to_dict(), "local_path": handle.name})
            with patch("pypdf.PdfReader", Reader), patch("pypdf.PdfWriter", Writer):
                selected = _vision_failed_pages(
                    actual, config, ("cash_flow_core_missing",), existing
                )
        self.assertEqual([page.original_page for page in selected], [5, 6])

    def test_vision_prefetch_runs_two_filings_concurrently_and_preserves_order(self) -> None:
        filings = [_filing("vision-prefetch-1"), _filing("vision-prefetch-2")]
        active = 0
        peak = 0
        lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        class Extractor:
            def extract(self, _subject, filing):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    entered.set()
                release.wait(timeout=1)
                with lock:
                    active -= 1
                return type("Batch", (), {
                    "filing": filing, "candidates": (), "evidence": (), "diagnostics": ()
                })()

        result_holder: list[dict] = []
        worker = threading.Thread(target=lambda: result_holder.append(
            _prefetch_vision_batches(_company(), filings, Extractor(), timeout_seconds=1.0)
        ))
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        time.sleep(0.05)
        self.assertEqual(peak, 2)
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            tuple(result_holder[0]),
            tuple(item.document_id for item in filings),
        )

    def test_local_verified_group_never_calls_vision_adapter(self) -> None:
        import tempfile

        filing = _filing("cninfo:local-success")
        calls = []

        class Adapter:
            def extract(self, *args, **kwargs):
                calls.append(True)
                return VisionExtractionResult(error_code="unexpected")

        class StubEngine(FinancialIngestionEngine):
            def _parse_pdf_ast(self, path, company, filing, manifest):
                facts = list(_core(filing))
                return facts, [self._evidence_for_fact(fact, filing) for fact in facts]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            dataset = StubEngine().ingest(
                _company(), [filing.__class__(**{**filing.to_dict(), "local_path": handle.name})],
                vision_fallback=Adapter(),
                vision_config=VisionFallbackConfig(
                    enabled=True,
                    consent=True,
                    configured_model_id="fixture-model",
                    require_page_approval=True,
                    approve_upload=lambda _summary: True,
                ),
            )
        self.assertEqual(dataset.status, ValidationStatus.VERIFIED)
        self.assertEqual(calls, [])

    def test_rejected_local_group_can_supply_bounded_vision_candidates_to_same_gate(self) -> None:
        import tempfile
        from unittest.mock import patch

        filing = _filing("cninfo:vision-fallback")
        page = VisionPageRequest(1, b"opaque-page-bytes")
        calls = []

        class Adapter:
            def extract(self, *args, **kwargs):
                calls.append(args[2])
                facts = list(_core(filing))
                return VisionExtractionResult(tuple(facts), tuple(self._evidence_for_fact(fact, filing) for fact in facts), ("VISION_CANDIDATES_ONLY",))

            _evidence_for_fact = staticmethod(FinancialIngestionEngine._evidence_for_fact)

        class StubEngine(FinancialIngestionEngine):
            def _parse_pdf_ast(self, path, company, filing, manifest):
                facts = list(_core(filing)[:2])
                return facts, [self._evidence_for_fact(fact, filing) for fact in facts]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            with patch("openthesis.financial_ingestion._vision_failed_pages", return_value=(page,)):
                dataset = StubEngine().ingest(
                    _company(), [filing.__class__(**{**filing.to_dict(), "local_path": handle.name})],
                    vision_fallback=Adapter(),
                    vision_config=VisionFallbackConfig(
                        enabled=True,
                        consent=True,
                        configured_model_id="fixture-model",
                        require_page_approval=True,
                        approve_upload=lambda _summary: True,
                    ),
                )
        self.assertEqual(len(calls), 1)
        self.assertEqual(dataset.status, ValidationStatus.VERIFIED)

    def test_ambiguous_visual_duplicate_does_not_last_write_win(self) -> None:
        import tempfile
        filing = _filing("cninfo:vision-ambiguous")

        class Adapter:
            def extract(self, *args, **kwargs):
                facts = list(_core(filing))
                facts.append(_fact(filing, "revenue", 9999))
                return VisionExtractionResult(tuple(facts), tuple(), ("VISION_CANDIDATES_ONLY",))

        class StubEngine(FinancialIngestionEngine):
            def _parse_pdf_ast(self, path, company, filing, manifest):
                facts = list(_core(filing)[:2])
                return facts, [self._evidence_for_fact(fact, filing) for fact in facts]

        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            with patch("openthesis.financial_ingestion._vision_failed_pages", return_value=(VisionPageRequest(1, b"opaque"),)):
                dataset = StubEngine().ingest(
                    _company(), [filing.__class__(**{**filing.to_dict(), "local_path": handle.name})],
                    vision_fallback=Adapter(),
                    vision_config=VisionFallbackConfig(
                        enabled=True,
                        consent=True,
                        configured_model_id="fixture-model",
                        require_page_approval=True,
                        approve_upload=lambda _summary: True,
                    ),
                )
        # Vision is restricted to concepts missing from the local batch;
        # its unrelated duplicate revenue candidate cannot overwrite the
        # accepted local fact.
        self.assertEqual(dataset.status, ValidationStatus.VERIFIED)
        self.assertFalse(any(fact.value == 9999 for fact in dataset.accepted_facts))
    def test_profile_tracks_selected_manifest_without_facts(self) -> None:
        filing = _filing("cninfo:no-facts", end="2023-12-31")
        profile = build_financial_profile(
            [], (), "CNY", selected_filings=[filing]
        )
        self.assertEqual(profile.period_continuity[0]["status"], "no_facts")
        self.assertEqual(profile.period_continuity[0]["period_end"], "2023-12-31")
        self.assertFalse(profile.fact_dicts)

    def test_profile_distinguishes_rejected_and_accepted_selected_periods(self) -> None:
        rejected_filing = _filing("cninfo:rejected", end="2022-12-31")
        accepted_filing = _filing("cninfo:accepted", end="2023-12-31")
        rejected_group = FinancialGroupValidation(
            (rejected_filing.accession_number, rejected_filing.period_end, "FY", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.REJECTED, ("core_missing",), frozenset()),
        )
        accepted_facts = _core(accepted_filing)
        accepted_group = FinancialGroupValidation(
            (accepted_filing.accession_number, accepted_filing.period_end, "FY", "consolidated", "CNY"),
            FinancialValidation(ValidationStatus.VERIFIED, (), frozenset({"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity"}), accepted=accepted_facts),
        )
        profile = build_financial_profile(
            accepted_facts,
            (rejected_group, accepted_group),
            "CNY",
            selected_filings=[rejected_filing, accepted_filing],
        )
        by_end = {item["period_end"]: item for item in profile.period_continuity}
        self.assertEqual(by_end["2022-12-31"]["status"], "rejected")
        self.assertEqual(by_end["2023-12-31"]["status"], "accepted")
        self.assertEqual(profile.rejected_periods[0]["period_end"], "2022-12-31")
        self.assertTrue(all(item["end_date"] != "2022-12-31" for item in profile.fact_dicts))
    def test_page_context_summary_does_not_leak_to_next_page(self) -> None:
        summary_rows = (_row(("加权平均净资产收益率", 10, 180), ("20%", 220, 260)),)
        summary = _page_sections(None, "加权平均净资产收益率", summary_rows, 1, "CNY")
        self.assertTrue(summary and summary[0].summary)
        unrelated = _page_sections(summary[0].context, "appendix narrative", (_row(("unrelated", 10, 100),),), 2, "CNY")
        self.assertEqual(unrelated, ())

    def test_unrelated_narrative_page_resets_context(self) -> None:
        first = _page_sections(None, "Consolidated balance sheet thousand CNY", _formal_rows(), 1, "CNY")
        self.assertTrue(first)
        second = _page_sections(first[-1].context, "Narrative discussion without a table", (_row(("risk discussion", 10, 150),),), 2, "CNY")
        self.assertEqual(second, ())

    def test_formal_table_continuation_inherits_for_two_pages_then_resets(self) -> None:
        first = _page_sections(None, "Consolidated balance sheet thousand CNY", _formal_rows(), 1, "CNY")
        self.assertEqual(first[-1].context.multiplier, 1000.0)
        second = _page_sections(first[-1].context, "", (_formal_rows()[2],), 2, "CNY")
        self.assertTrue(second and second[0].inherited)
        self.assertEqual(second[0].context.inherited_pages, 1)
        third = _page_sections(second[0].context, "", (_formal_rows()[2],), 3, "CNY")
        self.assertTrue(third and third[0].inherited)
        self.assertEqual(third[0].context.inherited_pages, 2)
        fourth = _page_sections(third[0].context, "", (_formal_rows()[2],), 4, "CNY")
        self.assertEqual(fourth, ())

    def test_parent_title_splits_same_page_table_boundary(self) -> None:
        rows = _formal_rows()[:2] + (_formal_rows()[2],) + _formal_rows("资产负债表")[0:1] + (_formal_rows()[2],)
        sections = _page_sections(None, "Consolidated balance sheet 资产负债表", rows, 1, "CNY")
        self.assertEqual([(s.context.statement, s.context.scope) for s in sections], [("balance_sheet", "consolidated"), ("balance_sheet", "parent")])
        self.assertNotIn("Parent balance sheet", sections[0].rows[-1].text)

    def test_missing_unit_on_continuation_does_not_override_explicit_scale(self) -> None:
        first = _page_sections(None, "Consolidated balance sheet thousand CNY", _formal_rows(), 1, "CNY")
        second = _page_sections(first[-1].context, "", (_formal_rows()[2],), 2, "CNY")
        self.assertTrue(second)
        self.assertEqual(second[0].context.multiplier, 1000.0)
        self.assertTrue(second[0].context.unit_explicit)

    def test_eps_bare_yuan_does_not_reset_untitled_continuation_unit(self) -> None:
        bare_scale, bare_currency, bare_explicit = _explicit_unit_info(
            "基本每股收益(元/股)；本期0 元"
        )
        self.assertEqual((bare_scale, bare_currency, bare_explicit), (1.0, "CNY", False))
        header_scale, header_currency, header_explicit = _explicit_unit_info(
            "单位：元 币种：人民币"
        )
        self.assertEqual((header_scale, header_currency, header_explicit), (1.0, "CNY", True))

        first = _page_sections(
            None,
            "合并利润表 单位：人民币千元",
            _formal_rows("合并利润表"),
            1,
            "CNY",
        )
        continuation = _page_sections(
            first[-1].context,
            "基本每股收益(元/股)；本期0 元",
            (
                _row(("净利润", 10, 80), ("100", 100, 140), ("90", 220, 260)),
                _row(("基本每股收益(元/股)", 10, 150), ("0", 100, 140), ("0", 220, 260)),
            ),
            2,
            "CNY",
        )
        self.assertTrue(continuation)
        self.assertEqual(continuation[0].context.multiplier, 1000.0)
        self.assertEqual(continuation[0].context.currency, "CNY")
        self.assertTrue(continuation[0].context.unit_explicit)

    def test_titled_income_continuation_inherits_unit_and_period_context(self) -> None:
        first_rows = (
            _row(("合并利润表", 10, 180)),
            _row(("2021", 100, 140), ("2020", 220, 260)),
            _row(("净利润", 10, 80), ("100", 100, 140), ("90", 220, 260)),
        )
        first = _page_sections(None, "合并利润表 单位：人民币千元", first_rows, 1, "CNY")
        self.assertTrue(first)
        continuation = _page_sections(
            first[0].context,
            "合并利润表（续）",
            (_row(("合并利润表（续）", 10, 180)), _row(("净利润", 10, 80), ("80", 100, 140), ("70", 220, 260))),
            2,
            "CNY",
        )
        self.assertTrue(continuation)
        context = continuation[0].context
        self.assertEqual(context.statement, "income_statement")
        self.assertEqual(context.scope, "consolidated")
        self.assertEqual(context.multiplier, 1000.0)
        self.assertEqual(context.currency, "CNY")
        self.assertTrue(context.unit_explicit)
        self.assertEqual([column.year for column in context.periods], [2021, 2020])
        self.assertTrue(continuation[0].inherited)

    def test_same_statement_without_continuation_title_does_not_inherit_unit(self) -> None:
        first = _page_sections(None, "合并利润表 单位：人民币千元", _formal_rows("合并利润表"), 1, "CNY")
        second = _page_sections(
            first[0].context, "合并利润表", _formal_rows("合并利润表"), 2, "CNY"
        )
        self.assertTrue(second)
        self.assertEqual(second[0].context.multiplier, 1.0)
        self.assertFalse(second[0].context.unit_explicit)

    def test_changed_scope_does_not_inherit_continuation_unit(self) -> None:
        first = _page_sections(None, "合并利润表 单位：人民币千元", _formal_rows("合并利润表"), 1, "CNY")
        second = _page_sections(
            first[0].context, "母公司利润表（续）", _formal_rows("母公司利润表"), 2, "CNY"
        )
        self.assertTrue(second)
        self.assertEqual(second[0].context.scope, "parent")
        self.assertEqual(second[0].context.multiplier, 1.0)
        self.assertFalse(second[0].context.unit_explicit)

    def test_same_statement_unit_scale_mismatch_is_fatal(self) -> None:
        filing = _filing("scale-mismatch")
        facts = list(_core(filing))
        revenue = _fact(
            filing, "revenue", 1_000_000, scale=1000
        )
        net_income = _fact(
            filing, "net_income", 100, scale=1
        )
        revenue.parser_version = net_income.parser_version = "financial-ingestion-ast-v2"
        facts[facts.index(next(item for item in facts if item.concept == "revenue"))] = revenue
        facts[facts.index(next(item for item in facts if item.concept == "net_income"))] = net_income
        identity = (filing.accession_number, filing.period_end, "FY", "consolidated", "CNY")
        result = FinancialIngestionEngine().validate_group(facts, identity)
        self.assertEqual(result.validation.status, ValidationStatus.REJECTED)
        self.assertIn("statement_unit_scale_inconsistent", result.validation.issues)

    def test_mixed_normalized_sources_do_not_trigger_unit_scale_issue(self) -> None:
        filing = _filing("mixed-source")
        facts = list(_core(filing))
        revenue = _fact(filing, "revenue", 1_000_000, scale=1000)
        net_income = _fact(filing, "net_income", 100, scale=1)
        revenue.parser_version = "financial-ingestion-ast-v2"
        revenue.source_document = "report.pdf"
        net_income.parser_version = "sec-companyfacts-v1"
        net_income.source_document = "companyfacts.json"
        facts[facts.index(next(item for item in facts if item.concept == "revenue"))] = revenue
        facts[facts.index(next(item for item in facts if item.concept == "net_income"))] = net_income
        identity = (filing.accession_number, filing.period_end, "FY", "consolidated", "CNY")
        result = FinancialIngestionEngine().validate_group(facts, identity)
        self.assertNotIn("statement_unit_scale_inconsistent", result.validation.issues)

    def test_coordinate_ast_merges_wrapped_label_around_period_values(self) -> None:
        rows = (
            PdfRowAST((PdfCellAST("加权平均净资产收益", 62.304, 521.899, 143.304, 530.899),), 521.899, (62.304, 521.899, 143.304, 530.899)),
            PdfRowAST((PdfCellAST("24.91%", 219.89, 528.574, 247.763, 537.574), PdfCellAST("24.13%", 315.07, 528.574, 342.943, 537.574)), 528.574, (219.89, 528.574, 342.943, 537.574)),
            PdfRowAST((PdfCellAST("率", 62.304, 533.899, 71.304, 542.899),), 533.899, (62.304, 533.899, 71.304, 542.899)),
        )
        merged = _merge_visual_rows(rows, 0)
        self.assertIn("加权平均净资产收益率", _row_label_text(merged))
        self.assertEqual(len(merged.cells), 4)
        headers = PdfRowAST((PdfCellAST("2025", 219.89, 500, 247.763, 509), PdfCellAST("2024", 315.07, 500, 342.943, 509)), 500, (219.89, 500, 342.943, 509))
        selected = _select_period_cell(merged, 143.304, _period_columns((headers,)), 2025)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "24.91%")
        self.assertTrue(_is_summary_page("", rows))

    def test_formal_statement_candidate_outranks_summary_candidate(self) -> None:
        filing = _filing("rank")
        summary = _fact(filing, "revenue", 1_000)
        summary.statement = "summary"
        formal = _fact(filing, "revenue", 2_000)
        self.assertGreater(_fact_rank(formal), _fact_rank(summary))

    def test_current_period_column_is_selected_even_when_not_first_numeric(self) -> None:
        header = PdfRowAST((PdfCellAST("2024", 100, 0, 140, 10), PdfCellAST("2025", 220, 0, 260, 10)), 0, (100, 0, 260, 10))
        row = PdfRowAST((PdfCellAST("Revenue", 10, 20, 80, 30), PdfCellAST("900", 100, 20, 140, 30), PdfCellAST("1000", 220, 20, 260, 30)), 20, (10, 20, 260, 30))
        columns = _period_columns((header,))
        selected = _select_period_cell(row, 80, columns, 2025)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "1000")

    def test_balance_date_headers_preserve_fiscal_current_and_opening_years(self) -> None:
        header = PdfRowAST(
            (
                PdfCellAST("2022年12月31日", 100, 0, 180, 10),
                PdfCellAST("2022年1月1日", 220, 0, 300, 10),
            ),
            0,
            (100, 0, 300, 10),
        )
        data = PdfRowAST(
            (
                PdfCellAST("资产总计", 10, 20, 80, 30),
                PdfCellAST("600", 100, 20, 180, 30),
                PdfCellAST("300", 220, 20, 300, 30),
            ),
            20,
            (10, 20, 300, 30),
        )
        columns = _period_columns((header, data), "CNY")
        self.assertEqual([column.year for column in columns], [2022, 2021])
        selected = _select_period_cell(data, 80, columns, 2022)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "600")

    def test_balance_english_date_headers_preserve_fiscal_current_and_opening_years(self) -> None:
        header = PdfRowAST(
            (
                PdfCellAST("31 December 2022", 100, 0, 180, 10),
                PdfCellAST("1 January 2022", 220, 0, 300, 10),
            ),
            0,
            (100, 0, 300, 10),
        )
        data = PdfRowAST(
            (
                PdfCellAST("Total assets", 10, 20, 80, 30),
                PdfCellAST("600", 100, 20, 180, 30),
                PdfCellAST("300", 220, 20, 300, 30),
            ),
            20,
            (10, 20, 300, 30),
        )
        columns = _period_columns((header, data), "CNY")
        self.assertEqual([column.year for column in columns], [2022, 2021])
        selected = _select_period_cell(data, 80, columns, 2022)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "600")

    def test_balance_american_date_headers_preserve_fiscal_current_and_opening_years(self) -> None:
        header = PdfRowAST(
            (
                PdfCellAST("December 31, 2022", 100, 0, 180, 10),
                PdfCellAST("January 1, 2022", 220, 0, 300, 10),
            ),
            0,
            (100, 0, 300, 10),
        )
        data = PdfRowAST(
            (
                PdfCellAST("Total assets", 10, 20, 80, 30),
                PdfCellAST("600", 100, 20, 180, 30),
                PdfCellAST("300", 220, 20, 300, 30),
            ),
            20,
            (10, 20, 300, 30),
        )
        columns = _period_columns((header, data), "CNY")
        self.assertEqual([column.year for column in columns], [2022, 2021])
        selected = _select_period_cell(data, 80, columns, 2022)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "600")

    def test_balance_date_range_uses_period_end_year(self) -> None:
        header = PdfRowAST(
            (
                PdfCellAST("January 1, 2022 - December 31, 2022", 100, 0, 210, 10),
                PdfCellAST("January 1, 2021 - December 31, 2021", 230, 0, 340, 10),
            ),
            0,
            (100, 0, 340, 10),
        )
        columns = _period_columns((header,))
        self.assertEqual([column.year for column in columns], [2022, 2021])

    def test_current_assets_label_is_not_mistaken_for_period_header(self) -> None:
        rows = (
            _row(("Balance at the end", 330, 435), ("Balance at the beginning", 445, 550)),
            _row(("Item", 75, 105), ("of the period", 360, 435), ("of the year", 490, 550)),
            _row(("Current assets:", 75, 180)),
            _row(("Cash", 90, 150), ("100", 340, 435), ("90", 455, 550)),
            _row(("Total assets", 75, 150), ("500", 340, 435), ("450", 455, 550)),
        )
        columns = _period_columns(rows)
        self.assertEqual(len(columns), 2)
        selected = _select_period_cell(rows[-1], 150, columns, 2026)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "500")

    def test_split_balance_header_with_repeated_prefix_finds_both_value_columns(self) -> None:
        rows = (
            _row(("Balance at the", 330, 435), ("Balance at the", 445, 550)),
            _row(("Item", 75, 105), ("end of the period", 360, 435), ("beginning of the year", 455, 550)),
            _row(("Current assets:", 75, 180)),
            _row(("Cash", 90, 150), ("100", 340, 435), ("90", 455, 550)),
            _row(("Total assets", 75, 150), ("500", 340, 435), ("450", 455, 550)),
        )
        columns = _period_columns(rows)
        self.assertEqual(len(columns), 2)
        selected = _select_period_cell(rows[-1], 150, columns, 2025)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "500")

    def test_single_year_statement_title_is_not_a_period_header(self) -> None:
        title = _row(("For the year ended 31 December 2025", 10, 260))
        self.assertEqual(_period_columns((title,)), ())
        header = _row(("2025", 100, 140))
        columns = _period_columns((title, header))
        self.assertEqual([column.year for column in columns], [2025])

    def test_revenue_group_requires_detail_sum_before_unlabeled_total(self) -> None:
        header = _row(("2025", 100, 140), ("2024", 220, 260))
        rows = (
            _row(("Revenues", 10, 80)),
            _row(("Value-added Services", 10, 80), ("10", 100, 140), ("9", 220, 260)),
            _row(("Marketing Services", 10, 80), ("20", 100, 140), ("18", 220, 260)),
            _row(("6", 50, 60), ("30", 100, 140), ("27", 220, 260)),
        )
        totals = _revenue_group_total_rows(rows, _period_columns((header,)), 2025)
        self.assertEqual(len(totals), 1)
        self.assertEqual(next(iter(totals.values())).cells[0].text, "6")

    def test_english_statement_labels_are_known(self) -> None:
        self.assertTrue(_known_label("Equity holders of the Company"))
        self.assertTrue(_known_label("Net cash flows generated from operating activities"))
        self.assertTrue(_known_label("Equity attributable to equity holders of the Company"))

    def test_net_income_equity_holder_label_requires_attributable_context(self) -> None:
        self.assertTrue(_net_income_candidate_allowed("Attributable to: Equity holders of the Company 224,842"))
        self.assertFalse(_net_income_candidate_allowed("Earnings per share for profit attributable to equity holders of the Company"))
        self.assertFalse(_net_income_candidate_allowed("Basic and diluted EPS attributable to equity holders of the Company"))

    def test_split_attribution_context_is_bounded_and_eps_is_excluded(self) -> None:
        rows = (
            _row(("Attributable to:", 10, 80)),
            _row(("Equity holders of the Company", 10, 180), ("188,243", 220, 270), ("224,822", 300, 350)),
            _row(("Earnings per share for profit attributable to equity holders", 10, 240)),
        )
        context = _attribution_context(rows, 1)
        self.assertEqual(context, "Attributableto:")
        self.assertTrue(_net_income_candidate_allowed(context + " Equity holders of the Company"))
        self.assertFalse(_net_income_candidate_allowed("Earnings per share for profit attributable to equity holders of the Company"))

    def test_hkd_million_is_not_treated_as_usd(self) -> None:
        self.assertEqual(_unit_scale("HK$ million"), (1_000_000.0, "HKD"))

    def test_curly_rmb_thousands_marker_is_explicit(self) -> None:
        self.assertEqual(_unit_scale("RMB’000"), (1_000.0, "CNY"))

    def test_non_calendar_fy_end_is_preserved_and_period_start_derived(self) -> None:
        filing = FilingDocument(
            "hkex:alibaba", "00700.HK", "alibaba", "ANNUAL_REPORT", "FY",
            "2026-03-31", "2026-06-18", "FY 2026 Annual Report", "https://example.invalid/report.pdf",
        )
        manifest = _manifest_for(filing)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.period_end, "2026-03-31")
        self.assertEqual(_period_start(manifest), "2025-04-01")

    def test_plural_statement_titles_are_formal_contexts(self) -> None:
        self.assertEqual(_statement_context("Consolidated Income Statements"), ("income_statement", "consolidated"))
        self.assertEqual(_statement_context("Consolidated Balance Sheets"), ("balance_sheet", "consolidated"))
        self.assertEqual(_statement_context("Consolidated Statements of Cash Flows"), ("cash_flow", "consolidated"))

    def test_column_level_currency_follows_visual_unit_header(self) -> None:
        rows = (
            _row(("2024", 100, 140), ("2025", 220, 260), ("US$", 320, 360)),
            _row(("RMB", 100, 140), ("RMB", 220, 260), ("US$", 320, 360)),
            _row(("(in millions)", 10, 90)),
        )
        columns = _period_columns(rows)
        target = next(column for column in columns if column.year == 2025)
        self.assertEqual(target.currency, "CNY")
        self.assertEqual(target.unit_scale, 1_000_000.0)

    def test_reporting_currency_wins_over_current_year_convenience_translation(self) -> None:
        rows = (
            _row(("Consolidated", 10, 90), ("Balance", 95, 145), ("Sheets", 150, 190)),
            _row(("2024", 357, 397), ("2025", 455, 495)),
            _row(("RMB", 357, 397), ("RMB", 423, 463), ("US$", 488, 528)),
            _row(("(in millions)", 10, 90)),
            _row(("Total", 10, 45), ("assets", 48, 90), ("1,764,829", 367, 407),
                 ("1,804,227", 432, 472), ("248,629", 500, 540)),
        )

        sections = _page_sections(
            None,
            "Consolidated Balance Sheets\n2024 2025\nRMB RMB US$\n(in millions)",
            rows,
            247,
            "CNY",
        )

        self.assertEqual(len(sections), 1)
        context = sections[0].context
        self.assertEqual(context.currency, "CNY")
        current = next(column for column in context.periods if column.year == 2025)
        self.assertEqual(current.currency, "CNY")
        self.assertLess(current.center, 470)

    def test_table_wide_currency_label_does_not_replace_period_column_center(self) -> None:
        rows = (
            _row(("CNY", 60, 90), ("2025", 430, 470), ("2024", 500, 540)),
            _row(
                ("Revenue", 60, 120),
                ("45", 390, 405),
                ("803,964,958", 430, 475),
                ("777,102,455", 500, 545),
            ),
        )

        columns = _period_columns(rows, "CNY")
        selected = _select_period_cell(rows[1], 120, columns, 2025)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "803,964,958")
        self.assertEqual(columns[0].center, 450)

    def test_missing_one_core_statement_keeps_verified_fields_with_warning(self) -> None:
        filing = _filing("partial")
        facts = list(_core(filing))
        facts = [fact for fact in facts if fact.concept != "operating_cash_flow"]
        result = FinancialIngestionEngine()._validate_group(list(facts), ("partial", "2025-12-31", "FY", "consolidated", "CNY"))
        self.assertEqual(result.validation.status.value, "READY_WITH_WARNINGS")
        self.assertIn("cash_flow_core_missing", result.validation.issues)
        self.assertEqual({fact.concept for fact in result.validation.accepted}, {fact.concept for fact in facts})
        self.assertEqual(result.validation.quarantined, ())

    def test_missing_provenance_rejects_group(self) -> None:
        filing = _filing("noprov")
        facts = list(_core(filing))
        for fact in facts:
            fact.source_url = ""
            fact.raw_text = ""
        result = FinancialIngestionEngine()._validate_group(facts, ("noprov", "2025-12-31", "FY", "consolidated", "CNY"))
        self.assertEqual(result.validation.status.value, "REJECTED")
        self.assertIn("provenance_missing", result.validation.issues)

    def test_good_group_keeps_verified_status_when_other_group_is_bad(self) -> None:
        good, bad = _filing("good-status"), _filing("bad-status", end="2024-12-31")
        bad_facts = list(_core(bad)); bad_facts[-1].value = 20
        source = InMemoryFinancialSource({"good-status": _core(good), "bad-status": tuple(bad_facts)})
        dataset = FinancialIngestionEngine().ingest(_company(), [good, bad], structured_sources=(source,))
        good_facts = [fact for fact in dataset.accepted_facts if fact.accession_number == "good-status"]
        self.assertTrue(good_facts)
        self.assertTrue(all(fact.validation_status == "VERIFIED" for fact in good_facts))
    def test_half_year_title_is_not_misclassified_as_fy(self) -> None:
        filing = _filing("h1", period="H1", end="2026-06-30")
        dataset = FinancialIngestionEngine().ingest(_company(), [filing], structured_sources=(InMemoryFinancialSource({"h1": _core(filing)}),))
        self.assertEqual(dataset.manifest[0].fiscal_period, "H1")
        self.assertEqual(dataset.manifest[0].period_end, "2026-06-30")

    def test_groups_with_different_units_are_validated_independently(self) -> None:
        first, second = _filing("f1"), _filing("f2")
        source = InMemoryFinancialSource({"f1": _core(first, 1.0), "f2": _core(second, 1000.0)})
        dataset = FinancialIngestionEngine().ingest(_company(), [first, second], structured_sources=(source,))
        self.assertEqual(dataset.status.value, "VERIFIED")
        self.assertEqual(len(dataset.group_validations), 2)
        self.assertEqual({fact.unit_scale for fact in dataset.accepted_facts}, {1.0, 1000.0})

    def test_bad_group_is_quarantined_without_discarding_good_group(self) -> None:
        good, bad = _filing("good"), _filing("bad", end="2024-12-31")
        bad_facts = list(_core(bad)); bad_facts[-1].value = 20
        source = InMemoryFinancialSource({"good": _core(good), "bad": tuple(bad_facts)})
        dataset = FinancialIngestionEngine().ingest(_company(), [good, bad], structured_sources=(source,))
        self.assertEqual(dataset.status.value, "READY_WITH_WARNINGS")
        self.assertTrue(any(f.accession_number == "good" for f in dataset.accepted_facts))
        self.assertTrue(dataset.validation.quarantined)

    def test_structured_source_has_priority_and_failure_falls_back_to_pdf(self) -> None:
        path = _official_pdf("OPENTHESIS_CATL_2025_PDF", "CN_A_SZSE_300750.SZ/1225002214.pdf")
        if not Path(path).is_file():
            self.skipTest("set OPENTHESIS_CATL_2025_PDF to run the official PDF integration test")
        filing = _filing(path=path)
        source = InMemoryFinancialSource({filing.document_id: _core(filing)}, failure_by_document={})
        dataset = FinancialIngestionEngine().ingest(_company(), [filing], structured_sources=(source,))
        self.assertEqual(dataset.accepted_facts[0].value, 1000)
        failed = InMemoryFinancialSource({}, failure_by_document={filing.document_id: "structured_timeout"})
        fallback = FinancialIngestionEngine().ingest(_company(), [filing], structured_sources=(failed,))
        self.assertTrue(any(f.concept == "revenue" for f in fallback.accepted_facts))
        self.assertTrue(any("structured_timeout" in item for item in fallback.diagnostics))

    def test_structured_full_concepts_but_bad_quality_falls_back_to_pdf(self) -> None:
        filing = _filing("structured-bad")
        filing.local_path = __file__
        bad = list(_core(filing))
        bad[-1].value = 20  # breaks assets = liabilities + equity

        class Adapter:
            def fetch(self, _company, _filing):
                return bad, [], None

        engine = FinancialIngestionEngine()
        pdf_facts = list(_core(filing))
        with patch.object(engine, "_parse_pdf_ast", return_value=(pdf_facts, [])):
            dataset = engine.ingest(_company(), [filing], structured_sources=(Adapter(),))
        # A conflicting source value is quarantined by the canonical
        # compiler; it must not silently fall back or overwrite the PDF
        # candidate with a last-write-wins result.
        self.assertEqual(dataset.status.value, "REJECTED")
        self.assertFalse(dataset.accepted_facts)
        self.assertTrue(any("compiler_quality_gate_failed" in item for item in dataset.diagnostics))

    def test_public_engine_parses_real_catl_pdf_with_statement_provenance(self) -> None:
        path = _official_pdf("OPENTHESIS_CATL_2025_PDF", "CN_A_SZSE_300750.SZ/1225002214.pdf")
        if not Path(path).is_file():
            self.skipTest("set OPENTHESIS_CATL_2025_PDF to run the official PDF integration test")
        dataset = FinancialIngestionEngine().ingest(_company(), [_filing(path=path)])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        for concept, expected in CATL_EXPECTED.items():
            self.assertIn(concept, values)
            self.assertAlmostEqual(values[concept].value, expected)
            self.assertEqual(values[concept].consolidated_scope, "consolidated")
            self.assertEqual(values[concept].unit_scale, 1000.0 if concept != "reported_roe" else 1.0)
            self.assertTrue(values[concept].source_page)
            self.assertTrue(values[concept].raw_text)
            self.assertTrue(values[concept].statement)
        self.assertEqual(dataset.validation.status.value, "VERIFIED")
        self.assertTrue(any(ref.bbox for ref in dataset.evidence))

    def test_public_engine_parses_real_smic_annual_pdf(self) -> None:
        path = _official_pdf("OPENTHESIS_SMIC_2025_PDF", "CN_A_SSE_688981.SH/1225037057.pdf")
        if not Path(path).is_file():
            self.skipTest("SMIC official PDF unavailable")
        company = Company("CN_A:SSE:688981.SH", "688981.SH", "中芯国际", "SSE", "CN:SMIC", "CN_A", "CN_A:SSE:688981.SH", "CNY", "CNY", "CAS")
        filing = FilingDocument("cninfo:1225037057", company.security_id, "1225037057", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-03-27", "中芯国际2025年年度报告", "https://static.cninfo.com.cn/finalpage/2026-03-27/1225037057.PDF", local_path=path)
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {"revenue": 67_323_192_000.0, "net_income": 5_040_734_000.0, "operating_cash_flow": 20_080_979_000.0, "assets": 367_718_196_000.0, "liabilities": 121_355_828_000.0, "equity": 150_823_788_000.0}
        self.assertEqual(dataset.status.value, "VERIFIED")
        for concept, number in expected.items():
            self.assertIn(concept, values)
            self.assertAlmostEqual(values[concept].value, number)
            self.assertEqual(values[concept].unit_scale, 1000.0)
            self.assertEqual(values[concept].consolidated_scope, "consolidated")
            self.assertEqual(values[concept].currency, "CNY")
            self.assertTrue(values[concept].source_page)

    def test_public_engine_parses_real_jinbo_annual_pdf(self) -> None:
        path = _official_pdf("OPENTHESIS_JINBO_2025_PDF", "CN_A_BSE_832982.BJ/1225267792.pdf")
        if not Path(path).is_file():
            self.skipTest("Jinbo official PDF unavailable")
        company = Company("CN_A:BSE:832982.BJ", "832982.BJ", "锦波生物", "BSE", "CN:JINBO", "CN_A", "CN_A:BSE:832982.BJ", "CNY", "CNY", "CAS")
        filing = FilingDocument("cninfo:1225267792", company.security_id, "1225267792", "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-29", "2025年年度报告", "https://static.cninfo.com.cn/finalpage/2026-04-29/1225267792.PDF", local_path=path)
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {"revenue": 1_595_372_919.28, "net_income": 652_128_968.10, "operating_cash_flow": 630_002_189.99, "assets": 2_583_855_317.33, "liabilities": 690_125_814.23, "equity": 1_895_253_856.52}
        self.assertEqual(dataset.status.value, "VERIFIED")
        for concept, number in expected.items():
            self.assertIn(concept, values)
            self.assertAlmostEqual(values[concept].value, number)
            self.assertEqual(values[concept].unit_scale, 1.0)
            self.assertEqual(values[concept].consolidated_scope, "consolidated")
            self.assertEqual(values[concept].currency, "CNY")
            self.assertTrue(values[concept].source_page)

    def test_public_engine_parses_real_moutai_annual_pdf(self) -> None:
        path = _cn_acceptance_pdf(
            "OPENTHESIS_MOUTAI_2025_PDF",
            "CN_A_SSE_600519.SH/1225114741.pdf",
            "600519-2025FY.pdf",
        )
        if not Path(path).is_file():
            self.skipTest("Moutai official acceptance PDF unavailable")
        company = Company(
            "CN_A:SSE:600519.SH", "600519.SH", "贵州茅台", "SSE",
            "CN:MOUTAI", "CN_A", "CN_A:SSE:600519.SH", "CNY", "CNY", "CAS",
        )
        filing = FilingDocument(
            "cninfo:1225114741", company.security_id, "1225114741", "ANNUAL_REPORT",
            "FY", "2025-12-31", "2026-04-17", "贵州茅台2025年年度报告",
            "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF",
            local_path=path,
        )
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {
            "revenue": 172_054_171_890.91,
            "net_income": 82_320_067_101.68,
            "operating_cash_flow": 61_522_204_989.35,
            "assets": 303_834_844_021.44,
            "liabilities": 49_875_590_112.37,
            "equity": 244_637_811_032.18,
            "total_equity": 253_959_253_909.07,
        }
        self.assertEqual(dataset.status.value, "VERIFIED")
        for concept, number in expected.items():
            self.assertIn(concept, values)
            self.assertAlmostEqual(values[concept].value, number)
            self.assertEqual(values[concept].unit_scale, 1.0)
            self.assertEqual(values[concept].consolidated_scope, "consolidated")
            self.assertEqual(values[concept].currency, "CNY")
            self.assertTrue(values[concept].source_page)
            self.assertTrue(values[concept].raw_text)

    def test_public_engine_parses_real_wuliangye_annual_pdf(self) -> None:
        path = _cn_acceptance_pdf(
            "OPENTHESIS_WULIANGYE_2025_PDF",
            "CN_A_SZSE_000858.SZ/7b179575-3e07-4607-8d7e-d1bb9c4c8786.pdf",
            "000858-2025FY.pdf",
        )
        if not Path(path).is_file():
            self.skipTest("Wuliangye official acceptance PDF unavailable")
        company = Company(
            "CN_A:SZSE:000858.SZ", "000858.SZ", "五粮液", "SZSE",
            "CN:WULIANGYE", "CN_A", "CN_A:SZSE:000858.SZ", "CNY", "CNY", "CAS",
        )
        filing = FilingDocument(
            "szse:7b179575-3e07-4607-8d7e-d1bb9c4c8786", company.security_id,
            "7b179575-3e07-4607-8d7e-d1bb9c4c8786", "ANNUAL_REPORT",
            "FY", "2025-12-31", "2026-04-30", "五粮液2025年年度报告",
            "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-30/7b179575-3e07-4607-8d7e-d1bb9c4c8786.PDF",
            local_path=path,
        )
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {
            "revenue": 40_528_509_770.23,
            "net_income": 8_954_257_202.51,
            "operating_cash_flow": 29_706_259_919.13,
            "assets": 189_984_270_815.47,
            "liabilities": 67_803_587_170.33,
            "equity": 119_932_271_234.99,
            "total_equity": 122_180_683_645.14,
        }
        self.assertEqual(dataset.status.value, "VERIFIED")
        for concept, number in expected.items():
            self.assertIn(concept, values)
            self.assertAlmostEqual(values[concept].value, number)
            self.assertEqual(values[concept].unit_scale, 1.0)
            self.assertEqual(values[concept].consolidated_scope, "consolidated")
            self.assertEqual(values[concept].currency, "CNY")
            self.assertTrue(values[concept].source_page)
            self.assertTrue(values[concept].raw_text)

    def test_public_engine_parses_real_tencent_annual_pdf(self) -> None:
        path = _acceptance_pdf(
            "OPENTHESIS_TENCENT_2025_PDF",
            "HK_HKEX_00700.HK/2026040901231.pdf",
            "00700.HK_2026040901231.pdf",
        )
        if not Path(path).is_file():
            self.skipTest("Tencent official acceptance PDF unavailable")
        company = Company(
            "HK:SEHK:00700.HK", "00700.HK", "Tencent Holdings", "SEHK",
            "HKEX:00700", "HK", "HK:SEHK:00700.HK", "CNY", "CNY", "IFRS",
        )
        source_url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.pdf"
        filing = FilingDocument(
            "hkex:2026040901231", company.security_id, "2026040901231",
            "ANNUAL_REPORT", "FY", "2025-12-31", "2026-04-09",
            "2025 Annual Report", source_url, local_path=path,
            content_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        )
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {
            "revenue": 751_766_000_000.0,
            "net_income": 224_842_000_000.0,
            "operating_cash_flow": 303_052_000_000.0,
            "assets": 2_038_986_000_000.0,
            "liabilities": 797_921_000_000.0,
            "equity": 1_154_152_000_000.0,
            "total_equity": 1_241_065_000_000.0,
        }
        expected_pages = {
            "revenue": 130, "net_income": 130, "assets": 132,
            "equity": 133, "total_equity": 133, "liabilities": 134,
            "operating_cash_flow": 139,
        }
        self.assertEqual(dataset.status.value, "VERIFIED")
        self.assertTrue(dataset.accepted_facts)
        for concept, number in expected.items():
            self.assertIn(concept, values)
            fact = values[concept]
            self.assertAlmostEqual(fact.value, number)
            self.assertEqual(fact.unit_scale, 1_000_000.0)
            self.assertEqual(fact.currency, "CNY")
            self.assertEqual(fact.consolidated_scope, "consolidated")
            self.assertEqual(fact.fiscal_period, "FY")
            self.assertEqual(fact.end_date, "2025-12-31")
            self.assertEqual(fact.source_page, expected_pages[concept])
            self.assertTrue(fact.source_bbox)
            self.assertTrue(fact.raw_text)
            self.assertTrue(fact.statement)

    def test_public_engine_parses_real_meituan_annual_pdf(self) -> None:
        path = _acceptance_pdf("OPENTHESIS_MEITUAN_2025_PDF", "HK_HKEX_03690.HK/2026042400179.pdf", "03690.HK_2026042400179.pdf")
        if not Path(path).is_file():
            self.skipTest("Meituan official acceptance PDF unavailable")
        company = _hk_company("03690.HK", "Meituan", "HKEX:03690", "IFRS")
        url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0424/2026042400179.pdf"
        dataset = FinancialIngestionEngine().ingest(company, [_hk_filing(company, "2026042400179", "2025-12-31", "2026-04-24", "FY 2025 Annual Report", url, path)])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {"revenue": 364_854_746_000.0, "net_income": -23_355_015_000.0, "assets": 346_910_280_000.0, "liabilities": 195_922_428_000.0, "equity": 151_045_913_000.0, "total_equity": 150_987_852_000.0, "operating_cash_flow": -13_815_001_000.0}
        pages = {"revenue": 212, "net_income": 212, "assets": 214, "equity": 214, "total_equity": 214, "liabilities": 215, "operating_cash_flow": 220}
        self.assertEqual(dataset.status.value, "VERIFIED")
        self.assertEqual(dataset.manifest[0].period_end, "2025-12-31")
        for concept, number in expected.items():
            fact = values[concept]
            self.assertAlmostEqual(fact.value, number)
            self.assertEqual((fact.currency, fact.unit_scale, fact.consolidated_scope, fact.source_page), ("CNY", 1000.0, "consolidated", pages[concept]))
            self.assertTrue(fact.source_bbox and fact.raw_text and fact.statement)

    def test_public_engine_parses_real_meituan_2022_operating_metrics(self) -> None:
        path = _official_pdf("OPENTHESIS_MEITUAN_2022_PDF", "HK_HKEX_03690.HK/2023042400042.pdf")
        if not Path(path).is_file():
            self.skipTest("Meituan 2022 official PDF unavailable")
        company = _hk_company("03690.HK", "Meituan", "HKEX:03690", "IFRS")
        filing = _hk_filing(
            company,
            "2023042400042",
            "2022-12-31",
            "2023-04-24",
            "2022 Annual Report",
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2023/0424/2023042400042.pdf",
            path,
        )
        dataset = FinancialIngestionEngine().ingest(company, [filing])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        self.assertEqual(dataset.status.value, "VERIFIED")
        for concept, expected, page in (
            ("operating_cash_flow", 11_411_448_000.0, 199),
            ("operating_income", -5_820_448_000.0, 193),
            ("capital_expenditure", 5_731_304_000.0, 199),
            ("gross_profit", 61_752_979_000.0, 193),
        ):
            fact = values[concept]
            self.assertAlmostEqual(fact.value, expected)
            self.assertEqual((fact.currency, fact.unit_scale, fact.consolidated_scope, fact.source_page), ("CNY", 1000.0, "consolidated", page))
            self.assertTrue(fact.source_bbox and fact.raw_text)
        profile = build_financial_profile(dataset.accepted_facts, dataset.group_validations, "CNY")
        metrics = next(item for item in profile.metrics if item["year"] == 2022)
        self.assertAlmostEqual(metrics["operating_margin"], -0.026462001, places=8)
        self.assertAlmostEqual(metrics["free_cash_flow"], 5_680_144_000.0)

    def test_public_engine_parses_real_byd_annual_pdf(self) -> None:
        path = _acceptance_pdf("OPENTHESIS_BYD_2025_PDF", "HK_HKEX_01211.HK/2026032703008.pdf", "01211.HK_2026032703008.pdf")
        if not Path(path).is_file():
            self.skipTest("BYD official acceptance PDF unavailable")
        company = _hk_company("01211.HK", "BYD Company", "HKEX:01211", "CAS")
        url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0327/2026032703008.pdf"
        dataset = FinancialIngestionEngine().ingest(company, [_hk_filing(company, "2026032703008", "2025-12-31", "2026-03-27", "FY 2025 Annual Report", url, path)])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {"revenue": 803_964_958_000.0, "net_income": 32_619_022_000.0, "assets": 883_729_883_000.0, "liabilities": 625_190_698_000.0, "equity": 246_274_606_000.0, "total_equity": 258_539_185_000.0, "operating_cash_flow": 59_135_544_000.0}
        pages = {"revenue": 144, "net_income": 144, "assets": 141, "equity": 143, "total_equity": 143, "liabilities": 142, "operating_cash_flow": 148}
        self.assertEqual(dataset.status.value, "VERIFIED")
        self.assertEqual(dataset.manifest[0].period_end, "2025-12-31")
        for concept, number in expected.items():
            fact = values[concept]
            self.assertAlmostEqual(fact.value, number)
            self.assertEqual((fact.currency, fact.unit_scale, fact.consolidated_scope, fact.source_page), ("CNY", 1000.0, "consolidated", pages[concept]))
            self.assertTrue(fact.source_bbox and fact.raw_text and fact.statement)

    def test_public_engine_parses_real_byd_q1_comparison_pair(self) -> None:
        company = _hk_company("01211.HK", "BYD Company", "HKEX:01211", "CAS")
        cases = (
            (
                "2026042803001", "2026-03-31", "2026-04-28",
                "First Quarterly Report 2026",
                "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0428/2026042803001.pdf",
                _acceptance_pdf(
                    "OPENTHESIS_BYD_2026_Q1_PDF",
                    "HK_HKEX_01211.HK/2026042803001.pdf",
                    "2026042803001.pdf",
                ),
            ),
            (
                "2025042502125", "2025-03-31", "2025-04-25",
                "First Quarterly Report 2025",
                "https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0425/2025042502125.pdf",
                _acceptance_pdf(
                    "OPENTHESIS_BYD_2025_Q1_PDF",
                    "HK_HKEX_01211.HK/2025042502125.pdf",
                    "2025042502125.pdf",
                ),
            ),
        )
        if not all(Path(item[-1]).is_file() for item in cases):
            self.skipTest("BYD official Q1 comparison PDFs unavailable")
        filings = [
            FilingDocument(
                f"hkex:{accession}", company.security_id, accession,
                "QUARTERLY_REPORT", "Q1", end, filed, title, url,
                local_path=path, content_hash=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            )
            for accession, end, filed, title, url, path in cases
        ]

        dataset = FinancialIngestionEngine().ingest(company, filings)
        grouped = {
            (fact.fiscal_year, fact.fiscal_period, fact.concept): fact
            for fact in dataset.accepted_facts
        }
        self.assertEqual(
            dataset.status.value,
            "VERIFIED",
            msg={
                "diagnostics": dataset.diagnostics,
                "groups": [
                    {
                        "status": item.validation.status.value,
                        "issues": item.validation.issues,
                        "concepts": sorted(fact.concept for fact in item.validation.accepted),
                        "quarantined": sorted(fact.concept for fact in item.validation.quarantined),
                    }
                    for item in dataset.group_validations
                ],
            },
        )
        expected_2026 = {
            "revenue": 150_225_314_000.0,
            "net_income": 4_084_551_000.0,
            "operating_cash_flow": 2_790_305_000.0,
            "assets": 902_076_965_000.0,
            "liabilities": 639_956_542_000.0,
            "equity": 249_916_729_000.0,
            "total_equity": 262_120_423_000.0,
        }
        for concept, expected in expected_2026.items():
            fact = grouped[(2026, "Q1", concept)]
            self.assertAlmostEqual(fact.value, expected)
            self.assertEqual((fact.currency, fact.unit_scale, fact.consolidated_scope), ("CNY", 1.0, "consolidated"))
            self.assertTrue(fact.source_page and fact.source_bbox and fact.raw_text)

        interim = calculate_interim_metrics([
            {
                "concept": fact.concept,
                "value": fact.value,
                "fiscal_year": fact.fiscal_year,
                "fiscal_period": fact.fiscal_period,
                "filed_at": fact.filed_at,
                "end_date": fact.end_date,
            }
            for fact in dataset.accepted_facts
        ])
        latest = next(item for item in interim if item["year"] == 2026 and item["period"] == "Q1")
        self.assertEqual(latest["comparison_period"], "2025 Q1")
        self.assertIsNone(latest["comparison_gap"])
        self.assertAlmostEqual(
            latest["revenue_growth"],
            150_225_314_000.0 / 170_360_448_000.0 - 1.0,
            places=9,
        )

    def test_public_engine_parses_real_alibaba_annual_pdf(self) -> None:
        path = _acceptance_pdf("OPENTHESIS_ALIBABA_2026_PDF", "HK_HKEX_09988.HK/2026061800844.pdf", "09988.HK_2026061800844.pdf")
        if not Path(path).is_file():
            self.skipTest("Alibaba official acceptance PDF unavailable")
        company = _hk_company("09988.HK", "Alibaba Group", "HKEX:09988", "US_GAAP")
        url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0618/2026061800844.pdf"
        dataset = FinancialIngestionEngine().ingest(company, [_hk_filing(company, "2026061800844", "2026-03-31", "2026-06-18", "FY 2026 Annual Report", url, path)])
        values = {fact.concept: fact for fact in dataset.accepted_facts}
        expected = {"revenue": 1_023_670_000_000.0, "net_income": 103_592_000_000.0, "assets": 1_909_570_000_000.0, "liabilities": 783_300_000_000.0, "total_equity": 1_118_425_000_000.0, "operating_cash_flow": 76_213_000_000.0}
        pages = {"revenue": 183, "net_income": 183, "assets": 185, "liabilities": 185, "total_equity": 186, "operating_cash_flow": 190}
        self.assertEqual(dataset.status.value, "VERIFIED")
        self.assertEqual(dataset.manifest[0].period_end, "2026-03-31")
        for concept, number in expected.items():
            fact = values[concept]
            self.assertAlmostEqual(fact.value, number)
            self.assertEqual((fact.currency, fact.unit_scale, fact.consolidated_scope, fact.source_page), ("CNY", 1_000_000.0, "consolidated", pages[concept]))
            self.assertTrue(fact.source_bbox and fact.raw_text and fact.statement)


if __name__ == "__main__":
    unittest.main()
