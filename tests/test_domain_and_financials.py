from __future__ import annotations

import unittest

from openthesis.demo import demo_facts
from openthesis.financials import (
    calculate_metrics,
    discounted_cash_flow_value,
    implied_fcf_growth,
    reverse_dcf_analysis,
)


class FinancialMetricTests(unittest.TestCase):
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
