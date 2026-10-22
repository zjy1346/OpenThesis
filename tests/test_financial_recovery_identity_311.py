from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from openthesis.domain import Company
from openthesis.financial_checkpoint import (
    CheckpointKey,
    PdfWindow,
    WorkerIsolationUnavailable,
    WindowCheckpointStore,
    run_checkpointed_windows,
)
from openthesis.domain import EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_contracts import (
    CandidateBatch,
    FactCandidate,
    FactGroupValidation,
    FinancialCandidateCollection,
    FinancialDataset,
    FinancialSourceFailure,
)
from openthesis.financial_pipeline import document_processing_outcome
from openthesis.financial_ingestion import (
    FilingManifest,
    FinancialIngestionEngine,
)
from openthesis.financial_sources.pdf_document_units import PdfDocumentContext


class CheckpointRecovery311Tests(unittest.TestCase):
    def test_deterministic_failure_is_classified_before_retry_and_keeps_successful_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            key = CheckpointKey("same-pdf", "parser-v1", "rules-v1")
            windows = (PdfWindow(0, (1,)), PdfWindow(1, (2,)), PdfWindow(2, (3,)))
            calls: list[int] = []

            def worker(window: PdfWindow, context: dict[str, object]):
                calls.append(window.index)
                if window.index == 1:
                    raise ValueError("stable malformed table")
                return (), (), {"prior_page": context.get("prior_page"), "last_page": window.pages[-1]}

            def deterministic(error: Exception) -> bool:
                return isinstance(error, ValueError)

            first = run_checkpointed_windows(
                store, key, windows, worker, max_retries=1,
                input_fingerprint="same-input", deterministic_failure=deterministic,
            )

            self.assertEqual(calls, [0, 1])
            self.assertEqual([item.window.index for item in first], [0])
            self.assertEqual(first[0].context["last_page"], 1)
            self.assertIsNotNone(store.load_exhausted(key, input_fingerprint="same-input"))
            self.assertEqual(store.load_exhausted(key, input_fingerprint="same-input")["failed_window"],
                             {"index": 1, "pages": [2]})

            resumed = run_checkpointed_windows(
                store, key, windows, worker, max_retries=1,
                input_fingerprint="same-input", deterministic_failure=deterministic,
            )
            self.assertEqual([item.window.index for item in resumed], [0])
            self.assertEqual(calls, [0, 1])

    def test_transient_failure_keeps_bounded_retry_and_never_exhausts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            key = CheckpointKey("transient-pdf", "parser-v1", "rules-v1")
            calls: list[int] = []

            def worker(window: PdfWindow, context: dict[str, object]):
                calls.append(window.index)
                if window.index == 1 and calls.count(1) == 1:
                    raise TimeoutError("parser timeout")
                return (), (), {"prior_page": context.get("last_page"), "last_page": window.pages[-1]}

            completed = run_checkpointed_windows(
                store, key, (PdfWindow(0, (1,)), PdfWindow(1, (2,))), worker,
                max_retries=1, input_fingerprint="transient-input",
                deterministic_failure=lambda error: isinstance(error, ValueError),
            )

            self.assertEqual(calls, [0, 1, 1])
            self.assertEqual([item.window.index for item in completed], [0, 1])
            self.assertEqual(completed[1].context["prior_page"], 1)
            self.assertIsNone(store.load_exhausted(key, input_fingerprint="transient-input"))

    def test_worker_isolation_failure_remains_resumable_without_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WindowCheckpointStore(directory)
            key = CheckpointKey("host-failure", "parser-v1", "rules-v1")
            calls: list[int] = []

            def worker(window: PdfWindow):
                calls.append(window.index)
                raise WorkerIsolationUnavailable()

            completed = run_checkpointed_windows(
                store, key, (PdfWindow(0, (1,)),), worker, max_retries=2,
                input_fingerprint="host-input", deterministic_failure=lambda _error: True,
            )

            self.assertEqual(completed, ())
            self.assertEqual(calls, [0])
            self.assertIsNone(store.load_exhausted(key, input_fingerprint="host-input"))

    def test_formal_pdf_resume_returns_prior_checkpoint_and_exhaustion_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "fixture.pdf"
            pdf_path.write_bytes(b"stable fixture PDF bytes")
            content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            filing = FilingDocument(
                "resume-doc", "issuer-1", "resume-accession", "ANNUAL_REPORT", "FY",
                "2025-12-31", "2026-03-01", "fixture.pdf", "https://example.test/fixture.pdf",
                local_path=str(pdf_path), content_hash=content_hash,
            )
            manifest = FilingManifest(
                filing.document_id, filing.accession_number, filing.source_url,
                filing.primary_document, filing.form_type, filing.fiscal_period,
                filing.period_end, filing.revision, filing.supersedes_document_id, content_hash,
            )
            company = Company("issuer-1", "FIX", "Fixture", market="CN_A")
            engine = FinancialIngestionEngine(
                checkpoint_dir=Path(directory) / "checkpoints", parse_timeout_seconds=5.0,
            )
            calls: list[int] = []
            fact = FinancialFact(
                "page-one-revenue", "issuer-1", "revenue", "Revenue", 100.0, "CNY", 2025,
                "FY", filing.form_type, "2025-01-01", filing.period_end, filing.filed_at,
                filing.accession_number, filing.source_url, statement="income_statement",
                currency="CNY", source_document=filing.document_id, source_page=1,
                raw_text="Revenue 100 CNY",
            )
            ref = EvidenceRef(
                f"fact:{fact.fact_id}", filing.document_id, filing.source_url, filing.primary_document,
                "page:1", fact.raw_text, filing.filed_at, filing.content_hash,
            )

            class LocalWindowWorker:
                def __call__(self, window: PdfWindow, _context: dict[str, object]):
                    calls.append(window.index)
                    if window.index == 1:
                        raise ValueError("stable malformed second table")
                    return (fact,), (ref,), {"last_page": 1}

            with patch("openthesis.financial_ingestion.read_document_context",
                       return_value=PdfDocumentContext(page_texts=("page one", "page two"))), \
                    patch("openthesis.financial_ingestion._manifest_from_observed_pages",
                          side_effect=lambda observed, *_args, **_kwargs: observed), \
                    patch("openthesis.financial_ingestion._CheckpointPdfWindowWorker",
                          side_effect=lambda *_args, **_kwargs: LocalWindowWorker()):
                first = engine.parse_local_pdf_resumable(
                    company, filing, manifest, candidate_pages=frozenset({1, 2}), window_size=1,
                )
                second = engine.parse_local_pdf_resumable(
                    company, filing, manifest, candidate_pages=frozenset({1, 2}), window_size=1,
                )

            self.assertEqual(calls, [0, 1])
            self.assertEqual([item.fact_id for item in first[0]], [fact.fact_id])
            self.assertEqual(first[2], ("pdf_window_exhausted_same_input",))
            self.assertEqual([item.fact_id for item in second[0]], [fact.fact_id])
            self.assertEqual(second[1], [ref])
            self.assertEqual(second[2], ("pdf_window_exhausted_same_input",))
            observation = engine._document_execution[filing.document_id]
            self.assertEqual(observation["source_hash"], content_hash)
            self.assertEqual(observation["completed_window_count"], 1)
            self.assertEqual(observation["window_total"], 2)
            self.assertEqual(observation["failure_stage"], "parse")
            self.assertEqual(observation["root_error_code"], "pdf_window_exhausted_same_input")
            self.assertEqual(observation["parser_failed_pages"], [2])

    def test_unknown_host_failure_is_not_a_permanent_document_failure(self) -> None:
        from openthesis.financial_ingestion import _is_deterministic_pdf_window_failure
        self.assertFalse(_is_deterministic_pdf_window_failure(PermissionError("host denied")))
        self.assertFalse(_is_deterministic_pdf_window_failure(RuntimeError("unknown native exit")))
        self.assertTrue(_is_deterministic_pdf_window_failure(ValueError("malformed table")))
        self.assertTrue(_is_deterministic_pdf_window_failure(RuntimeError("ValueError: malformed table")))

    def test_source_hash_conflict_stops_before_parsing_or_recovery_page_hints(self) -> None:
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.pdf"
            path.write_bytes(b"changed original bytes")
            filing = replace(_filing("changed", "0" * 64), local_path=str(path))
            manifest = FilingManifest(filing.document_id, filing.accession_number, filing.source_url,
                filing.primary_document, filing.form_type, filing.fiscal_period, filing.period_end,
                filing.revision, filing.supersedes_document_id, filing.content_hash)
            engine = FinancialIngestionEngine(checkpoint_dir=Path(directory) / "checkpoints")
            with patch("openthesis.financial_ingestion.read_document_context") as parse:
                result = engine.parse_local_pdf_resumable(Company("issuer-1", "FIX", "Fixture"), filing, manifest)
            self.assertEqual(result, ([], [], ("PDF_SOURCE_HASH_MISMATCH",)))
            parse.assert_not_called()


