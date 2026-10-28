from __future__ import annotations

import unittest

from openthesis.valuation_claims import enforce_valuation_consistency


class ValuationClaimConsistencyTests(unittest.TestCase):
    def test_valid_valuation_analysis_and_source_links_survive_numeric_projection(self) -> None:
        value = {"status": "ok", "implied_fcf_growth": 0.05,
                 "equity_market_value": 1000.0, "base_free_cash_flow": 100.0,
                 "currency": "USD", "discount_rate": 0.1,
                 "sensitivity": [{"fcf_growth": 0.03, "equity_value": 800.0}]}
        report, _ = enforce_valuation_consistency({
            "implied_expectations": {"summary": "The price relies on sustained cash generation.",
                                     "evidence_ids": ["fact:fcf"], "implied_growth": 0.9},
            "valuation_and_sensitivity": {"analysis": "Growth sensitivity is greater at lower discount rates.",
                                          "evidence_ids": ["fact:fcf"],
                                          "sensitivity": [{"fcf_growth": 0.9, "equity_value": 99999.0}]},
        }, value, "en")
        self.assertIn("sustained cash generation", report["implied_expectations"]["analysis"])
        self.assertEqual(report["implied_expectations"]["evidence_ids"], ["fact:fcf"])
        self.assertEqual(report["implied_expectations"]["implied_growth"], 0.05)
        self.assertEqual(report["valuation_and_sensitivity"]["sensitivity"], value["sensitivity"])
        self.assertIn("greater at lower discount rates", report["valuation_and_sensitivity"]["analysis"])

    def test_engine_notice_does_not_supply_missing_model_analysis(self) -> None:
        report, _ = enforce_valuation_consistency({"implied_expectations": {"evidence_ids": ["fact:fcf"]}}, None)
        self.assertEqual(report["implied_expectations"]["role"], "notice")

    def test_unquantified_dcf_limitations_and_method_notes_survive_all_locales(self) -> None:
        cases = {
            "zh-CN": (
                "反向 DCF 估值模型未计算。",
                "反向 DCF 需要同币种市场权益价值和正的全年自由现金流输入。",
            ),
            "zh-Hant": (
                "反向 DCF 估值模型未執行。",
                "反向 DCF 需要同幣別市場權益價值與正的全年自由現金流輸入。",
            ),
            "en": (
                "The reverse DCF model was not run.",
                "A reverse DCF requires same-currency market equity value and positive full-year free cash flow inputs.",
            ),
        }

        for language, (limitation, method_note) in cases.items():
            with self.subTest(language=language):
                report, audit = enforce_valuation_consistency(
                    {"implied_expectations": {"analysis": [limitation, method_note]}},
                    None,
                    language,
                )

                self.assertEqual(report["implied_expectations"].get("analysis"), [limitation, method_note])
                self.assertEqual(audit["quarantined_count"], 0)

    def test_unverified_margin_of_safety_is_removed_when_no_deterministic_valuation_exists(self) -> None:
        report, audit = enforce_valuation_consistency(
            {"thesis": {"analysis": ["The company has a wide margin of safety.",
                                      "Durable cash flows remain stable."]}},
            None,
            "en",
        )

        self.assertEqual(report["thesis"]["analysis"], ["Durable cash flows remain stable."])
        self.assertEqual(audit["quarantined_paths"], ["thesis.analysis[0]"])
        self.assertGreater(audit["quarantined_count"], 0)
