from __future__ import annotations

import pytest

from openthesis.financial_ingestion import (
    PdfCellAST,
    PdfRowAST,
    _interest_parent_row,
    _merge_visual_rows,
    _period_columns,
    _select_period_cell,
    _summary_revision_boundary,
)
from openthesis.financial_taxonomy import interest_income_row_concept


@pytest.mark.parametrize(
    ("heading", "expected"),
    (
        ("一、营业总收入", "interest_revenue"),
        ("财务费用", "finance_interest_income"),
        ("", None),
    ),
)
def test_interest_income_uses_only_its_explicit_statement_parent(
    heading: str, expected: str | None,
) -> None:
    assert interest_income_row_concept("利息收入", heading) == expected


@pytest.mark.parametrize(("label", "heading", "expected"), (
    ("Including: Interest revenue", "Revenues", "interest_revenue"),
    ("Interest income", "Operating revenue", "interest_revenue"),
    ("Including: Interest revenue", "Revenues Including: Interest revenue 30 22", "interest_revenue"),
    ("Interest revenue", "Revenues increased including interest revenue 30", None),
    ("Interest revenue", "Cost of revenue", None),
    ("Revenue including interest revenue", "Revenues", None),
))
def test_english_interest_leaf_requires_an_exact_revenue_parent_or_literal_proof(
    label, heading, expected,
):
    assert interest_income_row_concept(label, heading) == expected


def _row(top: float, entries: tuple[tuple[str, float, float], ...]) -> PdfRowAST:
    cells = tuple(
        PdfCellAST(text, x0, top, x1, top + 8)
        for text, x0, x1 in entries
    )
    return PdfRowAST(
        cells,
        top,
        (min(cell.x0 for cell in cells), top, max(cell.x1 for cell in cells), top + 8),
    )


@pytest.mark.parametrize("boundary", (
    "营业总成本", "投资收益", "二、营业总成本", "IV. 投资收益",
    "Cost of sales", "III. Operating expenses",
))
def test_interest_parent_search_stops_at_recent_statement_boundaries(
    boundary: str,
) -> None:
    rows = (
        _row(0, (("其他项目", 20, 150),)),
        _row(10, (("其他项目", 20, 150),)),
        _row(20, (("营业总收入", 20, 150),)),
        _row(30, (("其他项目", 20, 150),)),
        _row(40, ((boundary, 20, 150),)),
        _row(50, (("其他项目", 20, 150),)),
        _row(60, (("其他项目", 20, 150),)),
        _row(70, (("其他项目", 20, 150),)),
        _row(80, (("其他项目", 20, 150),)),
        _row(90, (("其他项目", 20, 150),)),
        _row(100, (("利息收入", 20, 150), ("30", 300, 340))),
    )

    assert _interest_parent_row(rows, 10) is None


def test_valued_interest_income_keeps_its_own_row_and_parent_amount_separate() -> None:
    parent_expense = _row(10, (("财务费用", 30, 150), ("3,000", 300, 350)))
    interest_income = _row(
        20,
        (("利息收入", 30, 150), ("30", 300, 330), ("22", 380, 410)),
    )
    unvalued_premium = _row(30, (("已赚保费", 30, 150),))
    rows = (parent_expense, interest_income, unvalued_premium)

    assert _interest_parent_row(rows, 1) == parent_expense
    merged = _merge_visual_rows(rows, 1)

    assert merged.cells == interest_income.cells
    assert [cell.text for cell in merged.cells] == ["利息收入", "30", "22"]
    assert "3,000" not in [cell.text for cell in merged.cells]
    assert "已赚保费" not in [cell.text for cell in merged.cells]


def test_summary_revision_boundary_requires_a_nonnumeric_header_row() -> None:
    header = _row(
        0,
        (("项目", 10, 80), ("业绩快报", 100, 170), ("年度报告", 200, 270), ("变动比例", 300, 380)),
    )
    valued_row = _row(
        10,
        (("项目", 10, 80), ("业绩快报", 100, 170), ("年度报告", 200, 270),
         ("变动比例", 300, 380), ("100", 400, 440), ("90", 460, 500), ("11%", 520, 560)),
    )

    assert _summary_revision_boundary((header,)) == 0
    assert _summary_revision_boundary((valued_row,)) is None


def test_narrative_audit_year_does_not_replace_real_period_columns() -> None:
    header = _row(0, (("项目", 10, 80), ("2025", 220, 260), ("2024", 340, 380)))
    narrative = _row(12, (("会计师事务所审计。2025", 10, 220),))

    columns = _period_columns((header, narrative))

    assert [(column.year, column.header) for column in columns] == [
        (2025, "2025"),
        (2024, "2024"),
    ]


def test_restatement_header_selects_the_adjusted_comparative_column() -> None:
    header = _row(
        0,
        (("项目", 10, 80), ("2024（调整前）", 200, 250),
         ("2024（调整后）", 300, 350), ("2023", 400, 450)),
    )
    values = _row(
        12,
        (("营业收入", 10, 80), ("90", 200, 240), ("100", 300, 340), ("80", 400, 440)),
    )

    columns = _period_columns((header, values))
    current_comparator = next(column for column in columns if column.year == 2024)
    selected = _select_period_cell(values, 80, columns, 2024)

    assert current_comparator.header == "2024（调整后）"
    assert selected is not None
    assert selected.text == "100"