def test_failed_page_hint_prioritizes_only_previously_justified_necessary_pages(tmp_path):
    from test_flash_page_representation import _write_source_pdf
    from openthesis.financial_ingestion import _vision_failed_pages
    from openthesis.vision_financials import VisionFallbackConfig
    filing = _write_source_pdf(tmp_path / "scoped.pdf", (
        "Consolidated Income Statement", "Revenue 100 CNY", "Profit 80 CNY",
        "Unrelated shareholder address", "Private appendix"))
    config = VisionFallbackConfig(enabled=True, consent=True, provider="configured_model",
        configured_model_id="fixture", max_pages=2)
    pages = _vision_failed_pages(filing, config, parser_failed_pages=(3, 5, -1, True, 999))
    assert [page.original_page for page in pages] == [3, 1]
    assert 5 not in {page.original_page for page in pages}


def test_failed_page_hints_cross_the_existing_extractor_and_worker_seams(tmp_path):
    from test_vision_worker_diagnostics_310 import _config, _filing as source_filing
    from openthesis.financial_sources import VisionCandidateExtractor
    filing = source_filing(tmp_path)
    with patch("openthesis.financial_ingestion._run_pdfium_isolated", return_value=([], None)) as worker:
        batch = VisionCandidateExtractor(object(), object(), _config(),
            parser_failed_pages_by_document={filing.document_id: (2, 3)}).extract(
                Company("000000001", "EX", "Example Corp"), filing)
    assert worker.call_args.args[-1] == (2, 3)
    assert batch.diagnostics == ("vision_no_failed_pages",)


