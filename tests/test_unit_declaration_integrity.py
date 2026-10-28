"""Independent admission must prove money units from literal source headers."""
import json
from dataclasses import replace
from unittest.mock import patch

from openthesis.domain import FilingDocument
from openthesis.financial_contracts import fact_row_evidence_ids
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    _checkpoint_context_from_dict,
    _checkpoint_context_to_dict,
    _manifest_for,
)
from openthesis.financial_validation import FinancialValidator
from openthesis.markets import build_company
from tests.test_pdf_statement_semantics import Page, Pdf, parse


def observation(header="RMB in thousands"):
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), (header,), ("Item", "2025", "2024"),
        ("Net cash generated from operating activities", "120", "100"),
    ])])
    return next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025), refs


def validate(fact, refs):
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    return FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                 fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, required_concepts={"operating_cash_flow"}, source_evidence=refs,
    ).validation


def test_arithmetically_consistent_forged_multiplier_cannot_pass_original_header():
    fact, refs = observation()
    assert validate(fact, refs).accepted == (fact,)
    forged = replace(fact, value=120000000, unit_scale=1000000,
                     normalization_lineage=replace(fact.normalization_lineage,
                         scale="1000000", normalized_value="120000000"))
    result = validate(forged, refs)
    assert result.accepted == ()
    assert "normalization_unit_declaration_mismatch" in result.issues


def test_currency_and_multiplier_on_separate_header_lines_are_both_bound():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("Item", "2025", "2024"),
        ("", "RMB", "RMB"), ("(in millions, except per share data)",),
        ("Net cash generated from operating activities", "120", "100"),
    ])])
    fact = next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025)
    assert fact.normalization_lineage.normalized_value == "120000000"
    assert validate(fact, refs).accepted == (fact,)


def test_scaled_currency_column_is_not_overridden_by_broad_currency_only_line():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("Expressed in Renminbi (RMB)",),
        ("Item", "2025", "2024"), ("", "RMB’000", "RMB’000"),
        ("Net cash generated from operating activities", "120", "100"),
    ])])
    fact = next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025)
    assert fact.normalization_lineage.normalized_value == "120000"
    assert validate(fact, refs).accepted == (fact,)


def test_chapter_scale_without_currency_binds_statement_wide_currency_evidence():
    facts, refs = parse([
        Page([("合并资产负债表",), ("财务报表单位：千元",), ("项目", "2025", "2024"),
              ("资产总计", "200", "180")]),
        Page([("合并现金流量表",), ("项目", "2025", "2024"),
              ("经营活动产生的现金流量净额", "120", "100")]),
        Page([("本财务报表除另有说明外，所有金额均以人民币千元表示。",)]),
    ], selected={1, 2})
    fact = next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025)
    assert fact.unit_provenance == "chapter_inherited"
    assert fact.normalization_lineage.normalized_value == "120000"
    assert validate(fact, refs).accepted == (fact,)
    proof = [ref for ref in refs if ref.evidence_id in fact.normalization_lineage.unit_evidence_ids]
    assert {ref.locator for ref in proof} == {"page:1", "page:3"}


def test_arithmetically_consistent_forged_currency_cannot_pass_original_header():
    fact, refs = observation()
    forged = replace(fact, currency="USD", unit="USD", normalization_lineage=replace(
        fact.normalization_lineage, currency="USD"))
    result = validate(forged, refs)
    assert result.accepted == ()
    assert "normalization_unit_declaration_mismatch" in result.issues


def test_million_yuan_header_is_not_ten_thousand_substring():
    fact, refs = observation("人民币百万元")
    assert fact.normalization_lineage.scale == "1000000"
    assert fact.normalization_lineage.normalized_value == "120000000"
    assert validate(fact, refs).accepted == (fact,)


def test_bare_display_unit_without_currency_does_not_use_issuer_currency_as_proof():
    fact, refs = observation("单位：元")
    result = validate(fact, refs)
    assert result.accepted == ()
    assert "normalization_unit_declaration_mismatch" in result.issues


def test_opening_column_uses_preceding_fiscal_year_without_rewriting_header():
    fact, refs = observation()
    fact = replace(fact, source_column=f"{fact.fiscal_year + 1}年1月1日")
    assert validate(fact, refs).accepted == (fact,)
    wrong = replace(fact, source_column=f"{fact.fiscal_year + 1}年12月31日")
    assert "normalization_period_column_mismatch" in validate(wrong, refs).issues


