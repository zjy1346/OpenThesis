from __future__ import annotations

import pytest

from openthesis.demo import demo_facts
from openthesis.financials import (
    calculate_interim_metrics,
    calculate_metrics,
    deterministic_summary,
    read_metric_period,
    reverse_dcf_analysis,
)
from openthesis.report_document import assemble_report_document, render_markdown
from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run


def test_demo_annual_metrics_round_trip_through_period_identity() -> None:
    metrics = calculate_metrics(demo_facts())
    metric = metrics[0]

    assert metric["year"] == 2025
    assert metric["period"] == "FY"
    identity = read_metric_period(metric)

    assert identity.serialize() == {
        "year": 2025,
        "period": "FY",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
    }


def test_legacy_fiscal_year_is_read_only_when_year_is_absent() -> None:
    identity = read_metric_period(
        {
            "fiscal_year": 2034,
            "period": " H1 ",
            "start_date": "2034-01-01",
            "end_date": "2034-06-30",
        }
    )

    assert identity.serialize() == {
        "year": 2034,
        "period": "H1",
        "period_start": "2034-01-01",
        "period_end": "2034-06-30",
    }


def test_conflicting_year_aliases_fail_with_stable_diagnostic() -> None:
    with pytest.raises(ValueError, match="METRIC_PERIOD_YEAR_CONFLICT"):
        read_metric_period({"year": 2034, "fiscal_year": 2033, "period": "FY"})


@pytest.mark.parametrize("year", [True, False, "2034", 0, -1, 10000, 2034.0])
def test_invalid_metric_years_are_rejected_without_coercion(year: object) -> None:
    with pytest.raises(ValueError, match="METRIC_PERIOD_YEAR_INVALID"):
        read_metric_period({"year": year, "fiscal_year": 2034, "period": "FY"})


def test_missing_year_does_not_become_zero_or_use_invalid_alias() -> None:
    with pytest.raises(ValueError, match="METRIC_PERIOD_YEAR_MISSING"):
        read_metric_period({"period": "FY"})
    with pytest.raises(ValueError, match="METRIC_PERIOD_YEAR_INVALID"):
        read_metric_period({"year": None, "fiscal_year": 2034, "period": "FY"})


def test_annual_period_aliases_normalize_to_fy_and_interim_periods_survive() -> None:
    for period in ("FY", "CY", "ANNUAL"):
        assert read_metric_period({"year": 2034, "period": period}).period == "FY"

    interim_metrics = calculate_interim_metrics(
        [
            {
                "fact_id": "q1-2035",
                "concept": "revenue",
                "value": 100.0,
                "fiscal_year": 2035,
                "fiscal_period": "Q1",
                "start_date": "2035-01-01",
                "end_date": "2035-03-31",
                "filed_at": "2035-04-30",
            },
            {
                "fact_id": "h1-2035",
                "concept": "revenue",
                "value": 210.0,
                "fiscal_year": 2035,
                "fiscal_period": "H1",
                "start_date": "2035-01-01",
                "end_date": "2035-06-30",
                "filed_at": "2035-08-01",
            },
        ]
    )

    identities = {
        identity.period: identity
        for identity in map(read_metric_period, interim_metrics)
    }
    assert identities["Q1"].period_start == "2035-01-01"
    assert identities["Q1"].period_end == "2035-03-31"
    assert identities["H1"].period_start == "2035-01-01"
    assert identities["H1"].period_end == "2035-06-30"


def test_non_calendar_annual_dates_survive_metric_production_and_reading() -> None:
    facts = [
        {
            "fact_id": f"{concept}-fy2035",
            "concept": concept,
            "value": value,
            "fiscal_year": 2035,
            "fiscal_period": "FY",
            "start_date": "2034-07-01",
            "end_date": "2035-06-30",
            "filed_at": "2035-08-15",
        }
        for concept, value in (("revenue", 100.0), ("net_income", 20.0))
    ]

    metric = calculate_metrics(facts)[0]
    identity = read_metric_period(metric)

    assert identity.serialize() == {
        "year": 2035,
        "period": "FY",
        "period_start": "2034-07-01",
        "period_end": "2035-06-30",
    }


def test_report_document_renders_legacy_fiscal_year_metric() -> None:
    artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "content": {
                "currency": "USD",
                "metrics": [{"fiscal_year": 2035, "revenue": 125.0}],
            },
        }]
    document = assemble_report_document("legacy-metric-report", artifacts, language="en")

    financial = next(section for section in document.sections if section.section_id == "financial-overview")

    assert "2035" in financial.body
    assert "$125" in financial.body
    assert "2035" in render_research_run("legacy-metric-report", artifacts, language="en")
    assert "2035" in render_research_html("legacy-metric-report", artifacts, language="en")


def test_deterministic_summary_uses_the_same_legacy_period_adapter() -> None:
    summary = deterministic_summary(
        "Example Co.",
        [{"fiscal_year": 2035, "revenue": 125.0}],
        language="en",
        currency="USD",
    )

    assert "| 2035 | $125 |" in summary


def test_deterministic_summary_diagnoses_and_skips_conflicting_years() -> None:
    summary = deterministic_summary(
        "Example Co.",
        [{"year": 2035, "fiscal_year": 2034, "revenue": 125.0}],
        language="en",
        currency="USD",
    )

    assert "METRIC_PERIOD_YEAR_CONFLICT" in summary
    assert "$125" not in summary


def test_report_document_skips_conflicting_metric_and_exposes_diagnostic() -> None:
    document = assemble_report_document(
        "conflicting-metric-report",
        [{
            "artifact_type": "deterministic-financial-summary",
            "content": {
                "currency": "USD",
                "metrics": [{"year": 2035, "fiscal_year": 2034, "revenue": 125.0}],
            },
        }],
        language="en",
    )

    rendered = render_markdown(document)

    assert "METRIC_PERIOD_YEAR_CONFLICT" in rendered
    assert "$125" not in rendered


def test_valuation_reads_legacy_year_and_never_sorts_missing_year_as_zero() -> None:
    result = reverse_dcf_analysis(
        [
            {
                "fiscal_year": 2034,
                "period": "FY",
                "filed_at": "2036-03-01",
                "period_start": "2033-07-01",
                "period_end": "2034-06-30",
                "free_cash_flow": 100.0,
            },
            {
                "fiscal_year": 2035,
                "period": "FY",
                "filed_at": "2036-03-01",
                "period_start": "2034-07-01",
                "period_end": "2035-06-30",
                "free_cash_flow": 200.0,
            },
        ],
        1000.0,
        currency="USD",
    )

    assert result["base_free_cash_flow"] == 200.0
    assert result["base_fcf_period"] == "2035 FY"


def test_valuation_reports_metric_period_conflict_as_invalid_parameters() -> None:
    result = reverse_dcf_analysis(
        [{
            "year": 2035,
            "fiscal_year": 2034,
            "period": "FY",
            "free_cash_flow": 200.0,
        }],
        1000.0,
        currency="USD",
    )

    assert result["status"] == "invalid_parameters"
    assert result["reason"] == "METRIC_PERIOD_YEAR_CONFLICT"