def test_financial_pipeline_routes_page_hints_only_from_the_current_original():
    from dataclasses import replace
    from openthesis.financial_pipeline import FinancialCandidatePipeline
    from openthesis.vision_financials import VisionFallbackConfig
    from test_financial_pipeline import _filing as target_filing, _subject
    filing = replace(target_filing(), content_hash="current-hash")
    class Engine:
        _document_execution = {filing.document_id: {
            "source_hash": "current-hash", "parser_failed_pages": [4, 5]}}
        def collect_candidate_batches(self, *_args, **_kwargs):
            return FinancialCandidateCollection((), {filing.document_id: (CandidateBatch(filing), CandidateBatch(filing))})
    engine = Engine()
    dataset = FinancialDataset((filing,), (), (), (), (), (), {}, (), False)
    selected = []
    def capture(_subject, _filings, extractor, **_kwargs):
        selected.append(extractor.parser_failed_pages_by_document)
        return {filing.document_id: CandidateBatch(filing)}
    with patch.object(FinancialCandidatePipeline, "compile", return_value=dataset), \
            patch("openthesis.financial_pipeline._prefetch_vision_batches", side_effect=capture):
        for source_hash in ("current-hash", "other-hash"):
            engine._document_execution[filing.document_id]["source_hash"] = source_hash
            FinancialCandidatePipeline().compile_from_ingestion(_subject(), (filing,), engine,
                vision_fallback=object(), vision_config=VisionFallbackConfig(enabled=True, consent=True,
                    provider="configured_model", configured_model_id="fixture",
                    require_page_approval=True, approve_upload=lambda _plan: True))
    assert selected == [{filing.document_id: (4, 5)}, {}]


def _filing(document_id: str, content_hash: str) -> FilingDocument:
    return FilingDocument(
        document_id, "issuer-1", "acc-1", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-01", f"{document_id}.pdf",
        f"https://example.test/{document_id}.pdf", content_hash=content_hash,
    )


def _fact(filing: FilingDocument, fact_id: str, *, page: int, statement: str) -> FinancialFact:
    return FinancialFact(
        fact_id, filing.company_cik, "revenue", "Revenue", 100.0, "CNY", 2025,
        "FY", filing.form_type, "2025-01-01", filing.period_end, filing.filed_at,
        filing.accession_number, filing.source_url, statement=statement, currency="CNY",
        source_document=filing.document_id, source_page=page, raw_text=f"Revenue {fact_id}",
    )


