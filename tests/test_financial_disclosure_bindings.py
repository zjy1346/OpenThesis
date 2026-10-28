from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import pytest

from openthesis.domain import (
    Company,
    EvidenceRef,
    FilingDocument,
    FinancialFact,
    FinancialNormalizationLineage,
)
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy, FactCandidate
from openthesis.financial_disclosure_bindings import (
    bind_compiled_disclosures,
    cited_compiled_rows,
)
from openthesis.financial_evidence import build_fact_evidence
from openthesis.financial_pipeline import FinancialPipeline, FinancialResearchSnapshot
from openthesis.research import verify_agent_output
from openthesis.research_claim_repair import ClaimRepairContract


ROW_TEXT = "Operating revenue 100 CNY"
CONTENT_HASH = "a" * 64


def _subject() -> Company:
    return Company(
        "CN_A:SZSE:002594.SZ", "002594.SZ", "BYD", "SZSE",
        market="CN_A", reporting_currency="CNY",
    )


def _compiled_view_fixture():
    subject = _subject()
    filing = FilingDocument(
        "filing-2025", subject.security_id, "accession-2025", "ANNUAL_REPORT",
        "FY", "2025-12-31", "2026-03-01", "2025 Annual Report",
        "https://example.test/annual-2025.pdf", content_hash=CONTENT_HASH,
    )
    row = EvidenceRef(
        "fact:revenue-2025", filing.document_id, filing.source_url,
        "Income statement revenue row", "page:12", ROW_TEXT,
        filing.filed_at, CONTENT_HASH,
    )
    unit = EvidenceRef(
        "unit:revenue-2025", filing.document_id, filing.source_url,
        "Displayed monetary unit", "page:12", "Currency: CNY; unit scale: 1",
        filing.filed_at, CONTENT_HASH,
    )
    fact = FinancialFact(
        "revenue-2025", subject.security_id, "revenue", "Operating revenue", 100.0,
        "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", filing.period_end,
        filing.filed_at, filing.accession_number, filing.source_url,
        scope="consolidated", entity=subject.name, market="CN_A",
        statement="income_statement", period_start="2025-01-01",
        consolidated_scope="consolidated", currency="CNY", unit_scale=1.0,
        unit_provenance="structured_normalized", source_document=filing.primary_document,
        source_page=12, raw_text=ROW_TEXT, parser_version="official-structured-fixture-v1",
        normalization_lineage=FinancialNormalizationLineage(
            raw_value="100", scale="1", normalized_value="100", quantity_kind="money",
            currency="CNY", unit_evidence_ids=(unit.evidence_id,),
        ),
    )
    candidate = FactCandidate(fact, (row,), "official-structured-fixture")
    batch = CandidateBatch(filing, (candidate,), (unit,))
    snapshot = FinancialResearchSnapshot(
        subject, (filing,), frozen_candidate_batches=(batch,),
    )
    policy = CompilationPolicy(
        filings=(filing,), required_concepts=frozenset({"revenue"}),
        reporting_currency="CNY",
    )
    view = FinancialPipeline(None, FinancialFactCompiler()).prepare(snapshot, policy)
    return subject, filing, fact, candidate, batch, view


def _filing_text_record(subject: Company, filing: FilingDocument, *, excerpt: str | None = None):
    text = excerpt or f"Income statement row: {ROW_TEXT}; FY2025."
    return {
        "evidence_id": "filing:revenue-disclosure",
        "kind": "filing_text",
        "company_cik": subject.security_id,
        "entity": subject.name,
        "market": subject.market,
        "document_id": filing.document_id,
        "source_url": filing.source_url,
        "content_hash": filing.content_hash,
        "locator": "page:12",
        "excerpt": text,
        "raw_text": text,
        "accession_number": filing.accession_number,
        "source_document": filing.primary_document,
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "end_date": filing.period_end,
    }


def _financial_evidence(view):
    return build_fact_evidence([fact.to_dict() for fact in view.facts])


def test_frozen_candidate_is_compiled_and_admitted_through_public_pipeline_prepare() -> None:
    _subject_value, _filing_value, original_fact, _candidate, _batch, view = _compiled_view_fixture()

    assert view.canonical_dataset is not None
    assert any(item.fact_id == original_fact.fact_id for item in view.facts)
    assert any(item.evidence_id == f"fact:{original_fact.fact_id}" for item in view.evidence)
    assert original_fact.validation_status == "unvalidated"
    assert asdict(original_fact)["normalization_lineage"]["normalized_value"] == "100"


def test_only_complete_same_source_row_can_bind_and_inputs_are_not_mutated() -> None:
    subject, filing, original_fact, candidate, _batch, view = _compiled_view_fixture()
    record = _filing_text_record(subject, filing)
    record["compiled_disclosure_binding"] = {
        "version": "stale-untrusted-binding",
        "content_hash": "0" * 64,
        "locator": "page:99",
        "evidence_ids": ["fact:caller-injected"],
    }
    original_records = deepcopy([record])
    candidate_before = asdict(candidate.fact)
    view_fact = next(item for item in view.facts if item.fact_id == original_fact.fact_id)
    view_fact_before = asdict(view_fact)

    bound = bind_compiled_disclosures(view, [record])

    assert bound[0]["compiled_disclosure_binding"] == {
        "version": "compiled-disclosure-binding-v1",
        "content_hash": CONTENT_HASH,
        "locator": "page:12",
        "evidence_ids": [f"fact:{original_fact.fact_id}"],
    }
    assert record == original_records[0]
    assert asdict(candidate.fact) == candidate_before
    assert asdict(original_fact) == candidate_before
    assert asdict(view_fact) == view_fact_before


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("content_hash", "b" * 64),
        ("locator", "page:13"),
        ("document_id", "another-document"),
        ("source_url", "https://example.test/another-report.pdf"),
        ("company_cik", "CN_A:SZSE:999999.SZ"),
    ),
)
def test_mismatched_disclosure_context_cannot_retain_or_gain_binding(
    field: str, wrong_value: str,
) -> None:
    subject, filing, _fact, _candidate, _batch, view = _compiled_view_fixture()
    record = _filing_text_record(subject, filing)
    record[field] = wrong_value
    record["compiled_disclosure_binding"] = {
        "version": "caller-supplied-old-binding",
        "content_hash": CONTENT_HASH,
        "locator": "page:12",
        "evidence_ids": ["fact:revenue-2025"],
    }

    result = bind_compiled_disclosures(view, [record])

    assert "compiled_disclosure_binding" not in result[0]
    assert record["compiled_disclosure_binding"]["version"] == "caller-supplied-old-binding"


