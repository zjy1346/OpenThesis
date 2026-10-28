"""Financial totals must not borrow amounts from a different semantic table."""
from dataclasses import replace
import pytest
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_validation import FinancialValidator
from tests.test_pdf_statement_semantics import Page, parse


def test_owner_equity_heading_does_not_borrow_first_share_capital_amount():
    facts, _ = parse([Page([
        ("Consolidated Balance Sheet",), ("RMB in thousands",), ("Item", "2025", "2024"),
        ("Equity attributable to owners of the Company",),
        ("Share capital", "426", "400"), ("Reserves", "265792235", "188337377"),
        ("", "265792661", "188337777"),
        ("Non-controlling interests", "104608", "467342"),
        ("Total equity", "265897269", "188805119"),
    ], line_gap=12)])
    owners = [fact for fact in facts if fact.concept == "equity" and fact.fiscal_year == 2025]
    assert [fact.normalization_lineage.normalized_value for fact in owners] == ["265792661000"]


def test_comprehensive_income_allocations_are_not_net_profit_allocations():
    facts, _ = parse([
        Page([("Consolidated Income Statement",), ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Profit for the year", "100", "90"), ("Attributable to:",),
              ("Owners of the Company", "110", "95"), ("Non-controlling interests", "(10)", "(5)")]),
        Page([("Consolidated Statement of",), ("Comprehensive Income",),
              ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Total comprehensive income for the year", "80", "70"), ("Attributable to:",),
              ("Owners of the Company", "85", "75"), ("Non-controlling interests", "(5)", "(5)")]),
    ])
    current = {fact.concept: fact.normalization_lineage.normalized_value for fact in facts if fact.fiscal_year == 2025}
    assert current["parent_net_income"] == "110000"
    assert current["minority_interest_profit"] == "-10000"
    assert current["comprehensive_income"] == "80000"
    assert current["parent_comprehensive_income"] == "85000"
    assert current["minority_interest_comprehensive_income"] == "-5000"


def test_equity_changes_matrix_is_preserved_as_disclosure_not_balance_total():
    facts, refs = parse([
        Page([("Consolidated Balance Sheet",), ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Non-controlling interests", "104", "467"), ("Total equity", "120", "110")]),
        Page([("Consolidated Statement of Changes in Equity",), ("RMB in thousands",),
              ("Item", "2025", "2024"), ("Non-controlling interests", "(2)", "(3)")]),
    ])
    current = [fact for fact in facts if fact.concept == "minority_interest_equity" and fact.fiscal_year == 2025]
    assert [fact.normalization_lineage.normalized_value for fact in current] == ["104000"]
    assert any("Changes in Equity" in ref.excerpt and "(2)" in ref.excerpt for ref in refs)


def test_generic_capex_heading_does_not_borrow_a_segment_amount_and_is_preserved():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("RMB in millions",), ("Item", "2025", "2024"),
        ("Capital expenditures",), ("Business Alpha", "6.1", "6.4"),
        ("Business Beta", "12.0", "4.1"), ("Total", "18.1", "10.5"),
    ], line_gap=12)])
    assert not any(fact.concept == "capital_expenditure" for fact in facts)
    assert any("Business Alpha" in ref.excerpt and "18.1" in ref.excerpt for ref in refs)


def test_comprehensive_allocation_cannot_be_relabelled_as_profit_by_candidate_metadata():
    facts, refs = parse([Page([
        ("Consolidated Statement of Comprehensive Income",), ("RMB in thousands",),
        ("Item", "2025", "2024"), ("Total comprehensive income for the year", "80", "70"),
        ("Attributable to:",), ("Owners of the Company", "85", "75"),
    ])])
    original = next(fact for fact in facts if fact.concept == "parent_comprehensive_income" and fact.fiscal_year == 2025)
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(original))
    identity = (original.accession_number, original.end_date, original.fiscal_period,
                original.consolidated_scope, original.currency)
    def validate(fact):
        return FinancialValidator().validate_group(
            [fact], identity, {fact.fact_id: row}, required_concepts={fact.concept},
            source_evidence=refs,
        ).validation
    assert validate(original).accepted == (original,)
    forged = replace(original, concept="parent_net_income", statement="income_statement")
    assert validate(forged).accepted == ()
    assert "statement_source_concept_mismatch" in validate(forged).issues
    assert "statement_source_concept_mismatch" in validate(replace(forged, classification_evidence_ids=())).issues


@pytest.mark.parametrize("change", [
    {"content_hash": "b" * 64}, {"source_url": "https://other.invalid/report.pdf"},
    {"document_id": "another-filing"}, {"locator": "page:2"},
    {"bbox": (50.0, 500.0, 290.0, 508.0)},
    {"excerpt": "Consolidated Income Statement"},
])
def test_comprehensive_allocation_rejects_unbound_or_corrupted_title(change):
    facts, refs = parse([Page([
        ("Consolidated Statement of Comprehensive Income",), ("RMB in thousands",),
        ("Item", "2025", "2024"), ("Total comprehensive income for the year", "80", "70"),
        ("Attributable to:",), ("Owners of the Company", "85", "75"),
    ])])
    fact = next(item for item in facts if item.concept == "parent_comprehensive_income" and item.fiscal_year == 2025)
    row = next(item for item in refs if item.evidence_id in fact_row_evidence_ids(fact))
    title = next(item for item in refs if item.evidence_id in fact.classification_evidence_ids)
    changed_refs = [replace(item, **change) if item == title else item for item in refs]
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, required_concepts={fact.concept}, source_evidence=changed_refs,
    ).validation
    assert result.accepted == ()
    assert "statement_source_concept_mismatch" in result.issues


def test_continuation_allocation_keeps_wrapped_title_evidence():
    facts, refs = parse([
        Page([("Consolidated Statement of",), ("Comprehensive Income",), ("RMB in thousands",),
              ("Item", "2025", "2024"), ("Total comprehensive income for the year", "80", "70")]),
        Page([("Attributable to:",), ("Owners of the Company", "85", "75"),
              ("Non-controlling interests", "(5)", "(5)")]),
    ])
    fact = next(item for item in facts if item.concept == "parent_comprehensive_income" and item.fiscal_year == 2025)
    row = next(item for item in refs if item.evidence_id in fact_row_evidence_ids(fact))
    title = next(item for item in refs if item.evidence_id in fact.classification_evidence_ids)
    assert title.locator == "page:1"
    assert title.excerpt == "Consolidated Statement of\nComprehensive Income"
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period, fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation
    assert result.accepted == (fact,)
