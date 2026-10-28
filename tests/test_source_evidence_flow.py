from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from openthesis.domain import Company, EvidenceRef, FilingDocument, FinancialFact
from openthesis.financial_contracts import CandidateBatch, FactCandidate, GapStageKind, PDF_PARSER_VERSION, SourceAmount
from openthesis.financial_sources import GapResolver, MappedCandidateExtractor
from openthesis.financial_sources.pdf import FinancialPdfSource


def source_fixture():
    company = Company("CN_A:SZSE:002594.SZ", "002594.SZ", "BYD", "SZSE", market="CN_A",
                      reporting_currency="CNY")
    filing = FilingDocument("doc-2025", company.security_id, "accession-2025", "ANNUAL_REPORT", "FY",
                            "2025-12-31", "2026-03-01", "2025 Annual Report",
                            "https://example.invalid/report.pdf", content_hash="source-hash")
    fact = FinancialFact(
        "flow-revenue", company.security_id, "revenue", "营业收入", 100.0, "CNY", 2025, "FY",
        "ANNUAL_REPORT", "2025-01-01", "2025-12-31", filing.filed_at,
        filing.accession_number, filing.source_url, statement="income_statement",
        currency="CNY", consolidated_scope="consolidated", scope="consolidated",
        source_document=filing.primary_document, source_page=1, source_bbox=(0, 0, 100, 20),
        raw_text="营业收入 100 元", unit_provenance="explicit_source_unit", unit_scale=1,
        parser_version=PDF_PARSER_VERSION,
        normalization_lineage=SourceAmount("100", "1", "money", "CNY", ("unit:flow",)).normalize(),
    )
    row = EvidenceRef("fact:flow-revenue", filing.document_id, filing.source_url,
                      filing.primary_document, "page:1", fact.raw_text, filing.filed_at,
                      filing.content_hash, fact.source_bbox)
    unit = replace(row, evidence_id="unit:flow", excerpt="单位：元 币种：人民币")
    return company, filing, fact, row, unit


def test_pdf_source_preserves_structured_unit_evidence_separately_from_row_mapping():
    company, filing, fact, row, unit = source_fixture()

    class StructuredSource:
        def fetch(self, subject, document):
            return [fact], [unit, row], None

    engine = SimpleNamespace(_max_workers=1, _parse_timeout_seconds=30, _batch_timeout_seconds=60)
    with patch("openthesis.financial_ingestion._parse_local_pdfs_bounded", return_value={}):
        collection = FinancialPdfSource(engine).collect_candidate_batches(
            company, [filing], structured_sources=[StructuredSource()],
        )
    batch = collection.batches_by_document[filing.document_id][0]
    assert batch.candidates[0].evidence == (row,)
    assert {item.evidence_id for item in batch.evidence} == {row.evidence_id, unit.evidence_id}
    assert {item.evidence_id for item in collection.evidence} == {row.evidence_id, unit.evidence_id}


def test_gap_resolution_keeps_unit_evidence_for_accepted_candidate_without_positional_binding():
    company, filing, fact, row, unit = source_fixture()
    batch = CandidateBatch(filing, (FactCandidate(fact, (row,), "pdf"),), (unit, row))
    extractor = MappedCandidateExtractor({filing.document_id: batch}, GapStageKind.PDF_AST, "pdf")
    result = GapResolver().resolve(company, filing, ["revenue"], [extractor])
    assert result.candidates[0].evidence == (row,)
    assert {item.evidence_id for item in result.evidence} == {row.evidence_id, unit.evidence_id}


def test_production_gap_pipeline_reaches_lineage_validator_with_unit_proof():
    from openthesis.financial_contracts import FinancialCandidateCollection
    from openthesis.financial_pipeline import FinancialCandidatePipeline

    company, filing, fact, row, unit = source_fixture()

    class Collector:
        def collect_candidate_batches(self, subject, filings, **kwargs):
            return FinancialCandidateCollection((), {filing.document_id: (
                CandidateBatch(filing),
                CandidateBatch(filing, (FactCandidate(fact, (row,), "pdf"),), (unit, row)),
            )})

    dataset = FinancialCandidatePipeline().compile_from_ingestion(company, [filing], Collector())
    assert [item.fact_id for item in dataset.resolved_facts] == [fact.fact_id]
    assert unit.evidence_id in {item.evidence_id for item in dataset.evidence}
    assert not any("normalization" in str(item) for item in dataset.diagnostics)
