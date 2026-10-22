"""Literal interim header geometry, not a year inferred from nearby prose."""
import pytest

from openthesis.financial_ingestion import PdfCellAST, PdfRowAST, _period_columns, _select_period_cell


def row(cells, top=10):
    values = tuple(PdfCellAST(text, left, top, right, top + 10) for text, left, right in cells)
    return PdfRowAST(values, top, (min(c.x0 for c in values), top, max(c.x1 for c in values), top + 10))


@pytest.mark.parametrize('suffix', ['年半年度', '年中期', '年第一季度', '年第三季度', '年1—6月', '年1-9月'])
def test_interim_period_columns_keep_literal_source_headers(suffix):
    header = row([('项目', 10, 40), ('2026' + suffix, 300, 380), ('2025' + suffix, 430, 510)])
    values = row([('营业收入', 10, 80), ('38,635,115', 300, 380), ('32,348,049', 430, 510)], 30)
    columns = _period_columns((header, values))
    assert [item.year for item in columns] == [2026, 2025]
    assert [item.header for item in columns] == ['2026' + suffix, '2025' + suffix]
    assert _select_period_cell(values, 80, columns, 2026).text == '38,635,115'
    assert _select_period_cell(values, 80, columns, 2025).text == '32,348,049'


def test_prose_with_interim_years_does_not_create_numeric_columns():
    prose = row([('2026年半年度收入较2025年半年度提高', 10, 510)])
    assert _period_columns((prose,)) == ()
