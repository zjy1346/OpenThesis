from dataclasses import replace
from decimal import Decimal, localcontext

from openthesis.domain import FilingDocument
from openthesis.financial_contracts import FactCandidate, fact_row_evidence_ids
from openthesis.financial_contracts import CandidateBatch, CompilationPolicy
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_candidate_resolution import resolve_display_rounding
from openthesis.financial_validation import FinancialValidator
from openthesis.markets import build_company
from tests.test_pdf_statement_semantics import Page, parse


def observations(precise="34,142,377", rounded="34,142.4"):
    facts, refs = parse([
        Page([("Consolidated Statement of Cash Flows",), ("RMB in millions",),
              ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", rounded, "39,295.5")]),
        Page([("Consolidated Statement of Cash Flows",), ("RMB in thousands",),
              ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", precise, "39,295,499")]),
    ])
    facts = [fact for fact in facts if fact.concept == "operating_cash_flow" and fact.fiscal_year == 2025]
    options = tuple(FactCandidate(
        fact, tuple(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact)), "pdf",
    ) for fact in facts)
    row = options[0].evidence[0]
    filing = FilingDocument(row.document_id, facts[0].company_cik, facts[0].accession_number,
                           facts[0].form_type, "FY", "2025-12-31", facts[0].filed_at,
                           facts[0].source_document, row.source_url, content_hash=row.content_hash)
    return options, filing, refs


def test_same_original_finer_display_is_selected_without_altering_either_amount():
    options, filing, _refs = observations()
    result = resolve_display_rounding(options, filing)
    assert result is not None
    assert result.selected.fact.normalization_lineage.normalized_value == "34142377000"
    assert {option.fact.normalization_lineage.normalized_value for option in options} == {
        "34142400000.0", "34142377000",
    }
    assert result.rounded_candidate_ids


def test_difference_outside_printed_half_quantum_remains_unresolved():
    options, filing, _refs = observations(precise="34,142,499")
    assert resolve_display_rounding(options, filing) is None


def test_different_original_hash_or_currency_or_generation_cannot_reconcile():
    options, filing, _refs = observations()
    for altered in (
        replace(options[0], evidence=(replace(options[0].evidence[0], content_hash="b" * 64),)),
        replace(options[0], fact=replace(options[0].fact, currency="USD")),
        replace(options[0], fact=replace(options[0].fact, generation_id="other")),
    ):
        assert resolve_display_rounding((altered, options[1]), filing) is None


def test_two_disagreeing_finest_originals_remain_a_conflict():
    facts, refs = parse([
        Page([("Consolidated Statement of Cash Flows",), ("RMB in millions",), ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", "34,142.4", "39,295.5")]),
        Page([("Consolidated Statement of Cash Flows",), ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", "34,142,377", "39,295,499")]),
        Page([("Consolidated Statement of Cash Flows",), ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", "34,142,378", "39,295,499")]),
    ])
    options = tuple(FactCandidate(fact, tuple(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact)), "pdf")
                    for fact in facts if fact.concept == "operating_cash_flow" and fact.fiscal_year == 2025)
    _, filing, _ = observations()
    for option in options:
        result = FinancialValidator().validate_group(
            [option.fact], (option.fact.accession_number, option.fact.end_date, option.fact.fiscal_period,
                            option.fact.consolidated_scope, option.fact.currency),
            {option.fact.fact_id: option.evidence[0]}, required_concepts={"operating_cash_flow"}, source_evidence=refs,
        ).validation
        assert result.accepted == (option.fact,)
    assert resolve_display_rounding(options, filing) is None


def test_resolution_is_independent_of_global_decimal_precision():
    options, filing, _refs = observations()
    with localcontext() as context:
        context.prec = 3
        result = resolve_display_rounding(options, filing)
    assert result.selected.fact.normalization_lineage.normalized_value == "34142377000"


def compile_observations(options, filing, refs):
    return FinancialFactCompiler().compile(
        build_company("002594.SZ", "BYD"),
        (CandidateBatch(filing, options, tuple(refs)),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY",
                          required_concepts=frozenset({"operating_cash_flow"})),
    )


def test_compiler_reconciles_only_proven_display_rounding_and_keeps_audit_proof():
    options, filing, refs = observations()
    dataset = compile_observations(options, filing, refs)
    assert len(dataset.resolved_facts) == 1
    selected = dataset.resolved_facts[0]
    assert selected.normalization_lineage.normalized_value == "34142377000"
    assert dataset.conflicts == ()
    assert dataset.quarantined_facts == ()
    proof = dataset.coverage["candidate_resolutions"][0]
    assert proof["selected_fact_id"] == selected.fact_id
    assert proof["policy"] == "same-original-display-quantization-v1"
    assert set(proof["source_candidate_ids"]) == {option.fact.fact_id for option in options}
    assert {ref.evidence_id for ref in dataset.evidence} == {ref.evidence_id for ref in refs}


def test_compiler_does_not_reconcile_real_amount_disagreement():
    options, filing, refs = observations(precise="34,142,499")
    dataset = compile_observations(options, filing, refs)
    assert dataset.resolved_facts == ()
    assert len(dataset.quarantined_facts) == 2
    assert dataset.conflicts[0]["reason"] == "conflicting_candidate_values"
    assert not dataset.coverage.get("candidate_resolutions")


def test_identical_normalized_amount_at_different_explicit_scales_is_not_conflicting():
    options, filing, refs = observations(rounded="34,142.377")
    dataset = compile_observations(options, filing, refs)
    assert len(dataset.resolved_facts) == 1
    assert Decimal(dataset.resolved_facts[0].normalization_lineage.normalized_value) == Decimal("34142377000")
    assert dataset.conflicts == ()


def test_validator_compares_scales_within_bound_table_context_not_entire_document():
    options, _filing, refs = observations()
    facts = [option.fact for option in options]
    identity = (facts[0].accession_number, facts[0].end_date, facts[0].fiscal_period,
                facts[0].consolidated_scope, facts[0].currency)
    result = FinancialValidator().validate_group(
        facts, identity, {option.fact.fact_id: option.evidence[0] for option in options},
        required_concepts={"operating_cash_flow"}, source_evidence=refs,
    ).validation
    assert {fact.fact_id for fact in result.accepted} == {fact.fact_id for fact in facts}
    assert "statement_unit_scale_inconsistent" not in result.issues


def test_validator_still_rejects_inconsistent_scales_bound_to_one_unit_context():
    options, _filing, refs = observations()
    # The wrong observation claims a different scale but points at the fine
    # table's same explicit header. Group consistency must still reject it.
    wrong = replace(options[0].fact, normalization_lineage=replace(
        options[0].fact.normalization_lineage,
        unit_evidence_ids=options[1].fact.normalization_lineage.unit_evidence_ids,
    ))
    facts = [wrong, options[1].fact]
    result = FinancialValidator().validate_group(
        facts, (wrong.accession_number, wrong.end_date, wrong.fiscal_period,
                wrong.consolidated_scope, wrong.currency),
        {options[0].fact.fact_id: options[0].evidence[0],
         options[1].fact.fact_id: options[1].evidence[0]},
        required_concepts={"operating_cash_flow"}, source_evidence=refs,
    ).validation
    assert result.accepted == ()
    assert "statement_unit_scale_inconsistent" in result.issues


def test_capex_outflow_sign_and_printed_rounding_have_separate_exact_proofs():
    facts, refs = parse([
        Page([("Consolidated Statement of Cash Flows",), ("RMB in millions",), ("Item", "2025", "2024"),
              ("Capital expenditures", "5,799.6", "7,169.3")]),
        Page([("Consolidated Statement of Cash Flows",), ("RMB in thousands",), ("Item", "2025", "2024"),
              ("Purchases of property, plant and equipment and intangible assets", "(5,799,570)", "(7,169,313)")]),
    ])
    options = tuple(FactCandidate(fact, tuple(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact)), "pdf")
                    for fact in facts if fact.concept == "capital_expenditure" and fact.fiscal_year == 2025)
    assert len(options) == 2
    _, filing, _ = observations()
    dataset = FinancialFactCompiler().compile(
        build_company("002594.SZ", "BYD"), (CandidateBatch(filing, options, tuple(refs)),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", required_concepts=frozenset({"capital_expenditure"})),
    )
    assert [fact.normalization_lineage.normalized_value for fact in dataset.resolved_facts] == ["5799570000"]
    assert dataset.conflicts == ()
    assert {observation.fact.normalization_lineage.value_transform for observation in options} == {"absolute_value"}
    assert {ref.evidence_id for ref in dataset.evidence} == {ref.evidence_id for ref in refs}


def test_display_rounding_never_licenses_an_unapproved_sign_transform():
    options, filing, _ = observations()
    changed = tuple(replace(option, fact=replace(option.fact, normalization_lineage=replace(
        option.fact.normalization_lineage, value_transform="absolute_value",
    ))) for option in options)
    assert resolve_display_rounding(changed, filing) is None
