"""Relative-period PDF parsing regressions for the 3.0.3 ingestion contract."""

import json
from unittest.mock import patch

from openthesis.domain import FilingDocument
from openthesis.financial_ingestion import (
    FinancialIngestionEngine,
    PdfCellAST,
    PdfRowAST,
    _checkpoint_context_from_dict,
    _checkpoint_context_to_dict,
    _manifest_for,
    _page_sections,
)
from openthesis.markets import build_company
from tests.test_pdf_statement_semantics import Page, Pdf


def _parse_period(pages, *, period="FY", period_end="2025-12-31"):
    """Exercise the real positioned ``FinancialIngestionEngine`` seam."""

    company = build_company("002594.SZ", "BYD")
    title = {
        "Q1": "2025年第一季度报告",
        "Q3": "2025年第三季度报告",
        "H1": "2025年半年度报告",
    }.get(period, "2025 年年度报告")
    filing = FilingDocument(
        "relative-period-fixture",
        company.security_id,
        "relative-period-fixture",
        "ANNUAL_REPORT",
        period,
        period_end,
        "2026-03-27T00:00:00+00:00",
        title,
        "https://example.invalid/relative-period.pdf",
        content_hash="a" * 64,
    )
    with patch("pdfplumber.open", return_value=Pdf(pages)):
        return FinancialIngestionEngine()._parse_pdf_ast(
            "nonexistent-positioned-relative-period-fixture.pdf",
            company,
            filing,
            _manifest_for(filing),
            candidate_pages=frozenset(range(1, len(pages) + 1)),
            index_precomputed=True,
        )


def _ast_row(*texts):
    cells = []
    for index, text in enumerate(texts):
        x0 = 50.0 if index == 0 else 370.0 + 140.0 * (index - 1)
        text = str(text)
        cells.append(PdfCellAST(text, x0, 10.0, x0 + max(40.0, len(text) * 6.0), 20.0))
    return PdfRowAST(tuple(cells), 10.0, (50.0, 10.0, 700.0, 20.0))


def test_annual_chinese_current_and_prior_columns_emit_comparator_with_original_headers():
    facts, _refs = _parse_period([
        Page([
            ("合并利润表",),
            ("单位：人民币千元",),
            ("项目", "本期发生额", "上期发生额"),
            ("营业收入", "100", "80"),
        ])
    ])

    revenue = {
        fact.fiscal_year: fact
        for fact in facts
        if fact.concept == "revenue"
    }
    assert set(revenue) == {2025, 2024}
    assert revenue[2025].value == 100_000.0
    assert revenue[2025].source_column == "本期发生额"
    assert revenue[2025].usage_status != "comparator"
    assert revenue[2024].value == 80_000.0
    assert revenue[2024].source_column == "上期发生额"
    assert revenue[2024].usage_status == "comparator"
    assert all(fact.fiscal_period == "FY" for fact in revenue.values())
    assert all(fact.consolidated_scope == "consolidated" for fact in revenue.values())


def test_annual_chinese_balance_end_and_beginning_columns_keep_roles():
    facts, _refs = _parse_period([
        Page([
            ("合并资产负债表",),
            ("单位：人民币千元",),
            ("项目", "期末余额", "期初余额"),
            ("资产总计", "100", "80"),
        ])
    ])

    assets = {
        fact.fiscal_year: fact
        for fact in facts
        if fact.concept == "assets"
    }
    assert set(assets) == {2025, 2024}
    assert assets[2025].source_column == "期末余额"
    assert assets[2024].source_column == "期初余额"
    assert assets[2024].usage_status == "comparator"
    assert assets[2025].value == 100_000.0
    assert assets[2024].value == 80_000.0


def test_annual_english_current_and_prior_columns_keep_semantic_headers():
    facts, _refs = _parse_period([
        Page([
            ("Consolidated income statement",),
            ("Unit: RMB thousands",),
            ("Item", "Current period", "Previous period"),
            ("Revenue", "100", "80"),
        ])
    ])

    revenue = {
        fact.fiscal_year: fact
        for fact in facts
        if fact.concept == "revenue"
    }
    assert set(revenue) == {2025, 2024}
    assert revenue[2025].source_column == "Current period"
    assert revenue[2024].source_column == "Previous period"
    assert revenue[2024].usage_status == "comparator"
    assert revenue[2025].normalization_lineage.normalized_value == "100000"
    assert revenue[2024].normalization_lineage.normalized_value == "80000"


def test_continuation_checkpoint_preserves_relative_roles_and_unit_page():
    first_rows = (
        _ast_row("Consolidated income statement"),
        _ast_row("Unit: RMB thousands"),
        _ast_row("Item", "Current period", "Previous period"),
        _ast_row("Revenue", "100", "80"),
    )
    first = _page_sections(
        None,
        "Consolidated income statement\nUnit: RMB thousands",
        first_rows,
        1,
        "CNY",
    )
    assert first
    original_context = first[0].context
    assert [(column.year, column.relative_period, column.header) for column in original_context.periods] == [
        (0, "current", "Current period"),
        (0, "prior", "Previous period"),
    ]

    encoded = json.loads(json.dumps(_checkpoint_context_to_dict(original_context)))
    restored = _checkpoint_context_from_dict(encoded)
    assert restored == original_context
    assert restored.unit_provenance == "explicit"
    assert restored.unit_evidence_page == 1
    assert [column.relative_period for column in restored.periods] == ["current", "prior"]

    facts, refs = _parse_period([
        Page([
            ("Consolidated income statement",),
            ("Unit: RMB thousands",),
            ("Item", "Current period", "Previous period"),
            ("Revenue", "100", "80"),
        ]),
        Page([
            ("Consolidated income statement (continued)",),
            ("Net income", "8", "7"),
        ]),
    ])
    current_net_income = next(
        fact for fact in facts
        if fact.concept == "net_income" and fact.fiscal_year == 2025
    )
    assert current_net_income.source_page == 2
    assert current_net_income.unit_provenance == "inherited"
    assert current_net_income.source_column == "Current period"
    unit_refs = [
        ref for ref in refs
        if current_net_income.normalization_lineage
        and ref.evidence_id in current_net_income.normalization_lineage.unit_evidence_ids
    ]
    assert unit_refs
    assert all(ref.locator == "page:1" for ref in unit_refs)


def test_quarterly_relative_prior_does_not_blindly_infer_prior_fiscal_year():
    facts, _refs = _parse_period(
        [
            Page([
                ("Consolidated income statement",),
                ("Unit: RMB thousands",),
                ("Item", "Current period", "Previous period"),
                ("Revenue", "100", "80"),
            ])
        ],
        period="Q1",
        period_end="2025-03-31",
    )

    revenue = [fact for fact in facts if fact.concept == "revenue"]
    assert [(fact.fiscal_year, fact.fiscal_period, fact.source_column) for fact in revenue] == [
        (2025, "Q1", "Current period")
    ]
    assert all(fact.usage_status != "comparator" for fact in revenue)


def test_isolated_annual_header_does_not_fabricate_comparator():
    facts, _refs = _parse_period([
        Page([
            ("合并利润表",),
            ("单位：人民币千元",),
            ("项目", "上期发生额"),
            ("营业收入", "80"),
        ])
    ])

    assert not any(fact.concept == "revenue" and fact.fiscal_year == 2024 for fact in facts)
    assert not any(fact.usage_status == "comparator" for fact in facts)