@pytest.mark.parametrize(
    "excerpt",
    (
        "Income statement row: Operating revenue CNY; FY2025.",
        "The annual report says revenue reached CNY 100 in FY2025.",
    ),
)
def test_partial_or_nonnormative_quantitative_text_cannot_bind_to_compiled_row(
    excerpt: str,
) -> None:
    subject, filing, _fact, _candidate, _batch, view = _compiled_view_fixture()
    record = _filing_text_record(subject, filing, excerpt=excerpt)

    result = bind_compiled_disclosures(view, [record])

    assert "compiled_disclosure_binding" not in result[0]
    assert result[0]["excerpt"] == excerpt


def test_bound_alias_citation_verifies_against_the_compiled_financial_row() -> None:
    subject, filing, _fact, _candidate, _batch, view = _compiled_view_fixture()
    disclosure = bind_compiled_disclosures(
        view, [_filing_text_record(subject, filing)],
    )[0]
    financial_records = _financial_evidence(view)
    records = {
        disclosure["evidence_id"]: disclosure,
        **{item["evidence_id"]: item for item in financial_records},
    }

    assert cited_compiled_rows(disclosure, records) == financial_records
    verification = verify_agent_output(
        {"claims": [{
            "kind": "fact",
            "text": "Operating revenue was CNY 100 in FY2025.",
            "concept": "revenue",
            "value": 100,
            "unit": "CNY",
            "company_cik": subject.security_id,
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "end_date": filing.period_end,
            "evidence_ids": [disclosure["evidence_id"]],
        }]},
        set(records), "en", records,
    )

    assert verification["claim_verifications"][0]["state"] == "numeric_verified"
    assert verification["passed"]


def test_compiled_alias_rejects_conflicting_amount_and_unsupported_extra_number() -> None:
    subject, filing, _fact, _candidate, _batch, view = _compiled_view_fixture()
    disclosure = bind_compiled_disclosures(
        view, [_filing_text_record(subject, filing)],
    )[0]
    financial_records = _financial_evidence(view)
    records = {
        disclosure["evidence_id"]: disclosure,
        **{item["evidence_id"]: item for item in financial_records},
    }
    base_claim = {
        "kind": "fact",
        "text": "Operating revenue was CNY 100 in FY2025.",
        "concept": "revenue",
        "value": 100,
        "unit": "CNY",
        "company_cik": subject.security_id,
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "end_date": filing.period_end,
        "evidence_ids": [disclosure["evidence_id"]],
    }

    amount_conflict = verify_agent_output(
        {"claims": [{**base_claim, "value": 101}]}, set(records), "en", records,
    )
    extra_number = verify_agent_output(
        {"claims": [{
            **base_claim,
            "text": "Operating revenue was CNY 100 in FY2025 and CNY 900 in FY2026.",
        }]},
        set(records), "en", records,
    )

    assert amount_conflict["claim_verifications"][0]["state"] == "contradicted"
    assert amount_conflict["claim_verifications"][0]["issue_code"] == "CLAIM_VALUE_CONFLICT"
    assert extra_number["claim_verifications"][0]["state"] == "contradicted"
    assert extra_number["claim_verifications"][0]["issue_code"] == "CLAIM_PROSE_NUMERIC_CONFLICT"


def test_uncompiled_quantitative_disclosure_is_unrepairable_without_canonical_row() -> None:
    subject, filing, _fact, _candidate, _batch, view = _compiled_view_fixture()
    disclosure = bind_compiled_disclosures(
        view,
        [_filing_text_record(
            subject, filing,
            excerpt="The annual report states operating revenue reached 100 CNY in FY2025.",
        )],
    )[0]
    records = {disclosure["evidence_id"]: disclosure}
    claim = {
        "kind": "fact",
        "text": "Operating revenue reached 100 CNY in FY2025.",
        "company_cik": subject.security_id,
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "evidence_ids": [disclosure["evidence_id"]],
    }
    candidate = {"structured_output_valid": True, "claims": [claim]}
    verification = verify_agent_output(candidate, set(records), "en", records)
    repair = ClaimRepairContract(
        lambda output: verify_agent_output(output, set(records), "en", records),
        "financial-disclosure-binding-test-v1",
    )

    assert "compiled_disclosure_binding" not in disclosure
    assert verification["claim_verifications"][0]["issue_code"] == "CLAIM_QUANTITATIVE_DISCLOSURE_UNCOMPILED", verification
    assert verification["claim_verifications"][0]["repairability"] == "unrepairable"
    assert repair.plan(candidate) is None
