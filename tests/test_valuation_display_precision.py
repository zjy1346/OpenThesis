import pytest

from openthesis.research_numeric_assertions import numeric_prose_supported
from openthesis.valuation_claims import enforce_valuation_consistency


@pytest.mark.parametrize("written", ["5.5", "6.0", "4.0"])
def test_nearby_implied_growth_is_not_a_valid_alternative_to_engine(written):
    valuation = {"status": "ok", "implied_fcf_growth": "0.05321",
                 "equity_market_value": 1000, "base_free_cash_flow": 100, "currency": "USD"}
    report, audit = enforce_valuation_consistency({
        "thesis": {"analysis": ["Supported cash generation.",
                                f"Current price implies {written}% annual FCF growth."]}
    }, valuation, "en")
    assert report["thesis"]["analysis"] == ["Supported cash generation."]
    assert audit["quarantined_paths"] == ["thesis.analysis[1]"]
    assert audit["quarantined_count"] > 0


@pytest.mark.parametrize("written", ["5", "5.3", "5.32", "5.321"])
def test_implied_growth_accepts_only_its_written_rounding(written):
    valuation = {"status": "ok", "implied_fcf_growth": "0.05321",
                 "equity_market_value": 1000, "base_free_cash_flow": 100, "currency": "USD"}
    report, audit = enforce_valuation_consistency({
        "thesis": f"Current price implies {written}% annual FCF growth."
    }, valuation, "en")
    assert f"{written}%" in report["thesis"]
    assert audit["quarantined_count"] == 0


def test_a_ratio_level_cannot_prove_a_percentage_point_change():
    record = {"kind": "financial_fact", "concept": "operating_margin", "unit": "ratio", "value": 1}
    assert not numeric_prose_supported("Margin changed by 1 percentage point.", [record])
    assert numeric_prose_supported("Margin was 100%.", [record])


def test_explicit_percentage_point_amount_keeps_its_own_kind():
    record = {"kind": "financial_fact", "concept": "operating_margin_change",
              "unit": "percentage_points", "value": 1}
    assert numeric_prose_supported("Margin changed by 1 percentage point.", [record])
    assert not numeric_prose_supported("Margin was 100%.", [record])
