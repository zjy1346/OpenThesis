from __future__ import annotations

import unittest

from openthesis.report_html import render_message_html, render_research_html


def sample_artifacts() -> list[dict[str, object]]:
    return [
        {
            "artifact_type": "deterministic-financial-summary",
            "title": "确定性财务概览",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [
                    {
                        "year": 2025,
                        "revenue": 72_880_000_000,
                        "revenue_growth": 0.61,
                        "operating_income": 29_760_000_000,
                        "operating_margin": 0.373,
                        "net_income": 28_090_000_000,
                        "operating_cash_flow": 64_090_000_000,
                        "capital_expenditure": 10_000_000_000,
                        "free_cash_flow": 54_090_000_000,
                    }
                ],
                "evidence": [
                    {
                        "evidence_id": "fact:revenue",
                        "concept": "revenue",
                        "source_url": "https://www.sec.gov/example",
                    }
                ],
            },
        },
        {
            "artifact_type": "growth-opportunities",
            "title": "增长机会",
            "agent_id": "growth-opportunity-analyst",
            "model_id": "test:model",
            "content": {
                "opportunities": [
                    {
                        "title": "AI 加速计算基础设施",
                        "category": "产品与市场扩张",
                        "mechanism": "算力需求提升带动平台销售。",
                        "evidence_grade": "D",
                        "maturity_stage": "快速成长",
                        "time_horizon_years": 4,
                        "probability_range": [0.2, 0.4],
                        "supporting_evidence_ids": ["fact:revenue"],
                        "contradicting_evidence_ids": ["fact:risk"],
                        "scenario_eligibility": ["base", "upside"],
                        "leading_indicators": ["数据中心收入"],
                    }
                ]
            },
        },
        {
            "artifact_type": "research-report",
            "title": "完整长期研究报告",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {
                "report": {"executive_summary": "长期需求仍需持续验证。"},
                "verification": {
                    "passed": True,
                    "claim_count": 1,
                    "verified_claim_count": 1,
                    "unsupported_fact_count": 0,
                    "issues": [],
                },
            },
        },
    ]


class HtmlReportTests(unittest.TestCase):
    def test_renders_real_table_and_clean_growth_cards(self) -> None:
        report = render_research_html(
            "run-1",
            sample_artifacts(),
            company_name="示例公司",
        )
        self.assertIn('<table class="data-table">', report)
        self.assertIn("AI 加速计算基础设施", report)
        self.assertIn("20%–40%", report)
        self.assertIn("D · 主要为推论", report)
        self.assertNotIn("supporting_evidence_ids", report)
        self.assertNotIn("fact:risk", report)
        self.assertNotIn("- -", report)

    def test_technical_details_are_opt_in(self) -> None:
        report = render_research_html(
            "run-1",
            sample_artifacts(),
            include_technical=True,
        )
        self.assertIn("fact:revenue", report)
        self.assertIn("fact:risk", report)
        self.assertIn("技术详情", report)

    def test_english_and_html_escaping(self) -> None:
        artifacts = sample_artifacts()
        artifacts[1]["content"]["opportunities"][0]["title"] = "<unsafe>"
        report = render_research_html("run-en", artifacts, "en")
        self.assertIn("Growth Opportunities", report)
        self.assertIn("&lt;unsafe&gt;", report)
        self.assertNotIn("<unsafe>", report)
        self.assertIn("D · Primarily inferred", report)

    def test_message_renderer_escapes_untrusted_text(self) -> None:
        report = render_message_html("<script>alert(1)</script>", "en")
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("<script>", report)


if __name__ == "__main__":
    unittest.main()
