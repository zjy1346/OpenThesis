from copy import deepcopy

import pytest

from openthesis.report_readiness import REQUIRED_REPORT_SECTIONS
from openthesis.research import ResearchWorkflow, _REQUIRED_SYNTHESIS_SECTIONS


@pytest.mark.parametrize("language", ["en", "zh-CN", "zh-Hant"])
def test_synthesis_failure_keeps_computed_valuation_without_inventing_analysis(language):
    # Exercise the pure fallback boundary without a model, network or database.
    workflow = ResearchWorkflow.__new__(ResearchWorkflow)
    workflow.report_language = language
    valuation = {
        "status": "ok", "implied_fcf_growth": 0.05,
        "equity_market_value": 1000.0, "base_free_cash_flow": 100.0,
        "currency": "CNY", "discount_rate": 0.1, "terminal_growth": 0.03,
        "horizon_years": 10, "market_as_of": "2026-10-04",
        "sensitivity": [{"fcf_growth": 0.03, "equity_value": 800.0}],
    }
    frozen = deepcopy(valuation)
    report = workflow._build_staged_fallback({}, {}, {}, {}, [], valuation)
    section = report["implied_expectations"]
    assert section["implied_growth"] == 0.05
    assert section["equity_market_value"] == 1000.0
    assert section["currency"] == "CNY"
    assert section["as_of"] == "2026-10-04"
    assert section["role"] == "notice"
    assert report["valuation_and_sensitivity"]["sensitivity"] == frozen["sensitivity"]
    assert report["valuation_and_sensitivity"]["role"] == "notice"
    assert not report.get("research_complete", False)
    assert not report["claims"]
    assert valuation == frozen


@pytest.mark.parametrize("language", ["en", "zh-CN", "zh-Hant"])
def test_fallback_without_computed_valuation_does_not_fill_values(language):
    workflow = ResearchWorkflow.__new__(ResearchWorkflow)
    workflow.report_language = language
    report = workflow._build_staged_fallback({}, {}, {}, {}, [], {"status": "currency_mismatch"})
    assert report["implied_expectations"]["role"] == "notice"
    assert "implied_growth" not in report["implied_expectations"]
    assert "sensitivity" not in report["valuation_and_sensitivity"]
    assert not report.get("research_complete", False)


def test_synthesis_repair_and_readiness_share_the_required_section_contract():
    assert _REQUIRED_SYNTHESIS_SECTIONS == frozenset(REQUIRED_REPORT_SECTIONS)
    assert "implied_expectations" in _REQUIRED_SYNTHESIS_SECTIONS
