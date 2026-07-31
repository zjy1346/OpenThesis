from __future__ import annotations

import unittest

from openthesis.growth import (
    evidence_grade_label,
    format_probability_range,
    normalize_growth_output,
    scenario_label,
)


class GrowthNormalizationTests(unittest.TestCase):
    def test_normalizes_valid_growth_opportunity(self) -> None:
        result = normalize_growth_output(
            {
                "opportunities": [
                    {
                        "title": "AI 基础设施",
                        "category": "产品扩张",
                        "mechanism": "数据中心需求扩大。",
                        "evidence_grade": "B",
                        "maturity_stage": "快速成长",
                        "time_horizon_years": 4,
                        "probability_range": [0.2, 0.4],
                        "supporting_evidence_ids": ["fact:revenue"],
                        "contradicting_evidence_ids": [],
                        "scenario_eligibility": ["base", "upside"],
                    }
                ]
            },
            {"fact:revenue"},
        )
        self.assertTrue(result.passed)
        opportunity = result.output["opportunities"][0]
        self.assertEqual(opportunity["evidence_grade"], "B")
        self.assertEqual(opportunity["probability_range"], [0.2, 0.4])
        self.assertEqual(format_probability_range([0.2, 0.4]), "20%–40%")
        self.assertEqual(evidence_grade_label("B", "en"), "B · Good evidence")
        self.assertEqual(scenario_label("upside"), "乐观情景")

    def test_flags_malformed_and_unknown_values_without_crashing(self) -> None:
        result = normalize_growth_output(
            {
                "opportunities": [
                    {
                        "title": "",
                        "mechanism": "",
                        "evidence_grade": "Z",
                        "time_horizon_years": 99,
                        "probability_range": [0.8, 0.2],
                        "supporting_evidence_ids": ["fact:missing"],
                        "scenario_eligibility": ["impossible"],
                    }
                ]
            },
            {"fact:known"},
        )
        self.assertFalse(result.passed)
        opportunity = result.output["opportunities"][0]
        self.assertEqual(opportunity["evidence_grade"], "U")
        self.assertEqual(opportunity["probability_range"], [])
        self.assertIsNone(opportunity["time_horizon_years"])
        self.assertEqual(opportunity["unknown_evidence_ids"], ["fact:missing"])
        self.assertEqual(format_probability_range([]), "证据不足")

    def test_limits_output_to_five_opportunities(self) -> None:
        source = {
            "opportunities": [
                {
                    "title": f"Opportunity {index}",
                    "mechanism": "Mechanism",
                    "evidence_grade": "C",
                    "time_horizon_years": 3,
                    "probability_range": [0.1, 0.3],
                }
                for index in range(7)
            ]
        }
        result = normalize_growth_output(source, set(), "en")
        self.assertEqual(len(result.output["opportunities"]), 5)
        self.assertTrue(any("first 5" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
