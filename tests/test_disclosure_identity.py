from __future__ import annotations

import unittest

from openthesis.disclosure_identity import DisclosureIdentityResolver
from openthesis.domain import FinancialFact


class DisclosureIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = DisclosureIdentityResolver()

    def test_half_year_title_cannot_become_future_fy(self) -> None:
        result = self.resolver.resolve(
            title="宁德时代 2024年半年度报告",
            filed_at="2024-08-30",
            provider_metadata={"period_end": "2024-12-31", "fiscal_period": "FY"},
            observed_statement_dates=("2024-06-30",),
        )
        self.assertEqual(result.fiscal_period, "H1")
        self.assertEqual(result.period_end, "2024-06-30")
        self.assertNotEqual(result.period_end, "2024-12-31")
        self.assertFalse(result.provisional)

    def test_q3_title_does_not_use_provider_calendar_year_end(self) -> None:
        result = self.resolver.resolve(
            title="中芯国际 2024年第三季度报告",
            filed_at="2024-10-31",
            provider_metadata={"period_end": "2024-12-31"},
            observed_statement_dates=("2024-09-30",),
        )
        self.assertEqual(result.fiscal_period, "Q3")
        self.assertEqual(result.period_end, "2024-09-30")

    def test_march_year_end_is_preserved_for_alibaba_fy(self) -> None:
        result = self.resolver.resolve(
            title="Alibaba Annual Report for the year ended March 31, 2025",
            filed_at="2025-05-20",
            provider_metadata={"period_end": "2025-03-31", "fiscal_period": "FY"},
            observed_statement_dates=(),
        )
        self.assertEqual(result.fiscal_period, "FY")
        self.assertEqual(result.period_end, "2025-03-31")

    def test_unverified_calendar_year_end_is_provisional_not_fake_exact_date(self) -> None:
        result = self.resolver.resolve(
            title="2025年度报告",
            filed_at="2026-04-01",
            provider_metadata={"period_end": "2025-12-31", "fiscal_period": "FY"},
            observed_statement_dates=(),
        )
        self.assertIsNone(result.period_end)
        self.assertTrue(result.provisional)
        self.assertIn("period_end_provisional", result.diagnostics)

    def test_title_year_wins_over_audit_and_comparison_dates(self) -> None:
        result = self.resolver.resolve(
            title="2025 Annual Report",
            filed_at="2026-04-01",
            provider_metadata={"fiscal_period": "FY"},
            observed_statement_dates=("2026-03-20", "2025-03-31", "2024-03-31"),
        )
        self.assertEqual(result.period_end, "2025-03-31")
        self.assertFalse(result.provisional)

    def test_title_year_without_same_year_observation_is_provisional(self) -> None:
        result = self.resolver.resolve(
            title="2025年半年度报告",
            filed_at="2025-08-20",
            provider_metadata={"period_end": "2025-12-31", "fiscal_period": "FY"},
            observed_statement_dates=("2024-06-30", "2023-06-30"),
        )
        self.assertIsNone(result.period_end)
        self.assertTrue(result.provisional)

    def test_financial_fact_has_orthogonal_status_defaults(self) -> None:
        fact = FinancialFact(
            "fact-1", "cik", "revenue", "Revenue", 1.0, "USD", 2025, "FY",
            "10-K", "2025-01-01", "2025-12-31", "2026-02-01", "acc", "url",
        )
        self.assertEqual(fact.extraction_status, "unresolved")
        self.assertEqual(fact.usage_status, "audit_only")
        self.assertEqual(fact.provenance_status, "unresolved")
        self.assertEqual(fact.derived_version, "financial-facts-v2")


if __name__ == "__main__":
    unittest.main()
