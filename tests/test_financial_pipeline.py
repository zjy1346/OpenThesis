from __future__ import annotations

import ast
from concurrent.futures import CancelledError
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_admission import FinancialAdmission, FinancialResearchView
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import (
    CandidateBatch,
    CompilationPolicy,
    FactCandidate,
    FinancialDataset,
    FinancialSourceFailure,
    GapStageKind,
)
from openthesis.financial_ingestion import FinancialCandidateCollection
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.vision_financials import VisionAdapterError, VisionPageRequest


def _subject() -> Company:
    return Company(
        "CN_A:SZSE:002594.SZ", "002594.SZ", "BYD", "SZSE",
        market="CN_A", reporting_currency="CNY",
    )


def _filing() -> FilingDocument:
    return FilingDocument(
        "filing-2025", _subject().security_id, "accession-2025", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-01", "2025 Annual Report",
        "https://example.invalid/2025.pdf",
    )


def _fact(filing: FilingDocument) -> FinancialFact:
    return FinancialFact(
        "fact-2025-revenue", _subject().security_id, "revenue", "revenue", 100.0,
        "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31",
        filing.filed_at, filing.accession_number, filing.source_url,
        statement="income_statement", currency="CNY", consolidated_scope="consolidated",
        unit_provenance="structured_normalized", validation_status="VERIFIED",
        extraction_status="resolved", usage_status="target", provenance_status="verified",
        raw_text="Revenue 100 CNY FY2025",
    )


class RecordingCollector:
    def __init__(self, candidate: FactCandidate) -> None:
        self.candidate = candidate
        self.calls = []

    def collect_candidate_batches(self, subject, filings, **kwargs):
        self.calls.append((subject, tuple(filings), kwargs))
        filing = filings[0]
        return FinancialCandidateCollection(
            manifests=(),
            batches_by_document={
                filing.document_id: (
                    CandidateBatch(filing, (self.candidate,)),
                    CandidateBatch(filing),
                )
            },
        )


class RecordingCompiler:
    def __init__(self, dataset: FinancialDataset) -> None:
        self.dataset = dataset
        self.calls = []

    def compile(self, subject, batches, policy):
        self.calls.append((subject, tuple(batches), policy))
        assert len(batches) == 1
        assert batches[0].candidates[0].fact.fact_id == self.dataset.resolved_facts[0].fact_id
        return self.dataset


