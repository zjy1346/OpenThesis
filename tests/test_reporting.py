from __future__ import annotations

import unittest

from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run
from openthesis.financials import deterministic_summary


class ReportingTests(unittest.TestCase):
    def test_staged_context_capacity_callout_is_localized_and_keeps_byte_budget(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test",
            "content": {
                "mode": "staged-fallback",
                "report": {
                    "executive_summary": "已完成阶段资料。",
                    "cross_section_synthesis_status": "not_completed_context_capacity",
                    "context_budget": {
                        "required_bytes": 36327,
                        "available_bytes": 36000,
                        "counting_mode": "conservative_utf8_byte_upper_bound",
                    },
                },
            },
        }]
        expected = {
            "zh-CN": "研究阶段数据已完整保留，但跨章节最终综合因当前模型容量不足未完成",
            "zh-Hant": "研究階段資料已完整保留，但跨章節最終綜合因目前模型容量不足未完成",
            "en": "All completed research-stage data was preserved, but cross-section final synthesis was not completed because the model context capacity was insufficient",
        }
        for language, phrase in expected.items():
            report = render_research_run("capacity-callout", artifacts, language)
            self.assertIn(phrase, report)
            self.assertIn("36,327", report)
            self.assertIn("36,000", report)
            self.assertIn("conservative UTF-8 byte upper bound", report) if language == "en" else None

        normal = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test",
            "content": {
                "mode": "synthesized",
                "report": {
                    "executive_summary": "正常综合。",
                    "cross_section_synthesis_status": "not_completed_context_capacity",
                    "context_budget": {"required_bytes": 36327, "available_bytes": 36000},
                },
            },
        }]
        self.assertNotIn("跨章节最终综合因当前模型容量不足未完成", render_research_run("normal", normal, "zh-CN"))

    def test_five_valid_growth_opportunities_survive_both_renderers_and_sixth_is_diagnosed(self) -> None:
        opportunities = [
            {
                "title": f"Opportunity {index}",
                "mechanism": "A verified market mechanism.",
                "evidence_grade": "C",
                "time_horizon_years": 3,
                "probability_range": [0.3, 0.5],
                "supporting_evidence_ids": [f"fact:opportunity:{index}"],
            }
            for index in range(1, 7)
        ]
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "currency": "USD",
                    "metrics": [{"year": 2025, "revenue": 10.0}],
                    "evidence": [{"evidence_id": f"fact:opportunity:{index}"} for index in range(1, 7)],
                },
            },
            {
                "artifact_type": "growth-opportunities",
                "agent_id": "growth-opportunity-analyst",
                "model_id": "test",
                "content": {
                    "opportunities": opportunities[:5],
                    "_lineage": {
                        "raw_candidate_count": 6,
                        "normalized_count": 5,
                        "verified_count": 5,
                        "retained_count": 5,
                        "rejected_count": 1,
                        "cap_applied": True,
                        "cap_limit": 5,
                        "rejected_reasons": ["growth opportunity cap applied"],
                    },
                },
            },
        ]
        markdown = render_research_run("growth-lineage", artifacts, "en")
        html = render_research_html("growth-lineage", artifacts, "en")
        for report in (markdown, html):
            for index in range(1, 6):
                self.assertIn(f"Opportunity {index}", report)
            self.assertNotIn("Opportunity 6", report)

    def test_period_coverage_shows_actual_range_and_missing_reason_in_all_languages(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "currency": "CNY",
                "metrics": [{"year": 2025, "revenue": 10.0}],
                "evidence": [],
                "financial_quality": {
                    "period_coverage": {
                        "available_annual_years": [2023, 2025],
                        "displayed_annual_years": [2023, 2025],
                        "hidden_comparator_years": [2022],
                        "latest_official_fy": 2025,
                        "interim_periods": ["2026 H1"],
                        "missing_or_rejected_years": [{"year": 2024, "status": "rejected", "reason_code": "OFFICIAL_ANNUAL_UNAVAILABLE"}],
                    }
                },
            },
        }]
        for language, present in (("zh-CN", "年度披露范围"), ("zh-Hant", "年度披露範圍"), ("en", "Annual disclosure coverage")):
            report = render_research_run("coverage", artifacts, language, company_name="Example")
            self.assertIn(present, report)
            self.assertIn("2023", report)
            self.assertIn("2025", report)
            self.assertIn("2024", report)
            if language == "en":
                self.assertNotIn("截至研究日未获取到有效官方年报", report)
            if language == "zh-Hant":
                self.assertNotIn("截至研究日未获取到有效官方年报", report)
                self.assertIn("截至研究日未取得有效官方年報", report)
            if language == "zh-CN":
                self.assertIn("截至研究日未获取到有效官方年报", report)
            if language == "en":
                self.assertIn("No valid official annual report was available", report)
    def test_markdown_uses_latest_deterministic_financial_artifact(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "currency": "USD",
                    "metrics": [{"year": 2024, "revenue": 10.0}],
                    "evidence": [{
                        "evidence_id": "old-evidence",
                        "concept": "old source",
                        "source_url": "https://example.test/old",
                    }],
                    "financial_quality": {
                        "rejected_periods": [{"period_end": "2024-12-31"}],
                    },
                },
            },
            {
                "artifact_type": "deterministic-financial-summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "currency": "USD",
                    "metrics": [{"year": 2025, "revenue": 20.0}],
                    "evidence": [{
                        "evidence_id": "new-evidence",
                        "concept": "new source",
                        "source_url": "https://example.test/new",
                    }],
                    "financial_quality": {"rejected_periods": []},
                },
            },
        ]

        report = render_research_run(
            "run-latest-financial", artifacts, "en-US", company_name="Example"
        )

        self.assertIn("2025", report)
        self.assertIn("20", report)
        self.assertIn("new source", report)
        self.assertIn("https://example.test/new", report)
        self.assertNotIn("2024", report)
        self.assertNotIn("old source", report)
        self.assertNotIn("https://example.test/old", report)
        self.assertNotIn("Some annual data failed validation", report)

    def test_financial_refresh_marks_retained_qualitative_synthesis_as_stale(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "title": "refresh",
            "agent_id": "financial-refresh",
            "model_id": "",
            "content": {
                "mode": "financial-refresh",
                "report": {"business_model": "Existing qualitative analysis"},
            },
        }]

        report = render_research_run("refresh-run", artifacts, "en-US")

        self.assertIn("Financial Refresh Status", report)
        self.assertIn("refreshed without a model call", report)
        self.assertIn("Existing qualitative analysis", report)

    def test_financial_refresh_notice_is_traditional_chinese_in_both_formats(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "title": "refresh",
            "agent_id": "financial-refresh",
            "model_id": "",
            "content": {
                "mode": "financial-refresh",
                "report": {"business_model": "既有定性研究"},
            },
        }]

        markdown = render_research_run("refresh-hant", artifacts, "zh-Hant")
        html = render_research_html("refresh-hant", artifacts, "zh-Hant")

        self.assertIn("財務刷新狀態", markdown)
        self.assertIn("不呼叫模型", markdown)
        self.assertIn("財務刷新狀態", html)
        self.assertIn("不呼叫模型", html)

    def test_traditional_chinese_markdown_uses_traditional_section_labels(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "title": "Report",
            "agent_id": "research-synthesizer",
            "model_id": "test",
            "content": {"report": {"claims": [{"text": "\u7d50\u8ad6", "kind": "inference", "confidence": 0.9}]}, "verification": {"passed": True}},
        }]
        report = render_research_run("run-hant", artifacts, "zh-Hant")
        self.assertIn("\u4e3b\u8981\u7d50\u8ad6", report)
    def test_deterministic_summary_uses_one_explicit_column_schema(self) -> None:
        summary = deterministic_summary(
            "Example",
            [{
                "year": 2024,
                "revenue": 10.0,
                "revenue_growth": None,
                "operating_margin": None,
                "net_income": 1.0,
                "operating_cash_flow": 2.0,
                "free_cash_flow": None,
            }],
            "en-US",
            "USD",
        )
        table_lines = [line for line in summary.splitlines() if line.startswith("|")]
        self.assertEqual(len(table_lines), 3)
        self.assertTrue(all(line.count("|") == 6 for line in table_lines))
        self.assertNotIn("Operating margin", summary)
        self.assertNotIn("Free cash flow", summary)

    def test_html_financial_table_uses_one_explicit_column_schema(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "title": "Financial",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [{
                    "year": 2024,
                    "revenue": 10.0,
                    "revenue_growth": None,
                    "operating_margin": None,
                    "net_income": 1.0,
                    "operating_cash_flow": 2.0,
                    "free_cash_flow": None,
                }],
                "currency": "USD",
                "evidence": [],
            },
        }]
        html = render_research_html("run-schema", artifacts, "en-US", company_name="Example")
        import re
        table = re.search(r'<table class="data-table">(.*?)</table>', html, re.S)
        self.assertIsNotNone(table)
        rows = re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S)
        self.assertEqual([len(re.findall(r"<t[hd]", row)) for row in rows], [5, 5])
        cells = re.findall(r"<td[^>]*>(.*?)</td>", rows[1], re.S)
        self.assertEqual(len(cells), 5)
        self.assertIn("10", cells[1])
        self.assertIn("1", cells[3])
        self.assertIn("2", cells[4])

    def test_html_schema_keeps_operating_margin_and_omits_only_free_cash_flow(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "title": "Financial",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [{
                    "year": 2024,
                    "revenue": 10.0,
                    "revenue_growth": 0.1,
                    "operating_margin": 0.2,
                    "net_income": 1.0,
                    "operating_cash_flow": 2.0,
                    "free_cash_flow": None,
                }],
                "currency": "USD",
                "evidence": [],
            },
        }]
        html = render_research_html("run-schema-fcf", artifacts, "en-US", company_name="Example")
        import re
        table = re.search(r'<table class="data-table">(.*?)</table>', html, re.S)
        self.assertIsNotNone(table)
        rows = re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S)
        self.assertEqual([len(re.findall(r"<t[hd]", row)) for row in rows], [6, 6])
        self.assertIn("Operating margin", html)
        self.assertNotIn("Free cash flow", html)
    def test_financial_quality_gap_is_visible_and_uncovered_columns_are_hidden(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "title": "Financial",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [{"year": 2022, "revenue": 10.0, "revenue_growth": None, "net_income": 1.0, "operating_cash_flow": None, "operating_margin": None, "free_cash_flow": None}],
                "currency": "CNY",
                "financial_quality": {"rejected_periods": [{"period_end": "2021-12-31", "issues": ("cash_flow_core_missing",)}]},
                "evidence": [],
            },
        }]
        markdown = render_research_run("run-gap", artifacts, "en-US", company_name="Example")
        html = render_research_html("run-gap", artifacts, "en-US", company_name="Example")
        self.assertIn("Annual Data Continuity", markdown)
        self.assertNotIn("Operating margin", markdown)
        self.assertNotIn("Free cash flow", markdown)
        self.assertIn("Some annual data failed validation", html)
        self.assertNotIn("Operating margin", html)
        self.assertNotIn("Free cash flow", html)

    def test_nontechnical_continuity_notice_hides_identifiers_and_issue_codes(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "title": "Financial",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [{"year": 2024, "revenue": 10.0, "net_income": 1.0}],
                "currency": "CNY",
                "financial_quality": {
                    "period_continuity": [{
                        "accession_number": "private-accession-123",
                        "period_end": "2023-12-31",
                        "status": "no_facts",
                        "issues": ("core_missing",),
                    }],
                },
                "evidence": [],
            },
        }]
        markdown = render_research_run("run-continuity", artifacts, "en-US", company_name="Example")
        html = render_research_html("run-continuity", artifacts, "en-US", company_name="Example")
        for rendered in (markdown, html):
            self.assertIn("Some annual data failed validation", rendered)
            self.assertNotIn("private-accession-123", rendered)
            self.assertNotIn("core_missing", rendered)

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

    def test_empty_synthesis_growth_falls_back_to_valid_growth_artifact(self) -> None:
        artifacts = [
            {
                "artifact_type": "growth-opportunities",
                "title": "增长机会",
                "agent_id": "growth-opportunity-analyst",
                "model_id": "test:model",
                "content": {
                    "opportunities": [
                        {
                            "title": "海外储能扩张",
                            "mechanism": "渠道建设扩大可服务市场。",
                            "evidence_grade": "C",
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
                    "report": {
                        "executive_summary": "综合报告正文。",
                        "growth_opportunities": [],
                    },
                    "verification": {"passed": True},
                },
            },
        ]

        report = render_research_run("run-growth-fallback", artifacts)

        self.assertIn("海外储能扩张", report)
        self.assertNotIn("当前证据不足，未形成可展示的增长机会", report)

    def test_empty_growth_model_response_is_not_reported_as_evidence_insufficiency(self) -> None:
        artifacts = [
            {
                "artifact_type": "growth-opportunities",
                "title": "增长机会",
                "agent_id": "growth-opportunity-analyst",
                "model_id": "test:model",
                "content": {
                    "opportunities": [],
                    "structured_output_valid": False,
                    "_response_error": "empty_content",
                    "_validation": {"passed": False, "issues": ["empty"]},
                },
            }
        ]

        report = render_research_run("run-growth-empty", artifacts)

        self.assertIn("增长机会模型未返回有效内容", report)
        self.assertNotIn("当前证据不足，未形成可展示的增长机会", report)

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

    def test_markdown_explains_missing_prior_interim_comparison(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "currency": "CNY",
                    "metrics": [],
                    "interim_metrics": [
                        {
                            "year": 2026,
                            "period": "Q1",
                            "comparison_period": None,
                            "comparison_gap": "prior_period_unavailable",
                            "revenue": 150_225_314_000,
                            "revenue_growth": None,
                        }
                    ],
                    "evidence": [],
                },
            }
        ]

        report = render_research_run(
            "run-interim-gap", artifacts, "zh-CN", company_name="比亚迪"
        )

        self.assertIn("缺少或未通过校验的上年同期披露", report)
        self.assertNotIn("同期收入增长：—", report)


if __name__ == "__main__":
    unittest.main()
