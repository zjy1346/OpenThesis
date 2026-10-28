"""Cash acquisitions, lease liabilities and deferred consideration are distinct."""
from dataclasses import replace

import pytest

from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_taxonomy import capital_allocation_payment_concept
from openthesis.financial_validation import FinancialValidator
from tests.test_pdf_statement_semantics import Page, parse


def payment_source():
    return parse([
        Page([
            ("Consolidated Statement of Cash Flows",), ("RMB in thousands",),
            ("Item", "2025", "2024"), ("Cash flows from investing activities",),
            ("Purchase of property, plant and equipment, land use rights",),
            ("and intangible assets", "(12,769,155)", "(7,297,307)"),
        ], line_gap=12),
        Page([
            ("Cash flows from financing activities",),
            ("Payments of lease liabilities", "(2,902,504)", "(2,476,524)"),
            ("Payments of deferred consideration for acquisition of",),
            ("intangible assets", "(2,499,913)", "(3,182,954)"),
        ], line_gap=12),
    ])


def validate_one(fact, refs):
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    return FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                 fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, required_concepts={fact.concept}, source_evidence=refs,
    ).validation


def test_wrapped_deferred_payment_keeps_own_amount_and_not_previous_lease_amount():
    facts, refs = payment_source()
    expected = {
        ("capital_expenditure", 2025): "12769155000",
        ("capital_expenditure", 2024): "7297307000",
        ("lease_liability_payment", 2025): "2902504000",
        ("lease_liability_payment", 2024): "2476524000",
        ("deferred_asset_acquisition_payment", 2025): "2499913000",
        ("deferred_asset_acquisition_payment", 2024): "3182954000",
    }
    observations = {}
    for fact in facts:
        if fact.concept in {key[0] for key in expected}:
            observations.setdefault((fact.concept, fact.fiscal_year), set()).add(
                fact.normalization_lineage.normalized_value
            )
            assert validate_one(fact, refs).accepted == (fact,)
    assert observations == {key: {value} for key, value in expected.items()}
    deferred = next(fact for fact in facts
                    if fact.concept == "deferred_asset_acquisition_payment" and fact.fiscal_year == 2025)
    assert "2,499,913" in deferred.raw_text
    assert "2,902,504" not in deferred.raw_text
    assert "intangible assets" in deferred.reported_concept


@pytest.mark.parametrize("label", [
    "Payments of deferred consideration for acquisition of shares",
    "Payments of deferred consideration for acquisition of a subsidiary",
    "Payments of deferred consideration for acquisition of",
    "Disclosure of payments of lease liabilities",
    "Payments of lease liabilities and payments of deferred consideration for acquisition of intangible assets",
    "Payments of lease liabilities and interest",
])
def test_payment_concept_needs_one_complete_source_label(label):
    assert capital_allocation_payment_concept(label) is None


@pytest.mark.parametrize("label, concept", [
    ("Payments of lease liabilities", "lease_liability_payment"),
    ("Payment of lease liabilities", "lease_liability_payment"),
    ("Payments of deferred consideration for acquisition of intangible assets", "deferred_asset_acquisition_payment"),
    ("Payments of deferred consideration for acquisition of property, plant and equipment", "deferred_asset_acquisition_payment"),
])
def test_payment_concept_retains_literal_scope(label, concept):
    assert capital_allocation_payment_concept(label) == concept


@pytest.mark.parametrize("forge_label", [False, True])
def test_payment_cannot_be_relabelled_as_capex_even_with_forged_alias(forge_label):
    facts, refs = payment_source()
    original = next(fact for fact in facts
                    if fact.concept == "deferred_asset_acquisition_payment" and fact.fiscal_year == 2025)
    assert validate_one(original, refs).accepted == (original,)
    forged = replace(original, concept="capital_expenditure",
                     reported_concept="purchase of intangible assets" if forge_label else original.reported_concept)
    result = validate_one(forged, refs)
    assert result.accepted == ()
    assert "capital_allocation_source_concept_mismatch" in result.issues


def test_unsupported_deferred_business_purchase_is_preserved_not_asset_capex():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("RMB in thousands",),
        ("Item", "2025", "2024"),
        ("Payments of deferred consideration for acquisition of shares", "(123)", "(100)"),
    ])])
    assert not any(fact.concept in {"capital_expenditure", "deferred_asset_acquisition_payment"}
                   for fact in facts)
    assert any("acquisition of shares" in ref.excerpt and "123" in ref.excerpt for ref in refs)


def test_merged_financing_labels_cannot_be_accepted_by_candidate_concept():
    facts, refs = payment_source()
    original = next(fact for fact in facts
                    if fact.concept == "lease_liability_payment" and fact.fiscal_year == 2025)
    label = ("Payments of lease liabilities and payments of deferred consideration "
             "for acquisition of intangible assets")
    raw = label + " (2,902,504) (2,476,524)"
    forged = replace(original, reported_concept=label, raw_text=raw)
    bound_ids = fact_row_evidence_ids(original)
    changed_refs = [replace(ref, excerpt=raw) if ref.evidence_id in bound_ids else ref for ref in refs]
    result = validate_one(forged, changed_refs)
    assert result.accepted == ()
    assert "capital_allocation_source_concept_mismatch" in result.issues


def test_small_purchase_amounts_that_resemble_years_remain_amounts():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("RMB in thousands",),
        ("Item", "2025", "2024"),
        ("Purchase of property, plant and equipment", "(2025)", "(2024)"),
    ], line_gap=12)])
    current = next(fact for fact in facts if fact.concept == "capital_expenditure" and fact.fiscal_year == 2025)
    assert current.normalization_lineage.normalized_value == "2025000"
    assert validate_one(current, refs).accepted == (current,)
