import pytest

from openthesis.research_numeric_assertions import numeric_prose_supported


def fact(value=4_200_000_000, currency="USD", unit=None):
    return {"kind": "financial_fact", "value": value, "currency": currency,
            "unit": unit or currency, "fiscal_year": 2021, "fiscal_period": "FY",
            "start_date": "2021-01-01", "end_date": "2021-12-31"}


@pytest.mark.parametrize("text", [
    "Revenue was USD 4.2 billion in 2021.",
    "FY2021 revenue was USD 4.2 billion.",
    "Revenue was $4,200,000,000.",
    "Revenue was 4,200 million USD for 2021.",
    "2021年收入为４．２十亿美元。",
    "Revenue was USD 4.2B for the year 2021.",
    "Revenue for 2021-01-01 to 2021-12-31 was USD 4.2 billion.",
])
def test_supported_display_scales_and_source_bound_dates(text):
    assert numeric_prose_supported(text, [fact()])


@pytest.mark.parametrize("text", [
    "Revenue was USD 999 billion in 2021.",
    "Revenue was CNY 4.2 billion in 2021.",
    "Revenue was ¥4.2 billion in 2021.",
    "Revenue was HK$4.2 billion in 2021.",
    "Revenue was 4.2% in 2021.",
    "Revenue was USD 4.2 billion in 2022.",
    "Revenue was USD 4.2 billion for 2021-01-02 to 2021-12-31.",
    "Revenue was USD 4.2.3 billion in 2021.",
    "Revenue was USD 4,20,000,000 in 2021.",
    "Revenue was 2021.",
    "Revenue was USD (4,200,000,000) in 2021.",
])
def test_conflicting_or_malformed_amounts_are_not_verified(text):
    assert not numeric_prose_supported(text, [fact()])


def test_accounting_parentheses_do_not_turn_loss_into_profit():
    text = "Net loss was USD (100)."
    assert numeric_prose_supported(text, [fact(-100)])
    assert not numeric_prose_supported(text, [fact(100)])


def test_written_precision_rounding_is_local_and_not_a_global_tolerance():
    assert numeric_prose_supported("Revenue was USD 4.2 billion.", [fact(4_234_567_890)])
    assert not numeric_prose_supported("Revenue was USD 4.20 billion.", [fact(4_234_567_890)])


def test_ratio_display_and_leading_decimal():
    assert numeric_prose_supported("Operating margin was 12.3%.", [fact(.123, "", "ratio")])
    assert not numeric_prose_supported("Operating margin was 99%.", [fact(.123, "", "ratio")])
    assert numeric_prose_supported("Ratio was .85.", [fact(.85, "", "ratio")])


def test_malformed_comma_cannot_be_split_into_two_matching_numbers():
    assert not numeric_prose_supported("Revenue was 1,23.", [fact(1), fact(23)])


def test_filing_numeric_assertion_requires_the_same_currency_and_display_unit():
    source = {"kind": "filing_text", "raw_text": "Revenue was CNY 120 million in 2021.",
              "fiscal_year": 2021}
    assert numeric_prose_supported("Revenue was CNY 120 million in 2021.", [source])
    assert not numeric_prose_supported("Revenue was USD 120 million in 2021.", [source])
    assert not numeric_prose_supported("Revenue was CNY 120 billion in 2021.", [source])


def test_comparators_must_match_the_year_of_their_own_clause():
    current = fact(4_200_000_000)
    prior = {**fact(100), "fiscal_year": 2020, "start_date": "2020-01-01", "end_date": "2020-12-31"}
    assert numeric_prose_supported("Revenue was USD 4.2 billion in 2021, versus USD 100 in 2020.",
                                   [current, prior], fiscal_year=2021)
    assert not numeric_prose_supported("Revenue was USD 100 in 2021, versus USD 4.2 billion in 2020.",
                                       [current, prior], fiscal_year=2021)
    assert not numeric_prose_supported("Revenue was USD 100.", [current, prior], fiscal_year=2021)


def test_source_bound_non_december_fiscal_dates_are_not_calendar_year_amounts():
    source = {**fact(120), "start_date": "2020-04-01", "end_date": "2021-03-31"}
    assert numeric_prose_supported("Revenue for 2020-04-01 to 2021-03-31 was USD 120.",
                                   [source], fiscal_year=2021)
