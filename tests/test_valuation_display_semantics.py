from __future__ import annotations

import unittest

from openthesis.report_html import _render_generic
from openthesis.report_projection import project_report_value
from openthesis.reporting import _render_value
from openthesis.valuation_claims import enforce_valuation_consistency


class ValuationDisplaySemanticsTests(unittest.TestCase):
    def projected(self, currency="HKD"):
        valuation = {"status": "ok", "implied_fcf_growth": 0.05,
            "equity_market_value": 1000.0, "base_free_cash_flow": 100.0,
            "currency": currency, "discount_rate": 0.1, "terminal_growth": 0.03,
            "horizon_years": 5, "sensitivity": [{"fcf_growth": 0.03, "equity_value": 800.0}]}
        report, _ = enforce_valuation_consistency({
            "implied_expectations": {"analysis": "The price assumes recurring cash flows.", "equity_market_value": 99999},
            "valuation_and_sensitivity": {"analysis": "The value depends on growth and discount assumptions.", "currency": "USD"},
        }, valuation, "en")
        return project_report_value(report, include_technical=False)

    def test_projection_retains_engine_matrix_rates_values_and_currency(self):
        report = self.projected()
        self.assertEqual(report["implied_expectations"]["equity_market_value"], 1000)
        self.assertEqual(report["valuation_and_sensitivity"]["currency"], "HKD")
        self.assertEqual(report["valuation_and_sensitivity"]["sensitivity"], [{"fcf_growth": 0.03, "equity_value": 800}])

    def test_matrix_uses_same_percentage_and_money_format_in_three_languages_and_formats(self):
        section = self.projected()["valuation_and_sensitivity"]
        expected_labels = {"en": "FCF Growth", "zh-CN": "自由现金流增速", "zh-Hant": "自由現金流增速"}
        for language, label in expected_labels.items():
            for rendered in ("\n".join(_render_value(section, language)), _render_generic(section, language)):
                with self.subTest(language=language, rendered=rendered):
                    self.assertIn(label, rendered)
                    self.assertIn("3%", rendered)
                    self.assertIn("HK$800", rendered)
                    self.assertNotIn("0.03", rendered)
                    self.assertNotIn("fcf_growth", rendered)

    def test_missing_currency_does_not_infer_usd_or_listing_currency(self):
        section = {"sensitivity": [{"fcf_growth": 0.03, "equity_value": 800}]}
        for rendered in ("\n".join(_render_value(section, "en")), _render_generic(section, "en")):
            self.assertIn("800", rendered)
            self.assertNotIn("$", rendered)
            self.assertNotIn("¥", rendered)

    def test_nested_explicit_currency_overrides_parent_only_for_its_own_row(self):
        section = {"currency": "HKD", "sensitivity": [
            {"fcf_growth": 0.03, "equity_value": 800},
            {"currency": "CNY", "fcf_growth": 0.04, "equity_value": 900},
        ]}
        for rendered in ("\n".join(_render_value(section, "en")), _render_generic(section, "en")):
            self.assertIn("HK$800", rendered)
            self.assertIn("¥900", rendered)

    def test_rates_are_never_interpreted_as_monetary_amounts(self):
        section = {"currency": "CNY", "discount_rate": 0.1, "terminal_growth": 0.03, "implied_fcf_growth": 0.05}
        for rendered in ("\n".join(_render_value(section, "en")), _render_generic(section, "en")):
            self.assertIn("10%", rendered)
            self.assertIn("3%", rendered)
            self.assertIn("5%", rendered)
            self.assertNotIn("¥0", rendered)