def test_same_original_comparison_from_a_separate_explicit_header_is_not_unit_conflict():
    from openthesis.domain import FilingDocument
    from openthesis.financial_contracts import CandidateBatch, FactCandidate, CompilationPolicy
    from openthesis.financial_compiler import FinancialFactCompiler
    from openthesis.markets import build_company

    facts, refs = parse([
        Page([("Consolidated Statement of Cash Flows",), ("RMB in millions",),
              ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", "120", "100")]),
        Page([("Consolidated Statement of Cash Flows",), ("RMB in thousands",),
              ("Item", "2025", "2024"),
              ("Net cash generated from operating activities", "120000", "100000")]),
    ])
    selected = [fact for fact in facts if fact.concept == "operating_cash_flow" and
                ((fact.source_page == 1 and fact.fiscal_year == 2025) or
                 (fact.source_page == 2 and fact.fiscal_year == 2024))]
    options = tuple(FactCandidate(fact, tuple(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact)), "pdf")
                    for fact in selected)
    row = options[0].evidence[0]
    filing = FilingDocument(row.document_id, selected[0].company_cik, selected[0].accession_number,
                           "ANNUAL_REPORT", "FY", "2025-12-31", selected[0].filed_at,
                           "2025 Annual Report", row.source_url, content_hash=row.content_hash)
    dataset = FinancialFactCompiler().compile(
        build_company("002594.SZ", "BYD"), (CandidateBatch(filing, options, tuple(refs)),),
        CompilationPolicy(filings=(filing,), reporting_currency="CNY", required_concepts=frozenset({"operating_cash_flow"})),
    )
    assert dataset.allow_ai, dataset.diagnostics
    assert len(dataset.research_facts) == 2
    assert "same_filing_comparator_unit_mismatch" not in dataset.diagnostics


def test_explicit_statement_scale_conflict_is_not_swallowed_by_first_header():
    facts, refs = parse([Page([
        ("Consolidated Statement of Cash Flows",), ("RMB in millions",),
        ("Item", "2025", "2024"), ("", "RMB'000", "RMB'000"),
        ("Net cash generated from operating activities", "120", "100"),
    ])])
    fact = next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025)
    row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
    result = FinancialValidator().validate_group(
        [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                 fact.consolidated_scope, fact.currency),
        {fact.fact_id: row}, required_concepts={"operating_cash_flow"}, source_evidence=refs,
    ).validation

    assert not result.accepted
    assert result.status == "REJECTED"
    assert "normalization_unit_declaration_mismatch" in result.issues


def test_rmb_period_columns_win_over_usd_convenience_column_and_validate():
    facts, refs = parse([
        Page([
            ("Consolidated income statement",), ("Item", "2025", "2024", "2025 US$"),
            ("", "RMB", "RMB", "US$"), ("(in millions, except per share data)",),
            ("Revenue", "100", "90", "12"),
        ]),
        Page([
            ("Consolidated statements of cash flows",), ("Item", "2025", "2024", "2025 US$"),
            ("", "RMB", "RMB", "US$"), ("(in millions, except per share data)",),
            ("Net cash provided by operating activities", "30", "25", "4"),
        ]),
    ])
    selected = [
        fact for fact in facts
        if fact.concept in {"revenue", "operating_cash_flow"}
        and fact.fiscal_year in {2024, 2025}
    ]

    assert {(fact.concept, fact.fiscal_year): fact.value for fact in selected} == {
        ("revenue", 2025): 100_000_000,
        ("revenue", 2024): 90_000_000,
        ("operating_cash_flow", 2025): 30_000_000,
        ("operating_cash_flow", 2024): 25_000_000,
    }
    for fact in selected:
        assert fact.currency == "CNY"
        assert fact.unit_scale == 1_000_000
        assert fact.source_column in {"2025", "2024"}
        row = next(ref for ref in refs if ref.evidence_id in fact_row_evidence_ids(fact))
        validation = FinancialValidator().validate_group(
            [fact], (fact.accession_number, fact.end_date, fact.fiscal_period,
                     fact.consolidated_scope, fact.currency),
            {fact.fact_id: row}, required_concepts={fact.concept}, source_evidence=refs,
        ).validation
        assert validation.accepted == (fact,)
        unit_refs = [
            ref for ref in refs
            if ref.evidence_id in fact.normalization_lineage.unit_evidence_ids
        ]
        assert unit_refs
        assert all("US$" not in ref.excerpt and ref.locator in {"page:1", "page:2"} for ref in unit_refs)


def test_continued_statement_preserves_unit_page_through_checkpoint_roundtrip():
    company = build_company("002594.SZ", "BYD")
    filing = FilingDocument(
        "unit-continuation", company.security_id, "unit-continuation", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-27T00:00:00+00:00", "2025 Annual Report",
        "https://example.invalid/unit-continuation.pdf", content_hash="a" * 64,
    )
    pages = [
        Page([
            ("Consolidated Statement of Cash Flows",), ("Item", "2025", "2024"),
            ("", "RMB", "RMB"), ("(in millions, except per share data)",),
        ]),
        Page([
            ("Consolidated Statement of Cash Flows (continued)",),
            ("Net cash generated from operating activities", "120", "100"),
        ]),
    ]
    engine = FinancialIngestionEngine()
    with patch("pdfplumber.open", return_value=Pdf(pages)):
        facts, refs = engine._parse_pdf_ast(
            "unit-continuation.pdf", company, filing, _manifest_for(filing),
            candidate_pages=frozenset({1, 2}), index_precomputed=True,
        )

    current = next(f for f in facts if f.concept == "operating_cash_flow" and f.fiscal_year == 2025)
    assert current.value == 120_000_000
    assert current.currency == "CNY"
    assert current.unit_scale == 1_000_000
    assert current.source_page == 2
    assert current.source_column == "2025"
    unit_refs = [ref for ref in refs if ref.evidence_id in current.normalization_lineage.unit_evidence_ids]
    assert unit_refs and all(ref.locator == "page:1" for ref in unit_refs)

    context = engine._last_pdf_context
    payload = json.loads(json.dumps(_checkpoint_context_to_dict(context)))
    restored = _checkpoint_context_from_dict(payload)
    assert restored is not None
    assert restored.unit_evidence_page == 1
    assert restored.periods == context.periods
    assert all(column.unit_page == 1 for column in restored.periods)
