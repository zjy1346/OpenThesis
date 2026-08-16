from __future__ import annotations

import unittest

from openthesis.demo import demo_facts
from openthesis.financials import (
    calculate_metrics,
    calculate_interim_metrics,
    discounted_cash_flow_value,
    implied_fcf_growth,
    reverse_dcf_analysis,
)


class FinancialMetricTests(unittest.TestCase):
    def test_annual_and_interim_periods_are_never_mixed(self) -> None:
        def fact(year: int, period: str, concept: str, value: float, filed_at: str) -> dict[str, object]:
            return {
                "fact_id": f"{year}-{period}-{concept}",
                "company_cik": "fixture",
                "concept": concept,
                "reported_concept": concept,
                "value": value,
                "unit": "CNY",
                "fiscal_year": year,
                "fiscal_period": period,
                "form_type": "ANNUAL_REPORT" if period == "FY" else "QUARTERLY_REPORT",
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-{'12-31' if period == 'FY' else '03-31'}",
                "filed_at": filed_at,
                "accession_number": f"{year}-{period}",
                "source_url": "https://example.test/report.pdf",
                "scope": "consolidated",
            }

        facts = [
            fact(2025, "FY", "revenue", 100.0, "2026-03-27"),
            fact(2025, "FY", "net_income", 10.0, "2026-03-27"),
            fact(2025, "Q1", "revenue", 25.0, "2025-05-15"),
            fact(2025, "Q1", "net_income", 2.0, "2025-05-15"),
            fact(2026, "Q1", "revenue", 30.0, "2026-05-15"),
            fact(2026, "Q1", "net_income", 3.0, "2026-05-15"),
        ]

        annual = calculate_metrics(facts)
        interim = calculate_interim_metrics(facts)

        self.assertEqual([(item["year"], item["revenue"]) for item in annual], [(2025, 100.0)])
        self.assertEqual((interim[0]["year"], interim[0]["period"]), (2026, "Q1"))
        self.assertAlmostEqual(interim[0]["revenue_growth"], 0.2)
        self.assertEqual(interim[0]["comparison_period"], "2025 Q1")

    def test_missing_prior_interim_period_has_an_explainable_gap(self) -> None:
        facts = [
            {
                "fact_id": "2026-Q1-revenue",
                "company_cik": "fixture",
                "concept": "revenue",
                "reported_concept": "revenue",
                "value": 150_225_314_000.0,
                "unit": "CNY",
                "fiscal_year": 2026,
                "fiscal_period": "Q1",
                "form_type": "QUARTERLY_REPORT",
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
                "filed_at": "2026-04-28",
                "accession_number": "q1-26",
                "source_url": "https://example.test/q1-26.pdf",
                "scope": "consolidated",
            }
        ]

        interim = calculate_interim_metrics(facts)

        self.assertIsNone(interim[0]["revenue_growth"])
        self.assertIsNone(interim[0]["comparison_period"])
        self.assertEqual(interim[0]["comparison_gap"], "prior_period_unavailable")

    def test_demo_metrics_are_ordered_and_calculated(self) -> None:
        metrics = calculate_metrics(demo_facts())
        self.assertEqual(metrics[0]["year"], 2025)
        self.assertEqual(metrics[-1]["year"], 2021)
        self.assertAlmostEqual(
            metrics[0]["free_cash_flow"],
            300_000_000,
        )
        self.assertGreater(metrics[0]["revenue_growth"], 0)
        self.assertLess(metrics[0]["cash_conversion"], metrics[1]["cash_conversion"])

    def test_reverse_dcf_recovers_growth_assumption(self) -> None:
        base_fcf = 100.0
        expected_growth = 0.12
        value = discounted_cash_flow_value(base_fcf, expected_growth, 0.10, 0.03)
        implied = implied_fcf_growth(value, base_fcf, 0.10, 0.03)
        self.assertIsNotNone(implied)
        self.assertAlmostEqual(implied or 0, expected_growth, places=6)

        metrics = calculate_metrics(demo_facts())
        analysis = reverse_dcf_analysis(metrics, 8_000_000_000)
        self.assertIn(analysis["status"], {"ok", "outside_search_range"})
        self.assertEqual(len(analysis["sensitivity"]), 7)


if __name__ == "__main__":
    unittest.main()
