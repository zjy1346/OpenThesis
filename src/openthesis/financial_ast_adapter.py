"""Adapter from the official ingestion AST to the canonical compiler.

This module deliberately contains no PDF parsing or unit/period rules.  The
single source of truth is :class:`FinancialIngestionEngine`; this boundary only
associates the engine's explicit evidence with untrusted candidates.  The
compiler owns resolution, conflicts and acceptance.
"""

from __future__ import annotations

from .domain import Company, FilingDocument
from .financial_compiler import (
    CORE_CONCEPTS,
    CandidateBatch,
    CompilerPolicy,
    FactCandidate,
    FinancialDataset,
    FinancialFactCompiler,
    GapStageKind,
)
from .financial_ingestion import FinancialIngestionEngine, _manifest_for


class PdfAstFactExtractor:
    """Wrap the formal coordinate AST without a second PDF traversal."""

    name = "financial-ingestion-ast"
    stage_kind = GapStageKind.PDF_AST

    def __init__(self, engine: FinancialIngestionEngine | None = None) -> None:
        self.engine = engine or FinancialIngestionEngine()

    def extract(self, subject: Company, filing: FilingDocument) -> CandidateBatch:
        manifest = _manifest_for(filing)
        if manifest is None or not filing.local_path:
            return CandidateBatch(filing, diagnostics=("ambiguous_or_missing_pdf",))
        facts, refs = self.engine.extract_pdf_candidates(
            filing.local_path, subject, filing, manifest
        )
        candidates = tuple(
            FactCandidate(
                fact,
                ((ref,) if ref is not None else ()),
                self.name,
            )
            for fact, ref in zip(facts, refs)
        )
        return CandidateBatch(filing, candidates, tuple(refs))


def compile_local_pdf(
    subject: Company,
    filing: FilingDocument,
    *,
    required_concepts: frozenset[str] = CORE_CONCEPTS,
) -> FinancialDataset:
    """Compile one local filing through the formal AST and one quality gate."""
    return FinancialFactCompiler().compile(
        subject,
        (filing.period_end, filing.period_end),
        CompilerPolicy(
            filings=(filing,),
            extractors=(PdfAstFactExtractor(),),
            required_concepts=required_concepts,
            reporting_currency=subject.reporting_currency,
        ),
    )
