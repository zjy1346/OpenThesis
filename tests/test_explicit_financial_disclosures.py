from dataclasses import replace
from decimal import Decimal

import pytest

from openthesis.domain import FilingDocument
from openthesis.markets import build_company
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_admission import FinancialAdmission
from openthesis.financial_sources.amount_disclosures import (
    explicit_money_disclosures, extract_disclosure_candidates,
)
from openthesis.financial_validation import FinancialValidator


TEXT = "冰轮环境技术股份有限公司2025年年度报告全文\n如财务报表附注五、四十四所述，贵公司合并财务报表中 2025 年度的营业收入为人民币 705,183.46 万元。"


def candidate(text=TEXT):
    company = build_company("000811.SZ", "冰轮环境", market="CN_A", issuer_id="CNINFO:gssz0000811")
    filing = FilingDocument(document_id="doc", company_cik=company.security_id,
        accession_number="annual", form_type="2025年度报告", fiscal_period="FY",
        period_end="2025-12-31", filed_at="2026-03-20", primary_document="annual.pdf",
        source_url="https://static.cninfo.com.cn/annual.pdf", content_hash="7" * 64)
    facts, refs = extract_disclosure_candidates(company, filing, 65, text, (10.0, 20.0, 500.0, 760.0))
    return company, filing, facts, refs


def validate(fact, refs):
    row = next(ref for ref in refs if ref.evidence_id == "fact:" + fact.fact_id)
    return FinancialValidator().validate_group([fact],
        (fact.company_cik, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, expected_company_id=fact.company_cik, source_evidence=refs)


def test_rounded_audit_amount_is_an_independent_proven_candidate():
    company, filing, facts, refs = candidate()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.normalization_lineage.raw_value == "705,183.46"
    assert Decimal(fact.normalization_lineage.normalized_value) == Decimal("7051834600")
    assert explicit_money_disclosures(TEXT, "2025-12-31")[0].display_quantum == Decimal("100")
    assert fact.value != 7051834638.32  # Cannot manufacture the statement's extra digits.
    result = FinancialFactCompiler().compile_facts(company, [filing], facts, evidence=refs)
    assert [item.fact_id for item in result.research_facts] == [fact.fact_id]
    assert FinancialAdmission().admit(company, result, selected_filings=[filing]).model_ready
    assert "operating_cash_flow" not in {item.concept for item in result.research_facts}


@pytest.mark.parametrize("change", [
    {"value": 7051834638.32}, {"currency": "USD"}, {"consolidated_scope": "parent"},
    {"concept": "operating_cash_flow"}, {"fiscal_year": 2024}, {"end_date": "2024-12-31"},
    {"entity": "其他公司"}, {"source_page": 66}, {"start_date": "2025-04-01"},
])
def test_candidate_metadata_cannot_rewrite_original_disclosure(change):
    _, _, facts, refs = candidate()
    outcome = validate(replace(facts[0], **change), refs)
    assert not outcome.validation.accepted


@pytest.mark.parametrize("text", [
    TEXT.replace("人民币 ", ""), TEXT.replace("合并财务报表", "财务报表"),
    TEXT.replace("2025 年度", "2024 年度"), TEXT.replace("营业收入为", "营业收入预计为"),
    TEXT.replace("营业收入", "电池业务营业收入"), TEXT.replace("营业收入", "非营业收入"),
    TEXT.replace("705,183.46 万元", "705,183.46 万元，2024年为600,000万元"),
    TEXT.replace("合并财务报表", "合并财务报表与母公司财务报表"),
])
def test_ambiguous_or_unproven_disclosure_cannot_be_promoted(text):
    assert not candidate(text)[2]


def test_no_calendar_guess_for_march_ending_fiscal_year():
    assert not explicit_money_disclosures(TEXT, "2025-03-31")


def test_mutated_original_context_is_rejected():
    _, _, facts, refs = candidate()
    changed = [replace(ref, excerpt=ref.excerpt.replace("705,183.46", "999,999.00"))
               if ref.evidence_id.startswith("disclosure-context:") else ref for ref in refs]
    assert not validate(facts[0], changed).validation.accepted


def test_same_grammar_accepts_an_unrelated_issuer_and_preserves_parent_scope():
    company = build_company("600000.SH", "测试公司", market="CN_A")
    _, filing, _, _ = candidate()
    filing = replace(filing, company_cik=company.security_id)
    text = "测试公司2025年报。母公司财务报表中2025年度的净利润为人民币12.34万元。"
    facts, refs = extract_disclosure_candidates(company, filing, 12, text, (10., 20., 500., 760.))
    assert facts[0].consolidated_scope == "parent"
    assert validate(facts[0], refs).validation.accepted
