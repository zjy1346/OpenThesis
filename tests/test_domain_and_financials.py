from __future__ import annotations

import unittest
import openthesis.financials as financials_module

from openthesis.demo import demo_facts
from openthesis.financials import (
    NormalizedMoney,
    calculate_metrics,
    calculate_interim_metrics,
    discounted_cash_flow_value,
    implied_fcf_growth,
    reverse_dcf_analysis,
    reverse_dcf_status_text,
    deterministic_summary,
    growth_rate,
)


class FinancialMetricTests(unittest.TestCase):
    def test_gross_profit_is_derived_from_same_period_revenue_and_cost(self) -> None:
        metrics = calculate_metrics(
            [
                {
                    "concept": "revenue", "value": 120.0, "fiscal_year": 2025,
                    "fiscal_period": "FY", "filed_at": "2026-03-01",
                    "end_date": "2025-12-31", "usage_status": "canonical_research",
                },
                {
                    "concept": "cost_of_revenue", "value": 72.0, "fiscal_year": 2025,
                    "fiscal_period": "FY", "filed_at": "2026-03-01",
                    "end_date": "2025-12-31", "usage_status": "canonical_research",
                },
            ]
        )[0]
        self.assertEqual(metrics["gross_margin"], 0.4)
        self.assertEqual(metrics["gross_profit_basis"], "revenue_minus_cost_of_revenue")
    def test_growth_rate_does_not_emit_misleading_percentages_across_loss_boundaries(self) -> None:
        self.assertIsNone(growth_rate(-50.0, 100.0))
        self.assertIsNone(growth_rate(50.0, -100.0))
        self.assertIsNone(growth_rate(-50.0, -100.0))
        self.assertIsNone(growth_rate(-150.0, -100.0))
        self.assertIsNone(growth_rate(50.0, 0.0))
        self.assertAlmostEqual(growth_rate(120.0, 100.0) or 0.0, 0.2)

    def test_metric_growth_statuses_and_extended_growth_metrics_are_explicit(self) -> None:
        facts = []
        values = {
            2024: {"revenue": 100.0, "gross_profit": 40.0, "operating_income": -10.0, "net_income": -20.0, "operating_cash_flow": -30.0, "equity": 50.0},
            2025: {"revenue": 120.0, "gross_profit": 54.0, "operating_income": 5.0, "net_income": -10.0, "operating_cash_flow": 15.0, "equity": -20.0},
        }
        for year, concepts in values.items():
            for concept, value in concepts.items():
                facts.append({
                    "fact_id": f"{year}-{concept}", "concept": concept, "value": value,
                    "fiscal_year": year, "fiscal_period": "FY", "filed_at": f"{year + 1}-03-01",
                })

        latest = calculate_metrics(facts)[0]

        self.assertAlmostEqual(latest["gross_margin"], 0.45)
        self.assertAlmostEqual(latest["revenue_growth"], 0.2)
        self.assertEqual(latest["revenue_growth_status"], "rate")
        self.assertIsNone(latest["operating_income_growth"])
        self.assertEqual(latest["operating_income_growth_status"], "turnaround")
        self.assertIsNone(latest["net_income_growth"])
        self.assertEqual(latest["net_income_growth_status"], "loss_narrowed")
        self.assertIsNone(latest["operating_cash_flow_growth"])
        self.assertEqual(latest["operating_cash_flow_growth_status"], "turnaround")
        self.assertIsNone(latest["return_on_equity"])
        self.assertEqual(latest["return_on_equity_gap"], "non_positive_equity")

    def test_all_same_filing_comparators_drive_extended_growth_metrics(self) -> None:
        facts = []
        for concept, value in {
            "revenue": 120.0,
            "operating_income": 12.0,
            "net_income": 6.0,
            "operating_cash_flow": 18.0,
        }.items():
            facts.append({
                "fact_id": f"current-{concept}", "concept": concept, "value": value,
                "fiscal_year": 2025, "fiscal_period": "FY", "filed_at": "2026-03-01",
                "usage_status": "canonical_research",
            })
            facts.append({
                "fact_id": f"standalone-{concept}", "concept": concept, "value": value / 3,
                "fiscal_year": 2024, "fiscal_period": "FY", "filed_at": "2025-03-01",
                "usage_status": "canonical_research",
            })
            facts.append({
                "fact_id": f"comparator-{concept}", "concept": concept, "value": value / 2,
                "fiscal_year": 2024, "fiscal_period": "FY", "filed_at": "2026-03-01",
                "usage_status": "comparator",
            })

        latest = calculate_metrics(facts)[0]

        self.assertAlmostEqual(latest["revenue_growth"], 1.0)
        self.assertAlmostEqual(latest["operating_income_growth"], 1.0)
        self.assertAlmostEqual(latest["net_income_growth"], 1.0)
        self.assertAlmostEqual(latest["operating_cash_flow_growth"], 1.0)
        self.assertCountEqual(
            latest["comparison_fact_ids"],
            [
                "comparator-revenue", "comparator-operating_income",
                "comparator-net_income", "comparator-operating_cash_flow",
            ],
        )

    def test_summary_renders_loss_transition_and_non_positive_equity_reason(self) -> None:
        summary = deterministic_summary(
            "示例",
            [{
                "year": 2025, "revenue": 10.0, "revenue_growth": 0.1,
                "net_income": 1.0, "net_income_growth": None,
                "net_income_growth_status": "turnaround",
                "operating_cash_flow": 2.0, "return_on_equity": None,
                "return_on_equity_gap": "non_positive_equity",
            }],
            "zh-CN", "CNY",
        )
        self.assertIn("扭亏为盈", summary)
        self.assertIn("权益为零或负数，不适用", summary)

    def test_reverse_dcf_invalid_parameters_return_a_structured_result(self) -> None:
        metrics = [{"year": 2025, "period": "FY", "free_cash_flow": 100.0}]
        for kwargs in (
            {"discount_rate": 0.03, "terminal_growth": 0.03},
            {"discount_rate": float("nan")},
            {"terminal_growth": float("inf")},
            {"horizon_years": 0},
        ):
            with self.subTest(kwargs=kwargs):
                result = reverse_dcf_analysis(metrics, 1_000.0, **kwargs)
                self.assertEqual(result["status"], "invalid_parameters")
                self.assertTrue(result["invalid_fields"])

    def test_traditional_deterministic_summary_uses_traditional_labels(self) -> None:
        summary = deterministic_summary("示例", [{"year": 2025, "revenue": 10.0, "revenue_growth": None, "net_income": 1.0, "operating_cash_flow": 2.0}], "zh-Hant", "CNY")
        self.assertIn("\u71df\u696d\u6536\u5165", summary)
        self.assertIn("\u6de8\u5229\u6f64", summary)

    def test_simplified_deterministic_summary_uses_exact_simplified_headers(self) -> None:
        summary = deterministic_summary("示例", [{"year": 2025, "revenue": 10.0, "revenue_growth": 0.1, "net_income": 1.0, "operating_cash_flow": 2.0}], "zh-CN", "CNY")
        self.assertIn("财年", summary)
        self.assertIn("收入增长", summary)

    def test_deterministic_summary_explains_missing_roe_input(self) -> None:
        summary = deterministic_summary(
            "示例",
            [{"year": 2025, "revenue": 10.0, "net_income": 1.0,
              "operating_cash_flow": 2.0, "return_on_equity": None,
              "return_on_equity_gap": "missing_equity"}],
            "zh-CN",
            "CNY",
        )
        self.assertIn("净资产收益率：—（缺少权益数据）", summary)
        self.assertNotIn("財年", summary)
        self.assertNotIn("镾", summary)

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

    def test_annual_growth_never_crosses_a_missing_fiscal_year(self) -> None:
        facts = []
        for year, revenue in ((2026, 160.0), (2024, 120.0), (2023, 100.0), (2022, 80.0)):
            facts.append(
                {
                    "fact_id": f"fy-{year}",
                    "concept": "revenue",
                    "value": revenue,
                    "fiscal_year": year,
                    "fiscal_period": "FY",
                    "filed_at": f"{year + 1}-03-01",
                }
            )
        metrics = calculate_metrics(facts)
        by_year = {item["year"]: item for item in metrics}
        self.assertIsNone(by_year[2026]["revenue_growth"])
        self.assertIsNone(by_year[2026]["comparison_year"])
        self.assertEqual(by_year[2026]["comparison_gap"], "missing_2025")
        self.assertEqual(by_year[2024]["comparison_year"], 2023)
        self.assertIsNone(by_year[2024]["comparison_gap"])
        self.assertAlmostEqual(by_year[2024]["revenue_growth"], 0.2)
        self.assertEqual(by_year[2023]["comparison_year"], 2022)

    def test_roe_accepts_total_equity_and_prefers_average_equity(self) -> None:
        facts = []
        for year, concept, value in (
            (2025, "net_income", 30.0),
            (2025, "total_equity", 220.0),
            (2024, "total_equity", 180.0),
        ):
            facts.append(
                {
                    "fact_id": f"{year}-{concept}",
                    "concept": concept,
                    "value": value,
                    "fiscal_year": year,
                    "fiscal_period": "FY",
                    "filed_at": f"{year + 1}-03-01",
                }
            )

        latest = calculate_metrics(facts)[0]

        self.assertAlmostEqual(latest["return_on_equity"], 0.15)
        self.assertEqual(latest["return_on_equity_basis"], "average_equity")
        self.assertIsNone(latest["return_on_equity_gap"])
        self.assertEqual(
            latest["return_on_equity_inputs"],
            {"net_income": 30.0, "opening_equity": 180.0, "closing_equity": 220.0},
        )

    def test_roe_missing_inputs_are_explained(self) -> None:
        latest = calculate_metrics(
            [{"fact_id": "2025-net", "concept": "net_income", "value": 30.0,
              "fiscal_year": 2025, "fiscal_period": "FY", "filed_at": "2026-03-01"}]
        )[0]

        self.assertIsNone(latest["return_on_equity"])
        self.assertEqual(latest["return_on_equity_gap"], "missing_equity")

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

    def test_reverse_dcf_requires_typed_normalized_money_boundary(self) -> None:
        """Table-unit inputs must not be compared to base-currency market value."""
        money = financials_module.NormalizedMoney
        market = money.from_normalized(9_500_000_000, "CNY")
        raw_table_metrics = [{
            "year": 2025,
            "period": "FY",
            "filed_at": "2026-03-01",
            "free_cash_flow": 380,
            "free_cash_flow_currency": "CNY",
            "free_cash_flow_unit_scale": 1_000_000,
            "free_cash_flow_unit_provenance": "declared",
        }]
        rejected = reverse_dcf_analysis(raw_table_metrics, market)
        self.assertEqual(rejected["status"], "valuation_unit_mismatch")

        normalized = [{
            **raw_table_metrics[0],
            "free_cash_flow_money": money.from_raw(380, "CNY", 1_000_000),
        }]
        accepted = reverse_dcf_analysis(normalized, market)
        self.assertEqual(accepted["status"], "ok")
        self.assertEqual(accepted["base_free_cash_flow"], 380_000_000)
        self.assertAlmostEqual(accepted["implied_fcf_growth"], 0.159291, places=4)

        mismatch = reverse_dcf_analysis(
            normalized, money.from_normalized(9_500_000_000, "USD")
        )
        self.assertEqual(mismatch["status"], "valuation_currency_mismatch")

    def test_interim_metrics_uses_same_filing_comparator_column(self) -> None:
        facts = [
            {
                "fact_id": "current-revenue", "concept": "revenue", "value": 120.0,
                "fiscal_year": 2025, "fiscal_period": "H1", "end_date": "2025-06-30",
                "filed_at": "2025-08-20", "accession_number": "current-filing",
                "source_document": "2025-h1.pdf", "source_page": 10,
                "source_column": "2025 H1 current",
            },
            {
                "fact_id": "same-filing-comparator-revenue", "concept": "revenue", "value": 100.0,
                "fiscal_year": 2024, "fiscal_period": "H1", "end_date": "2024-06-30",
                "filed_at": "2025-08-20", "accession_number": "current-filing",
                "source_document": "2025-h1.pdf", "source_page": 10,
                "source_column": "2024 H1 comparative",
                "usage_status": "comparator",
            },
        ]
        interim = calculate_interim_metrics(facts)
        current = next(item for item in interim if item["year"] == 2025)
        self.assertEqual(current["comparison_period"], "2024 H1")
        self.assertAlmostEqual(current["revenue_growth"], 0.2)

    def test_same_filing_comparator_has_explicit_metric_priority(self) -> None:
        facts = [
            {"concept": "revenue", "value": 150.0, "fiscal_year": 2025,
             "fiscal_period": "FY", "filed_at": "2026-03-01", "fact_id": "current"},
            {"concept": "revenue", "value": 100.0, "fiscal_year": 2024,
             "fiscal_period": "FY", "filed_at": "2025-03-01", "fact_id": "history"},
            {"concept": "revenue", "value": 120.0, "fiscal_year": 2024,
             "fiscal_period": "FY", "filed_at": "2026-03-01", "fact_id": "same-filing",
             "usage_status": "comparator"},
        ]
        metric = next(item for item in calculate_metrics(facts) if item["year"] == 2025)
        self.assertAlmostEqual(metric["revenue_growth"], 0.25)
        self.assertEqual(metric["comparison_source"], "same_filing_comparator")
        self.assertEqual(metric["comparison_basis"], "same_filing_comparator")
        self.assertEqual(metric["comparison_fact_ids"], ["same-filing"])
        self.assertTrue(metric["restatement_available"])

    def test_same_filing_interim_comparator_priority_is_not_filed_at_accidental(self) -> None:
        facts = [
            {"concept": "revenue", "value": 150.0, "fiscal_year": 2025,
             "fiscal_period": "H1", "end_date": "2025-06-30",
             "filed_at": "2026-08-01", "fact_id": "current"},
            {"concept": "revenue", "value": 100.0, "fiscal_year": 2024,
             "fiscal_period": "H1", "end_date": "2024-06-30",
             "filed_at": "2025-08-01", "fact_id": "history"},
            {"concept": "revenue", "value": 120.0, "fiscal_year": 2024,
             "fiscal_period": "H1", "end_date": "2024-06-30",
             "filed_at": "2026-08-01", "fact_id": "same-filing",
             "usage_status": "comparator"},
        ]
        metric = next(item for item in calculate_interim_metrics(facts) if item["year"] == 2025)
        self.assertAlmostEqual(metric["revenue_growth"], 0.25)
        self.assertEqual(metric["comparison_source"], "same_filing_comparator")
        self.assertEqual(metric["comparison_basis"], "same_filing_comparator")
        self.assertEqual(metric["comparison_fact_ids"], ["same-filing"])

    def test_same_filing_comparator_is_not_rendered_as_an_annual_row(self) -> None:
        facts = [
            {"concept": "revenue", "value": 150.0, "fiscal_year": 2025,
             "fiscal_period": "FY", "filed_at": "2026-03-01", "fact_id": "current"},
            {"concept": "revenue", "value": 120.0, "fiscal_year": 2024,
             "fiscal_period": "FY", "filed_at": "2026-03-01", "fact_id": "same-filing",
             "usage_status": "comparator"},
        ]
        metrics = calculate_metrics(facts)
        self.assertEqual([item["year"] for item in metrics], [2025])
        self.assertAlmostEqual(metrics[0]["revenue_growth"], 0.25)

    def test_same_filing_comparator_is_not_rendered_as_an_interim_row(self) -> None:
        facts = [
            {"concept": "revenue", "value": 150.0, "fiscal_year": 2025,
             "fiscal_period": "H1", "end_date": "2025-06-30", "filed_at": "2026-08-01",
             "fact_id": "current"},
            {"concept": "revenue", "value": 120.0, "fiscal_year": 2024,
             "fiscal_period": "H1", "end_date": "2024-06-30", "filed_at": "2026-08-01",
             "fact_id": "same-filing", "usage_status": "comparator"},
        ]
        metrics = calculate_interim_metrics(facts)
        self.assertEqual([(item["year"], item["period"]) for item in metrics], [(2025, "H1")])
        self.assertAlmostEqual(metrics[0]["revenue_growth"], 0.25)

    def test_reverse_dcf_unit_and_currency_statuses_are_localized(self) -> None:
        for status, simplified, traditional, english in (
            ("valuation_unit_mismatch", "金额单位", "金額單位", "monetary unit"),
            ("valuation_currency_mismatch", "币种", "幣別", "currencies"),
        ):
            self.assertIn(simplified, reverse_dcf_status_text(status, "zh-CN"))
            self.assertIn(traditional, reverse_dcf_status_text(status, "zh-Hant"))
            self.assertIn(english, reverse_dcf_status_text(status, "en"))

    def test_reverse_dcf_strict_requires_verified_normalized_fcf(self) -> None:
        market = NormalizedMoney.from_normalized(
            9_500_000_000, "CNY", source_id="quote:fixture", as_of="2026-01-01"
        )
        metrics = [{
            "year": 2025,
            "period": "FY",
            "free_cash_flow": 380_000_000,
            "free_cash_flow_currency": "CNY",
            "free_cash_flow_unit_scale": 1,
        }]
        result = reverse_dcf_analysis(metrics, market, require_typed=True)
        self.assertEqual(result["status"], "valuation_unit_mismatch")

    def test_reverse_dcf_strict_accepts_normalized_fcf_money(self) -> None:
        market = NormalizedMoney.from_normalized(9_500_000_000, "CNY")
        metrics = [{
            "year": 2025,
            "period": "FY",
            "free_cash_flow": 380_000_000,
            "free_cash_flow_money": {
                "value": 380_000_000,
                "currency": "CNY",
                "unit_scale": 1,
                "unit_provenance": "normalized",
            },
        }]
        result = reverse_dcf_analysis(metrics, market, require_typed=True)
        self.assertNotEqual(result["status"], "valuation_unit_mismatch")

    def test_reverse_dcf_strict_rejects_money_without_explicit_provenance(self) -> None:
        market = NormalizedMoney.from_normalized(9_500_000_000, "CNY")
        result = reverse_dcf_analysis(
            [{
                "year": 2025,
                "period": "FY",
                "free_cash_flow_money": {
                    "value": 380_000_000,
                    "currency": "CNY",
                    "unit_scale": 1,
                    "unit_provenance": "unknown",
                },
            }],
            market,
            require_typed=True,
        )
        self.assertEqual(result["status"], "valuation_unit_mismatch")

    def test_demo_facts_are_eligible_for_strict_dcf(self) -> None:
        metrics = calculate_metrics(demo_facts())
        result = reverse_dcf_analysis(
            metrics,
            NormalizedMoney.from_normalized(1_000_000_000, "USD"),
            require_typed=True,
        )
        self.assertNotEqual(result["status"], "valuation_unit_mismatch")


if __name__ == "__main__":
    unittest.main()
