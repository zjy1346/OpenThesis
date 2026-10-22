"""Chinese-adjacent fiscal-year labels are not reported financial amounts."""

from openthesis.research_numeric_assertions import _quantities, numeric_prose_supported


def _fact(year, value):
    return {
        "kind": "financial_fact",
        "fiscal_year": year,
        "value": value,
        "unit": "CNY",
        "currency": "CNY",
    }


def test_fy_year_tight_against_chinese_label_is_ignored_but_amount_is_checked():
    text = "FY2025合并营业收入为2025元"
    records = (_fact(2025, 2025),)

    quantities = _quantities(text, records)

    assert [(item.number, item.fiscal_year) for item in quantities] == [(2025, "2025")]
    assert numeric_prose_supported(text, records) is True


def test_real_2025_yuan_amount_is_not_hidden_as_the_fiscal_year():
    text = "FY2025合并营业收入为2025元"

    assert numeric_prose_supported(text, (_fact(2025, 2024),)) is False


def test_fy_year_does_not_let_a_different_period_support_the_amount():
    text = "FY2025合并营业收入为2025元"

    assert numeric_prose_supported(text, (_fact(2024, 2025),)) is False