class RecordingAdmission(FinancialAdmission):
    def __init__(self):
        self.calls = []

    def admit(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return super().admit(*args, **kwargs)


class FinancialPipelineTests(unittest.TestCase):
    def test_typed_source_failure_survives_without_a_diagnostic_string(self):
        current = _filing()
        prior = replace(current, document_id="prior", accession_number="prior-2024", period_end="2024-12-31")
        failure = FinancialSourceFailure(prior.document_id, prior.accession_number, "parse", "FILING_LOCAL_PARSE_FAILED")
        prior_fact = replace(_fact(prior), fact_id="prior-revenue", fiscal_year=2024,
                             end_date="2024-12-31", start_date="2024-01-01")
        class Collector:
            def collect_candidate_batches(self, _subject, _filings, **_kwargs):
                return FinancialCandidateCollection((), {
                    current.document_id: (CandidateBatch(current, (FactCandidate(_fact(current)),)),),
                    prior.document_id: (CandidateBatch(prior, (FactCandidate(prior_fact),)),),
                }, source_failures=(failure,))
        pipeline = FinancialPipeline(Collector())
        policy = CompilationPolicy(reporting_currency="CNY")
        view = pipeline.prepare(FinancialResearchSnapshot(_subject(), (current, prior)), policy)
        self.assertEqual(view.canonical_dataset.source_failures, (failure,))
        self.assertEqual(view.publishable_accessions, (current.accession_number,))
        revalidated = pipeline.prepare(FinancialResearchSnapshot.from_canonical(
            _subject(), (current, prior), view.canonical_dataset,
        ), policy)
        self.assertEqual(revalidated.canonical_dataset.source_failures, (failure,))
        self.assertEqual(revalidated.publishable_accessions, (current.accession_number,))

    def test_canonical_snapshot_keeps_existing_and_additional_typed_failures(self):
        current = _filing()
        first = FinancialSourceFailure(current.document_id, current.accession_number,
                                       "parse", "FILING_LOCAL_PARSE_FAILED")
        second = replace(first, stage="download", code="FILING_DOWNLOAD_FAILED")
        canonical = SimpleNamespace(resolved_facts=(), evidence=(), source_failures=(first,))
        snapshot = FinancialResearchSnapshot.from_canonical(
            _subject(), (current,), canonical, source_failures=(first, second),
        )
        self.assertEqual(snapshot.source_failures, (first, second))
        empty = FinancialResearchSnapshot.from_canonical(
            _subject(), (current,), canonical, source_failures=(),
        )
        self.assertEqual(empty.source_failures, (first,))

    def test_safe_partial_fact_publication_does_not_claim_complete_coverage(self):
        filing = _filing()
        collector = RecordingCollector(FactCandidate(_fact(filing), extractor="structured"))
        view = FinancialPipeline(collector).prepare(
            FinancialResearchSnapshot(_subject(), (filing,)),
            CompilationPolicy(reporting_currency="CNY"),
        )
        self.assertEqual(len(view.facts), 1)
        self.assertEqual(view.publishable_accessions, (filing.accession_number,))
        self.assertFalse(view.canonical_dataset.allow_ai)
        self.assertNotEqual(view.readiness.level.value, "full")

    def test_parser_failure_fences_only_affected_sibling_publication(self):
        current = _filing()
        prior = replace(current, document_id="prior", accession_number="prior-2024", period_end="2024-12-31")
        current_fact = _fact(current)
        prior_fact = replace(_fact(prior), fact_id="prior-revenue", fiscal_year=2024,
                             end_date="2024-12-31", start_date="2024-01-01")
        class Collector:
            def collect_candidate_batches(self, _subject, filings, **_kwargs):
                return FinancialCandidateCollection((), {
                    current.document_id: (CandidateBatch(current, (FactCandidate(current_fact),)),),
                    prior.document_id: (CandidateBatch(prior, (FactCandidate(prior_fact),)),),
                }, diagnostics=(f"{prior.document_id}:pdf_window_failed",))
        view = FinancialPipeline(Collector()).prepare(
            FinancialResearchSnapshot(_subject(), (current, prior)),
            CompilationPolicy(reporting_currency="CNY"),
        )
        self.assertEqual({item.accession_number for item in view.facts}, {current.accession_number, prior.accession_number})
        self.assertEqual(view.publishable_accessions, (current.accession_number,))

    def test_snapshot_freezes_company_and_candidate_observations(self):
        subject = _subject()
        filing = _filing()
        fact = _fact(filing)
        snapshot = FinancialResearchSnapshot.from_facts(subject, (filing,), (fact,))
        subject.reporting_currency = "USD"
        filing.period_end = "2024-12-31"
        fact.value = 999.0
        self.assertEqual(snapshot.subject.reporting_currency, "CNY")
        self.assertEqual(snapshot.filings[0].period_end, "2025-12-31")
        self.assertEqual(snapshot.frozen_candidate_batches[0].candidates[0].fact.value, 100.0)

    def test_pdf_cache_preserves_explicit_ast_evidence_relation(self):
        filing = _filing()
        fact = replace(_fact(filing), fact_id="ingest:raw-cell-42", parser_version="financial-ingestion-ast-v99")
        ref = EvidenceRef(
            "fact:raw-cell-42", filing.document_id, filing.source_url,
            "revenue", "page:4", "Revenue 100", filing.filed_at, "source-sha",
        )
        foreign = replace(ref, document_id="different-document")
        snapshot = FinancialResearchSnapshot.from_facts(_subject(), (filing,), (fact,), evidence=(ref, foreign))
        self.assertEqual(snapshot.frozen_candidate_batches[0].candidates[0].evidence, (ref,))

    def test_legacy_source_acceptance_flag_cannot_bypass_compiler(self):
        filing = _filing()
        unproven = replace(_fact(filing), unit_provenance="unknown", provenance_status="unknown", company_cik="different-issuer")
        class Legacy:
            calls = 0
            def ingest(self, _subject, _filings):
                self.calls += 1
                return SimpleNamespace(accepted_facts=(unproven,), evidence=(), allow_ai=True)
        legacy = Legacy()
        view = FinancialPipeline(legacy).prepare(
            FinancialResearchSnapshot(_subject(), (filing,)),
            CompilationPolicy(required_concepts=frozenset({"revenue"}), reporting_currency="CNY"),
        )
        self.assertEqual(legacy.calls, 1)
        self.assertNotIn(unproven.fact_id, {item.fact_id for item in view.facts})
        self.assertIn(unproven.fact_id, {item.fact_id for item in view.quarantined_facts})

    def test_legacy_source_type_error_is_not_an_implicit_second_invocation(self):
        class Legacy:
            calls = 0
            def ingest(self, _subject, _filings, **_options):
                self.calls += 1
                raise TypeError("producer already performed I/O")
        legacy = Legacy()
        with self.assertRaisesRegex(TypeError, "already performed"):
            FinancialPipeline(legacy).prepare(FinancialResearchSnapshot(_subject(), (_filing(),)), CompilationPolicy())
        self.assertEqual(legacy.calls, 1)

    def test_public_prepare_collects_compiles_and_admits_once_from_frozen_inputs(self):
        source_filing = _filing()
        fact = _fact(source_filing)
        dataset = FinancialDataset(
            filings=(source_filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )
        collector = RecordingCollector(FactCandidate(fact, extractor="structured"))
        compiler = RecordingCompiler(dataset)
        admission = RecordingAdmission()
        pipeline = FinancialPipeline(collector, compiler, admission=admission)
        snapshot = FinancialResearchSnapshot(
            subject=_subject(),
            filings=(source_filing,),
            candidate_sources=(object(),),
            period_range=("2025", "2025"),
            authorized_source_stages=frozenset({GapStageKind.OFFICIAL_STRUCTURED}),
            requested_annual_count=1,
            research_as_of="2026-03-01",
        )

        view = pipeline.prepare(
            snapshot,
            CompilationPolicy(
                filings=(source_filing,), required_concepts=frozenset({"revenue"}),
                reporting_currency="CNY",
            ),
        )

        self.assertIsInstance(view, FinancialResearchView)
        self.assertEqual(view.canonical_dataset.resolved_facts, dataset.resolved_facts)
        self.assertEqual(len(collector.calls), 1)
        self.assertEqual(len(compiler.calls), 1)
        self.assertEqual(len(admission.calls), 1)
        self.assertEqual(compiler.calls[0][2].period_range, ("2025", "2025"))
        self.assertEqual(snapshot.filings[0].document_id, source_filing.document_id)
        self.assertIsNot(snapshot.filings[0], source_filing)
        with self.assertRaises(AttributeError):
            snapshot.filings = ()

    def test_frozen_facts_path_preserves_unknown_metadata_and_evidence(self):
        filing = _filing()
        fact = replace(_fact(filing), unit_provenance="unknown")
        ref = EvidenceRef(
            f"fact:{fact.fact_id}", filing.document_id, fact.source_url,
            "revenue", "page:4", "Revenue 100", filing.filed_at,
            filing.content_hash,
        )
        snapshot = FinancialResearchSnapshot.from_facts(
            _subject(), (filing,), (fact,), evidence=(ref,),
        )
        self.assertEqual(snapshot.frozen_candidate_batches[0].candidates[0].fact, fact)
        self.assertEqual(snapshot.frozen_candidate_batches[0].candidates[0].evidence, (ref,))

        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(ref,), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )
        compiler = RecordingCompiler(dataset)
        view = FinancialPipeline(None, compiler).prepare(
            snapshot,
            CompilationPolicy(filings=(filing,), required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(len(compiler.calls), 1)
        prepared_fact = compiler.calls[0][1][0].candidates[0].fact
        self.assertEqual(prepared_fact.unit_provenance, "unknown")
        self.assertEqual(compiler.calls[0][1][0].candidates[0].evidence, (ref,))
        self.assertEqual(view.canonical_dataset.resolved_facts, (fact,))

    def test_canonical_cache_reenters_as_frozen_candidates(self):
        filing = _filing()
        fact = _fact(filing)
        quarantined = replace(fact, fact_id="fact-quarantined", value=120.0)
        ref = EvidenceRef(
            f"fact:{fact.fact_id}", filing.document_id, fact.source_url,
            "revenue", "page:4", "Revenue 100", filing.filed_at,
            filing.content_hash,
        )
        canonical = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(quarantined,),
            evidence=(ref,), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )

        snapshot = FinancialResearchSnapshot.from_canonical(
            _subject(), (filing,), canonical,
        )

        candidates = snapshot.frozen_candidate_batches[0].candidates
        self.assertEqual(
            {item.fact.fact_id for item in candidates},
            {fact.fact_id, quarantined.fact_id},
        )
        self.assertEqual(
            next(item for item in candidates if item.fact.fact_id == fact.fact_id).evidence,
            (ref,),
        )

    def test_from_canonical_preserves_manifests_and_adapts_legacy_accepted_facts(self):
        filing = _filing()
        fact = _fact(filing)
        manifest = SimpleNamespace(
            accession_number="manifest-only-accession",
            period_end="2024-12-31",
            fiscal_period="FY",
            form_type="ANNUAL_REPORT",
        )
        legacy_canonical = SimpleNamespace(
            accepted_facts=(fact,),
            quarantined_facts=(),
            evidence=(),
            manifest=(manifest,),
        )
        snapshot = FinancialResearchSnapshot.from_canonical(
            _subject(), (filing,), legacy_canonical,
        )
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )
        compiler = RecordingCompiler(dataset)
        view = FinancialPipeline(None, compiler).prepare(
            snapshot, CompilationPolicy(required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(len(compiler.calls), 1)
        self.assertEqual(
            compiler.calls[0][1][0].candidates[0].fact.fact_id, fact.fact_id,
        )
        self.assertIs(view.canonical_dataset.manifests[0], manifest)
        self.assertIn(
            "manifest-only-accession",
            {item["accession_number"] for item in view.profile.period_continuity},
        )

    def test_compile_facts_keeps_undeclared_accessions_for_quarantine_audit(self):
        filing = _filing()
        fact = replace(
            _fact(filing), accession_number="undeclared-accession",
            unit_provenance="unknown",
        )
        result = FinancialFactCompiler().compile_facts(
            _subject(), (filing,), (fact,), required_concepts=frozenset({"revenue"}),
        )

        self.assertIn(fact.fact_id, {item.fact_id for item in result.quarantined_facts})
        quarantined = next(
            item for item in result.quarantined_facts if item.fact_id == fact.fact_id
        )
        self.assertEqual(quarantined.unit_provenance, fact.unit_provenance)
        self.assertTrue(any(
            "undeclared_accession_candidate:" in diagnostic
            for diagnostic in result.diagnostics
        ))
        self.assertTrue(any(
            "issuer_identity_mismatch" in group.issues
            for group in result.validations
        ))
        self.assertEqual(result.evidence, ())

    def test_prepare_rejects_vision_without_frozen_authorization(self):
        pipeline = FinancialPipeline(object(), object())
        snapshot = FinancialResearchSnapshot(
            subject=_subject(), filings=(_filing(),),
            vision_config=type("Config", (), {"enabled": True})(),
        )
        with self.assertRaisesRegex(ValueError, "MINERU_SOURCE_NOT_AUTHORIZED"):
            pipeline.prepare(snapshot, CompilationPolicy())

    def test_denied_vision_keeps_native_candidates_and_admits_once(self):
        filing = _filing()
        fact = _fact(filing)
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=False, research_facts=(fact,),
        )

        class DeniedConfig:
            enabled = True
            consent = False

            def validate(self):
                raise VisionAdapterError("VISION_CONSENT_REQUIRED")

        collector = RecordingCollector(FactCandidate(fact, extractor="structured"))
        compiler = RecordingCompiler(dataset)
        admission = RecordingAdmission()
        view = FinancialPipeline(collector, compiler, admission=admission).prepare(
            FinancialResearchSnapshot(
                subject=_subject(), filings=(filing,),
                authorized_source_stages=frozenset({GapStageKind.MINERU}),
                vision_fallback=object(), vision_config=DeniedConfig(),
            ),
            CompilationPolicy(required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(view.canonical_dataset.resolved_facts, (fact,))
        self.assertIn("VISION_CONSENT_REQUIRED", view.diagnostics)
        self.assertEqual(len(compiler.calls), 1)
        self.assertEqual(len(admission.calls), 1)

    def test_unavailable_vision_keeps_native_candidates_and_recoverable_diagnostic(self):
        filing = _filing()
        fact = _fact(filing)
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=False, research_facts=(fact,),
        )

        class Config:
            enabled = True
            consent = True
            timeout_seconds = 1.0

            def validate(self):
                return None

        class UnavailableAdapter:
            def extract(self, *args, **kwargs):
                raise VisionAdapterError("VISION_TEMPORARILY_UNAVAILABLE")

        collector = RecordingCollector(FactCandidate(fact, extractor="structured"))
        compiler = RecordingCompiler(dataset)
        admission = RecordingAdmission()
        with patch(
            "openthesis.financial_ingestion._vision_failed_pages",
            return_value=(VisionPageRequest(1, b"opaque-page"),),
        ):
            view = FinancialPipeline(collector, compiler, admission=admission).prepare(
                FinancialResearchSnapshot(
                    subject=_subject(), filings=(filing,),
                    authorized_source_stages=frozenset({GapStageKind.MINERU}),
                    vision_fallback=UnavailableAdapter(), vision_config=Config(),
                ),
                CompilationPolicy(required_concepts=frozenset({"revenue"})),
            )

        self.assertEqual(view.canonical_dataset.resolved_facts, (fact,))
        self.assertTrue(any(
            "VISION_TEMPORARILY_UNAVAILABLE" in item for item in view.diagnostics
        ))
        self.assertEqual(len(compiler.calls), 1)
        self.assertEqual(len(admission.calls), 1)

    def test_vision_cancellation_propagates_without_admission(self):
        filing = _filing()
        fact = _fact(filing)
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=False, research_facts=(fact,),
        )

        class Config:
            enabled = True
            consent = True
            timeout_seconds = 1.0

            def validate(self):
                return None

        class CancelledAdapter:
            def extract(self, *args, **kwargs):
                raise VisionAdapterError("VISION_CANCELLED")

        collector = RecordingCollector(FactCandidate(fact, extractor="structured"))
        compiler = RecordingCompiler(dataset)
        admission = RecordingAdmission()
        with patch(
            "openthesis.financial_ingestion._vision_failed_pages",
            return_value=(VisionPageRequest(1, b"opaque-page"),),
        ):
            with self.assertRaises(CancelledError):
                FinancialPipeline(collector, compiler, admission=admission).prepare(
                    FinancialResearchSnapshot(
                        subject=_subject(), filings=(filing,),
                        authorized_source_stages=frozenset({GapStageKind.MINERU}),
                        vision_fallback=CancelledAdapter(), vision_config=Config(),
                    ),
                    CompilationPolicy(required_concepts=frozenset({"revenue"})),
                )

        self.assertEqual(len(compiler.calls), 1)
        self.assertEqual(admission.calls, [])

    def test_engine_for_is_selected_inside_prepare(self):
        filing = _filing()
        fact = _fact(filing)
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )
        collector = RecordingCollector(FactCandidate(fact, extractor="structured"))
        compiler = RecordingCompiler(dataset)
        selected = []

        def engine_for(subject, filings):
            selected.append((subject, tuple(filings)))
            return collector

        pipeline = FinancialPipeline(None, compiler, engine_for=engine_for)
        pipeline.prepare(
            FinancialResearchSnapshot(subject=_subject(), filings=(filing,)),
            CompilationPolicy(required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0], _subject())
        self.assertEqual(selected[0][1][0].document_id, filing.document_id)
        self.assertEqual(len(collector.calls), 1)

    def test_empty_frozen_batches_skip_all_collection_and_still_compile(self):
        filing = _filing()
        engine_for_calls = []

        def engine_for(subject, filings):
            engine_for_calls.append((subject, tuple(filings)))
            raise AssertionError("explicitly empty candidate batches must skip collection")

        view = FinancialPipeline(None, engine_for=engine_for).prepare(
            FinancialResearchSnapshot(
                subject=_subject(), filings=(filing,), frozen_candidate_batches=(),
            ),
            CompilationPolicy(required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(engine_for_calls, [])
        self.assertFalse(view.canonical_dataset.resolved_facts)

    def test_frozen_quarter_facts_use_filing_period_when_policy_is_default(self):
        filing = replace(
            _filing(), fiscal_period="Q1", period_end="2025-03-31",
        )
        fact = replace(
            _fact(filing), fiscal_period="Q1", end_date="2025-03-31",
            period_start="2025-01-01", start_date="2025-01-01",
        )
        dataset = FinancialDataset(
            filings=(filing,), resolved_facts=(fact,), quarantined_facts=(),
            evidence=(), conflicts=(), validations=(), coverage={}, diagnostics=(),
            allow_ai=True, research_facts=(fact,),
        )
        compiler = RecordingCompiler(dataset)
        FinancialPipeline(None, compiler).prepare(
            FinancialResearchSnapshot.from_facts(_subject(), (filing,), (fact,)),
            CompilationPolicy(required_concepts=frozenset({"revenue"})),
        )

        self.assertEqual(compiler.calls[0][2].fiscal_period, "Q1")

    def test_compiler_import_graph_has_no_source_or_service_dependencies(self):
        source = Path(__file__).parents[1] / "src" / "openthesis" / "financial_compiler.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(any(
            item and any(boundary in item for boundary in (
                "financial_ingestion", "financial_sources", "financial_pipeline",
                "service", "application_services",
            ))
            for item in imported
        ))

    def test_admission_and_pdf_collection_do_not_depend_back_on_parser_or_pipeline(self):
        root = Path(__file__).parents[1] / "src" / "openthesis"
        for relative, forbidden in (
            ("financial_admission.py", ("financial_ingestion",)),
            ("financial_profile.py", ("financial_ingestion", "financial_pipeline")),
            ("financial_sources/pdf.py", (
                "financial_compiler", "financial_pipeline", "FinancialPipeline",
            )),
        ):
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            } | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            self.assertFalse(any(
                item and any(boundary in item for boundary in forbidden)
                for item in imported
            ), relative)
            if relative.endswith("/pdf.py"):
                calls = {
                    getattr(node.func, "id", "")
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                }
                self.assertFalse(calls & {
                    "FinancialPipeline", "FinancialCandidatePipeline",
                    "FinancialFactCompiler",
                })


if __name__ == "__main__":
    unittest.main()
