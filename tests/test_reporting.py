from __future__ import annotations

import unittest

from openthesis.reporting import render_research_run


class ReportingTests(unittest.TestCase):
    def test_renders_deterministic_and_structured_sections(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "确定性财务概览",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "markdown": "# Example 财务概览",
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
                "artifact_type": "research-report",
                "title": "完整长期研究报告",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {
                    "report": {
                        "executive_summary": "收入增长，但现金转化承压。",
                        "unresolved_questions": ["资本开支回报"],
                    },
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
        report = render_research_run("run1", artifacts)
        self.assertIn("执行摘要", report)
        self.assertIn("资本开支回报", report)
        self.assertIn("验证结果", report)
        self.assertIn("不构成投资建议", report)
        self.assertIn("证据来源", report)
        self.assertIn("https://www.sec.gov/example", report)

    def test_english_renderer_translates_local_content_but_preserves_ai_text(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "确定性财务概览",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "markdown": "# Example 财务概览",
                    "metrics": [
                        {
                            "year": 2025,
                            "revenue": 1_000_000_000,
                            "revenue_growth": 0.1,
                            "operating_margin": 0.2,
                            "net_income": 100_000_000,
                            "operating_cash_flow": 120_000_000,
                            "free_cash_flow": 90_000_000,
                            "cash_conversion": 1.2,
                            "debt_to_assets": 0.3,
                            "return_on_equity": 0.15,
                        }
                    ],
                    "evidence": [],
                },
            },
            {
                "artifact_type": "research-report",
                "title": "完整长期研究报告",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {
                    "report": {
                        "executive_summary": "这段旧 AI 正文必须保持原文。",
                    },
                    "verification": {
                        "passed": True,
                        "claim_count": 0,
                        "verified_claim_count": 0,
                        "unsupported_fact_count": 0,
                        "issues": [],
                    },
                },
            },
        ]
        report = render_research_run(
            "run-en", artifacts, "en", company_name="Example Corp"
        )
        self.assertIn("Long-term Company Research", report)
        self.assertIn("Financial Overview", report)
        self.assertIn("Executive Summary", report)
        self.assertIn("Verification Results", report)
        self.assertIn("这段旧 AI 正文必须保持原文。", report)
        self.assertNotIn("## 执行摘要", report)

    def test_unknown_report_language_falls_back_to_chinese(self) -> None:
        report = render_research_run("run-fallback", [], "unknown")
        self.assertIn("长期公司研究", report)

    def test_markdown_nontechnical_projection_hides_internal_ids(self) -> None:
        artifacts = [
            {
                "artifact_type": "research-report",
                "title": "完整长期研究报告",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {
                    "report": {
                        "counterarguments": [
                            {
                                "argument": "反方论点正文",
                                "target_opportunity_ids": ["growth-1"],
                                "evidence_ids": ["fact:market:hidden"],
                            }
                        ],
                        "claims": [
                            {
                                "text": "主要结论正文",
                                "kind": "inference",
                                "evidence_ids": ["filing:hidden"],
                            }
                        ],
                    },
                    "verification": {"passed": True},
                },
            }
        ]

        report = render_research_run("run-clean", artifacts, include_technical=False)

        self.assertIn("反方论点正文", report)
        self.assertIn("主要结论正文", report)
        self.assertNotIn("target opportunity ids", report.casefold())
        self.assertNotIn("evidence ids", report.casefold())
        self.assertNotIn("fact:market:", report)
        self.assertNotIn("filing:hidden", report)
        self.assertNotIn("类型：** inference", report)
        self.assertIn("**推论** · 主要结论正文", report)

    def test_growth_fields_are_localized_and_ids_hidden_by_default(self) -> None:
        artifacts = [
            {
                "artifact_type": "growth-opportunities",
                "title": "增长机会",
                "agent_id": "growth-opportunity-analyst",
                "model_id": "test:model",
                "content": {
                    "opportunities": [
                        {
                            "title": "新产品平台",
                            "mechanism": "扩大可服务市场。",
                            "evidence_grade": "C",
                            "time_horizon_years": 3,
                            "probability_range": [0.3, 0.5],
                            "supporting_evidence_ids": ["fact:private"],
                            "scenario_eligibility": ["base"],
                        }
                    ]
                },
            }
        ]
        report = render_research_run("run-growth", artifacts)
        self.assertIn("新产品平台", report)
        self.assertIn("30%–50%", report)
        self.assertNotIn("supporting_evidence_ids", report)
        self.assertNotIn("fact:private", report)
        technical = render_research_run(
            "run-growth",
            artifacts,
            include_technical=True,
        )
        self.assertIn("fact:private", technical)

    def test_chinese_markdown_groups_claims_and_hides_raw_validation_errors(self) -> None:
        artifacts = [
            {
                "artifact_type": "research-report",
                "title": "report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {
                    "mode": "staged-fallback",
                    "report": {
                        "business_model": {
                            "summary": "公司通过晶圆代工获得收入。",
                            "claims": [
                                {"text": "高置信结论一", "kind": "calculation", "confidence": 0.9},
                                {"text": "高置信结论二", "kind": "calculation", "confidence": 0.9},
                                {"text": "低置信结论", "kind": "unknown", "confidence": 0.3},
                            ],
                        },
                    },
                    "verification": {
                        "passed": False,
                        "issues": ["Missing required report sections: raw_internal_key"],
                    },
                },
            }
        ]

        report = render_research_run("run-grouped", artifacts, "zh-CN")

        self.assertIn("### 高置信度 · 0.90 · 2 条", report)
        self.assertIn("### 低置信度 · 0.30 · 1 条", report)
        self.assertLess(report.index("高置信结论一"), report.index("低置信结论"))
        self.assertIn("计算", report)
        self.assertNotIn("calculation", report)
        self.assertNotIn("raw_internal_key", report)
        self.assertNotIn("business_model", report)

    def test_markdown_renders_latest_interim_separately_from_annual_metrics(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "currency": "CNY",
                    "metrics": [{"year": 2025, "revenue": 100_000_000_000}],
                    "interim_metrics": [
                        {
                            "year": 2026,
                            "period": "Q1",
                            "comparison_period": "2025 Q1",
                            "revenue": 30_000_000_000,
                            "revenue_growth": 0.2,
                            "net_income": 3_000_000_000,
                            "operating_cash_flow": 4_000_000_000,
                        }
                    ],
                    "evidence": [],
                },
            }
        ]

        report = render_research_run(
            "run-interim", artifacts, "zh-CN", company_name="示例公司"
        )

        self.assertIn("最新季度及中期数据", report)
        self.assertIn("2026 Q1 为累计期间数据，不与完整财年混算", report)
        self.assertIn("对比 2025 Q1", report)


if __name__ == "__main__":
    unittest.main()
