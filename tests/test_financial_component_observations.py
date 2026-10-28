"""Per-fact diagnostics preserve source observations without changing admission."""
import json

from openthesis.domain import FilingDocument, FinancialFact
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_contracts import (
    FinancialValidation,
    ValidationStatus,
    fact_row_evidence_ids,
)
from openthesis.financial_validation import FinancialValidator
from openthesis.markets import build_company
from tests.test_pdf_statement_semantics import Page, parse
from tests.test_unit_declaration_integrity import observation


def _validate(fact, refs):
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    return FinancialValidator().validate_group(
        [fact],
        (fact.accession_number, fact.end_date, fact.fiscal_period,
         fact.consolidated_scope, fact.currency),
        {fact.fact_id: row},
        required_concepts={fact.concept},
        source_evidence=refs,
    ).validation


def _record(validation, fact):
    return next(record for record in validation.fact_records if record.fact_id == fact.fact_id)


def test_bare_yuan_unit_keeps_observed_amount_and_scale_but_currency_unknown():
    fact, refs = observation("单位：元")

    validation = _validate(fact, refs)
    record = _record(validation, fact)
    components = dict(record.components)

    assert validation.accepted == ()
    assert not record.accepted
    assert components["amount"].state == "observed"
    assert components["amount"].value == fact.normalization_lineage.raw_value
    assert components["scale"].state == "observed"
    assert components["scale"].value == "1"
    assert components["currency"].state == "unknown"
    assert "normalization_unit_declaration_mismatch" in record.issues
    assert components["amount"].evidence_ids
    assert components["scale"].evidence_ids
    assert json.loads(json.dumps(record.to_dict()))["components"]["currency"]["state"] == "unknown"


def test_explicit_rmb_yuan_records_each_component_as_source_proven():
    fact, refs = observation("单位：人民币元")

    validation = _validate(fact, refs)
    record = _record(validation, fact)
    components = dict(record.components)

    assert validation.accepted == (fact,)
    assert record.accepted
    assert {name: item.state for name, item in components.items()} == {
        "amount": "verified",
        "scale": "verified",
        "currency": "verified",
        "period": "verified",
        "scope": "verified",
    }
    assert all(item.evidence_ids for item in components.values())


def test_forged_usd_is_a_currency_conflict_not_an_observed_currency():
    fact, refs = observation("单位：人民币元")
    from dataclasses import replace

    forged = replace(
        fact,
        currency="USD",
        unit="USD",
        normalization_lineage=replace(fact.normalization_lineage, currency="USD"),
    )

    validation = _validate(forged, refs)
    record = _record(validation, forged)
    currency = dict(record.components)["currency"]

    assert validation.accepted == ()
    assert not record.accepted
    assert currency.state == "conflict"
    assert currency.value == "USD"
    assert currency.evidence_ids


def test_compiler_retains_rejected_per_fact_component_record():
    fact, refs = observation("单位：元")
    company = build_company("002594.SZ", "BYD")
    filing = FilingDocument(
        "byd-fixture", company.security_id, "byd-fixture", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-27T00:00:00+00:00", "2025 年年度报告",
        "https://example.invalid/byd.pdf", content_hash="a" * 64,
    )

    dataset = FinancialFactCompiler().compile_facts(
        company,
        [filing],
        [fact],
        required_concepts=frozenset({"operating_cash_flow"}),
        evidence=refs,
    )
    records = dataset.coverage["fact_validation_records"]
    record = records[fact.fact_id]

    assert fact.fact_id not in {item.fact_id for item in dataset.research_facts}
    assert record["accepted"] is False
    assert "normalization_unit_declaration_mismatch" in record["issues"]
    assert record["components"]["amount"]["state"] == "observed"
    assert record["components"]["currency"]["state"] == "unknown"


def test_compiler_group_record_overrides_individual_acceptance_for_atomic_imbalance():
    facts, refs = parse([Page([
        ("Consolidated Balance Sheets",), ("RMB in thousands",), ("Item", "2025", "2024"),
        ("Total assets", "100", "90"),
        ("Total liabilities", "20", "20"),
        ("Total equity", "50", "45"),
    ])])
    annual_facts = [fact for fact in facts if fact.fiscal_year == 2025]
    company = build_company("002594.SZ", "BYD")
    filing = FilingDocument(
        "byd-fixture", company.security_id, "byd-fixture", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-27T00:00:00+00:00", "2025 年年度报告",
        "https://example.invalid/byd.pdf", content_hash="a" * 64,
    )

    dataset = FinancialFactCompiler().compile_facts(
        company,
        [filing],
        annual_facts,
        required_concepts=frozenset({"assets", "liabilities", "total_equity"}),
        evidence=refs,
    )
    records = dataset.coverage["fact_validation_records"]

    assert not dataset.allow_ai
    assert set(records) == {fact.fact_id for fact in annual_facts}
    assert all(record["accepted"] is False for record in records.values())
    assert all("balance_sheet_imbalance" in record["issues"] for record in records.values())


def test_financial_validation_keeps_legacy_positional_constructor_compatibility():
    validation = FinancialValidation(ValidationStatus.VERIFIED, (), frozenset())

    assert validation.fact_records == ()


def test_legacy_accepted_fact_without_independent_source_proof_is_not_verified():
    fact = FinancialFact(
        "legacy-cash-flow", "002594.SZ", "operating_cash_flow", "Net cash generated from operating activities",
        120.0, "CNY", 2025, "FY", "ANNUAL_REPORT", "2025-01-01", "2025-12-31",
        "2026-03-27", "byd-fixture", "https://example.invalid/byd.pdf",
        statement="cash_flow", currency="CNY", unit_scale=1.0,
        raw_text="Net cash generated from operating activities 120",
        parser_version="legacy-import-v1",
    )

    validation = FinancialValidator().validate_group(
        [fact],
        (fact.accession_number, fact.end_date, fact.fiscal_period,
         fact.consolidated_scope, fact.currency),
        required_concepts={fact.concept},
    ).validation
    record = _record(validation, fact)

    assert validation.accepted == (fact,)
    assert record.accepted
    assert all(item.state != "verified" for _name, item in record.components)
