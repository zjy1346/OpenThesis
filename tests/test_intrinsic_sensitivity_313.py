from openthesis.financials import fcf_sensitivity_analysis
from openthesis.valuation_claims import enforce_valuation_consistency, intrinsic_sensitivity_is_usable


def metric(value=100):
    return {"fiscal_year": 2025, "fiscal_period": "FY", "end_date": "2025-12-31", "period_end": "2025-12-31",
        "free_cash_flow": value, "free_cash_flow_money": {"value": value, "currency": "USD", "unit_scale": 1.0,
        "unit_provenance": "normalized", "source_fact_ids": ["cfo", "capex"], "evidence_ids": ["fact:cfo", "fact:capex"],
        "formula": "operating_cash_flow - capital_expenditure"}}


def test_sensitivity_uses_positive_verified_annual_cash_flow_without_market_price():
    result = fcf_sensitivity_analysis([metric()], currency="USD")
    assert result["status"] == "ok"
    assert intrinsic_sensitivity_is_usable(result)
    assert len(result["sensitivity"]) == 7
    assert "market_cap" not in result and "implied_fcf_growth" not in result
    assert "share_count" not in result
    report, _ = enforce_valuation_consistency({"valuation_and_sensitivity": {"analysis": "Cash-flow sensitivity remains conditional."}},
        {"status": "missing_market_price", "intrinsic_analysis": result}, "en")
    assert report["valuation_and_sensitivity"]["sensitivity"] == result["sensitivity"]
    assert "implied_growth" not in report["implied_expectations"]
    assert "conditional" in " ".join(report["valuation_and_sensitivity"]["limitations"]).lower()


def test_negative_fcf_does_not_use_an_older_positive_year_or_fabricate_value():
    old = {**metric(), "fiscal_year": 2024, "end_date": "2024-12-31", "period_end": "2024-12-31"}
    result = fcf_sensitivity_analysis([old, metric(-20)], currency="USD")
    assert result["reason"] == "non_positive_annual_cash_flow"
    assert "sensitivity" not in result
    report, _ = enforce_valuation_consistency({"valuation_and_sensitivity": {"analysis": "Expansion currently consumes cash."}},
        {"status": "missing_market_price", "intrinsic_analysis": result}, "en")
    section = report["valuation_and_sensitivity"]
    assert section["analysis"] == "Expansion currently consumes cash."
    assert "not positive" in " ".join(section["limitations"])
    assert "sensitivity" not in section


def test_wrong_currency_missing_normalization_and_forged_grid_stay_unusable():
    assert fcf_sensitivity_analysis([metric()], currency="CNY")["status"] == "valuation_unit_mismatch"
    raw = metric()
    del raw["free_cash_flow_money"]
    assert fcf_sensitivity_analysis([raw], currency="USD")["status"] == "insufficient_data"
    result = fcf_sensitivity_analysis([metric()], currency="USD")
    result["sensitivity"][0]["equity_value"] *= 10
    assert not intrinsic_sensitivity_is_usable(result)


def test_foreign_period_scalar_conflicts_and_partial_grids_are_not_admitted():
    raw = metric()
    raw["free_cash_flow_money"]["period_end"] = "2024-12-31"
    assert fcf_sensitivity_analysis([raw], currency="USD")["reason"] == "cash_flow_identity_conflict"
    raw = metric()
    raw["free_cash_flow"] = 999
    assert fcf_sensitivity_analysis([raw], currency="USD")["reason"] == "cash_flow_identity_conflict"
    result = fcf_sensitivity_analysis([metric()], currency="USD")
    result["sensitivity"] = result["sensitivity"][:1]
    assert not intrinsic_sensitivity_is_usable(result)