def _ref(filing: FilingDocument, fact: FinancialFact) -> EvidenceRef:
    return EvidenceRef(
        f"fact:{fact.fact_id}", filing.document_id, filing.source_url, filing.primary_document,
        f"page:{fact.source_page}", fact.raw_text, filing.filed_at, filing.content_hash,
    )


def _group(fact: FinancialFact, *, role: str, issues: tuple[str, ...]) -> FactGroupValidation:
    return FactGroupValidation(
        (fact.accession_number, fact.end_date, fact.fiscal_period,
         fact.consolidated_scope or fact.scope, fact.currency or fact.unit),
        "PARTIAL", issues, frozenset({fact.concept}), (fact,), (), role,
    )


class DocumentRecoveryIdentity311Tests(unittest.TestCase):
    def test_empty_document_candidate_set_does_not_borrow_same_accession_roles_or_issues(self) -> None:
        current = _filing("current-doc", "current-hash")
        other = _filing("other-doc", "other-hash")
        other_fact = _fact(other, "other-revenue", page=91, statement="income_statement")
        other_ref = _ref(other, other_fact)
        current_batch = CandidateBatch(current, (), (), ("CURRENT_BATCH_FAILED",))
        other_batch = CandidateBatch(other, (FactCandidate(other_fact, (other_ref,)),), (other_ref,),
                                     ("FOREIGN_BATCH_DIAGNOSTIC",))
        collection = FinancialCandidateCollection(
            (), {current.document_id: (current_batch,), other.document_id: (other_batch,)},
            (other_ref,), source_failures=(
                FinancialSourceFailure(current.document_id, current.accession_number,
                                       "parse", "CURRENT_PARSE_FAILED"),
                FinancialSourceFailure(other.document_id, other.accession_number,
                                       "parse", "FOREIGN_PARSE_FAILED"),
            ),
        )
        dataset = FinancialDataset(
            (current, other), (other_fact,), (), (other_ref,), (),
            (_group(other_fact, role="comparator", issues=("FOREIGN_CONFLICT",)),),
            {}, (), False,
        )

        outcome = document_processing_outcome(current, dataset, collection)

        self.assertEqual(outcome["status"], "needs-action")
        self.assertEqual(outcome["availability"], "none")
        self.assertEqual(outcome["processing_state"], "failed")
        self.assertEqual(outcome["roles"], [])
        self.assertEqual(outcome["issue_codes"], ["CURRENT_BATCH_FAILED", "CURRENT_PARSE_FAILED"])
        self.assertEqual(outcome["recognized_statements"], [])
        self.assertEqual(outcome["extracted_pages"], [])

    def test_same_group_identity_does_not_admit_other_hash_validation_metadata(self) -> None:
        current = _filing("current-doc", "current-hash")
        other = _filing("other-doc", "other-hash")
        current_fact = _fact(current, "current-revenue", page=3, statement="income_statement")
        other_fact = _fact(other, "other-revenue", page=91, statement="balance_sheet")
        current_ref = _ref(current, current_fact)
        other_ref = _ref(other, other_fact)
        collection = FinancialCandidateCollection(
            (), {
                current.document_id: (CandidateBatch(
                    current, (FactCandidate(current_fact, (current_ref,)),), (current_ref,), (),
                ),),
                other.document_id: (CandidateBatch(
                    other, (FactCandidate(other_fact, (other_ref,)),), (other_ref,), (),
                ),),
            }, (current_ref, other_ref),
        )
        dataset = FinancialDataset(
            (current, other), (current_fact, other_fact), (), (current_ref, other_ref), (),
            (
                _group(current_fact, role="target", issues=("CURRENT_VALIDATION_ISSUE",)),
                _group(other_fact, role="comparator", issues=("FOREIGN_VALIDATION_ISSUE",)),
            ), {}, (), False,
        )

        outcome = document_processing_outcome(current, dataset, collection)

        self.assertEqual(outcome["roles"], ["target"])
        self.assertEqual(outcome["issue_codes"], ["CURRENT_VALIDATION_ISSUE"])
        self.assertEqual(outcome["recognized_statements"], ["income_statement"])
        self.assertEqual(outcome["extracted_pages"], [3])


if __name__ == "__main__":
    unittest.main()
