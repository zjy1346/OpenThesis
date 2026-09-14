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
    def test_html_staged_context_capacity_callout_is_localized_and_keeps_byte_budget(self) -> None:
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
            report = render_research_html("capacity-callout", artifacts, language)
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
        self.assertNotIn("跨章节最终综合因当前模型容量不足未完成", render_research_html("normal", normal, "zh-CN"))

    def test_html_period_coverage_shows_actual_years_and_interim_boundary(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "metrics": [{"year": 2025, "revenue": 10.0}],
                "currency": "CNY",
                "evidence": [],
                "financial_quality": {
                    "period_coverage": {
                        "displayed_annual_years": [2023, 2025],
                        "hidden_comparator_years": [2022],
                        "interim_periods": ["2026 H1"],
                        "missing_or_rejected_years": [{"year": 2024, "status": "rejected", "reason_code": "OFFICIAL_ANNUAL_UNAVAILABLE"}],
                    }
                },
            },
        }]
        for language, title in (("zh-CN", "年度披露范围"), ("zh-Hant", "年度披露範圍"), ("en", "Annual disclosure coverage")):
            html = render_research_html("coverage-html", artifacts, language, company_name="Example")
            self.assertIn(title, html)
            self.assertIn("2023", html)
            self.assertIn("2025", html)
            self.assertIn("2026 H1", html)
            self.assertIn("2024", html)
            if language == "en":
                self.assertNotIn("截至研究日未获取到有效官方年报", html)
            if language == "zh-Hant":
                self.assertNotIn("截至研究日未获取到有效官方年报", html)
                self.assertIn("截至研究日未取得有效官方年報", html)
            if language == "zh-CN":
                self.assertIn("截至研究日未获取到有效官方年报", html)
            if language == "en":
                self.assertIn("No valid official annual report was available", html)

    def test_reverse_dcf_non_ok_reason_and_disclaimer_are_localized(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-valuation",
            "title": "反向 DCF",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {
                "status": "insufficient_data",
                "reason": "没有足够的正自由现金流数据。",
            },
        }]
        english = render_research_html("run-dcf-en", artifacts, "en")
        traditional = render_research_html("run-dcf-hant", artifacts, "zh-Hant")
        assert "There is not enough positive free-cash-flow data." in english
        assert "This result explains market-implied expectations; it is not a price target or trading recommendation." in english
        assert "没有足够的正自由现金流数据。" not in english
        assert "该结果用于解释市场隐含预期，不是目标价或交易建议。" not in english
        assert "沒有足夠的正自由現金流資料。" in traditional
        assert "該結果用於解釋市場隱含預期，不是目標價或交易建議。" in traditional
        assert "没有足够的正自由现金流数据。" not in traditional
        assert "该结果用于解释市场隐含预期，不是目标价或交易建议。" not in traditional

    def test_html_uses_latest_deterministic_financial_artifact(self) -> None:
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

        report = render_research_html(
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

    def test_financial_metric_gaps_show_specific_retry_relevant_reasons(self) -> None:
        artifacts = sample_artifacts()
        metric = artifacts[0]["content"]["metrics"][0]
        metric["revenue_growth"] = None
        metric["comparison_gap"] = "missing_2024"
        metric["return_on_equity"] = None
        metric["return_on_equity_gap"] = "missing_equity"

        report = render_research_html("run-quality-gaps", artifacts, "zh-CN")

        self.assertIn("2025 财年收入增长缺少 2024 财年已验证收入", report)
        self.assertIn("净资产收益率无法计算：缺少权益数据", report)

    def test_financial_table_renders_growth_semantics_instead_of_a_dash(self) -> None:
        artifacts = sample_artifacts()
        metric = artifacts[0]["content"]["metrics"][0]
        metric.update({
            "net_income_growth": None,
            "net_income_growth_status": "turnaround",
            "return_on_equity": None,
            "return_on_equity_gap": "non_positive_equity",
        })

        report = render_research_html("run-growth-semantics", artifacts, "zh-CN")

        self.assertIn("净利润增长", report)
        self.assertIn("扭亏为盈", report)
        self.assertIn("净资产收益率无法计算：权益为零或负数，不适用", report)

    def test_traditional_chinese_report_declares_language(self) -> None:
        report = render_research_html("run-hant", sample_artifacts(), "zh-Hant")
        self.assertIn('<html lang="zh-Hant">', report)
        self.assertIn("\u589e\u9577\u6a5f\u6703", report)

    def test_empty_synthesis_growth_falls_back_to_valid_growth_artifact(self) -> None:
        artifacts = sample_artifacts()
        artifacts[-1]["content"]["report"]["growth_opportunities"] = []

        report = render_research_html("run-growth-fallback", artifacts, "zh-CN")

        self.assertIn("AI 加速计算基础设施", report)
        self.assertNotIn("当前证据不足，未形成可展示的增长机会", report)

    def test_chinese_fallback_report_uses_typed_sections_and_confidence_groups(self) -> None:
        artifacts = sample_artifacts()
        artifacts[-1]["content"] = {
            "mode": "staged-fallback",
            "report": {
                "executive_summary": "最终综合未完成。",
                "business_model": {
                    "summary": "公司通过晶圆代工获得收入。",
                    "possible_moats": ["规模与客户认证"],
                    "risks": ["资本开支较高"],
                    "unknowns": ["客户集中度尚未披露"],
                },
                "claims": [
                    {"text": "收入保持增长。", "kind": "calculation", "confidence": 1.0},
                    {"text": "规模可能构成壁垒。", "kind": "inference", "confidence": 0.7},
                    {"text": "订单能见度不足。", "kind": "unknown", "confidence": None},
                ],
                "unresolved_questions": ["客户集中度尚待核实。"],
            },
            "verification": {
                "passed": False,
                "claim_count": 0,
                "verified_claim_count": 0,
                "unsupported_fact_count": 0,
                "issues": ["Missing required report sections: claims"],
            },
            "retryable": True,
        }

        report = render_research_html("run-fallback", artifacts, "zh-CN", include_technical=False)

        self.assertNotIn('<div class="label">summary</div>', report)
        self.assertNotIn(">calculation<", report)
        self.assertNotIn('<div class="label">unknowns</div>', report)
        self.assertNotIn("Missing required report sections", report)
        self.assertIn('class="confidence-group confidence-high"', report)
        self.assertIn('class="confidence-group confidence-medium"', report)
        self.assertIn('class="confidence-group confidence-unknown"', report)
        self.assertLess(report.index("confidence-high"), report.index("confidence-medium"))
        self.assertIn("阶段性研究报告", report)

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

    def test_nontechnical_report_recursively_hides_internal_protocol_fields(self) -> None:
        artifacts = sample_artifacts()
        artifacts[-1]["content"]["report"].update(
            {
                "counterarguments": [
                    {
                        "argument": "资本回报仍需验证。",
                        "target_opportunity_ids": ["growth-1"],
                        "confidence": 0.9,
                        "evidence_ids": ["fact:market:secret"],
                    }
                ],
                "scenarios": {
                    "base": {
                        "assumptions": ["需求保持稳定。"],
                        "evidence_ids": ["filing:secret"],
                    }
                },
                "claims": [
                    {
                        "text": "盈利能力承压。",
                        "kind": "inference",
                        "evidence_ids": ["fact:market:claim"],
                    }
                ],
            }
        )

        report = render_research_html("run-clean", artifacts, include_technical=False)

        self.assertIn("资本回报仍需验证", report)
        self.assertIn("需求保持稳定", report)
        self.assertIn("盈利能力承压", report)
        self.assertNotIn("target_opportunity_ids", report)
        self.assertNotIn("evidence_ids", report)
        self.assertNotIn("fact:market:", report)
        self.assertNotIn("filing:secret", report)
        self.assertNotIn(">inference<", report)
        self.assertIn("推论", report)

    def test_upstream_counter_analysis_aliases_reach_html_projection(self) -> None:
        artifacts = sample_artifacts()
        artifacts[-1]["content"]["report"]["counterarguments"] = {
            "strongest_counterarguments": ["产业价格战压缩利润"],
            "unsupported_assumptions": ["高端化会自动成功"],
            "missing_evidence": ["海外关税敏感性尚待验证"],
        }
        report = render_research_html("run-counter-upstream", artifacts, "zh-CN")
        self.assertIn("产业价格战压缩利润", report)
        self.assertIn("高端化会自动成功", report)
        self.assertIn("海外关税敏感性尚待验证", report)

    def test_english_and_html_escaping(self) -> None:
        artifacts = sample_artifacts()
        artifacts[1]["content"]["opportunities"][0]["title"] = "<unsafe>"
        report = render_research_html("run-en", artifacts, "en")
        self.assertIn("Growth Opportunities", report)
        self.assertIn("&lt;unsafe&gt;", report)
        self.assertNotIn("<unsafe>", report)
        self.assertIn("D · Primarily inferred", report)

    def test_empty_growth_model_response_is_explained_without_claiming_evidence_shortage(self) -> None:
        report = render_research_html(
            "run-growth-empty",
            [
                {
                    "artifact_type": "growth-opportunities",
                    "title": "Growth Opportunities",
                    "agent_id": "growth-opportunity-analyst",
                    "model_id": "test:model",
                    "content": {
                        "opportunities": [],
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                        "_validation": {"passed": False, "issues": ["empty"]},
                    },
                }
            ],
            "en",
        )

        self.assertIn("The growth-opportunity model returned no usable content", report)
        self.assertNotIn("Current evidence is insufficient", report)

    def test_message_renderer_escapes_untrusted_text(self) -> None:
        report = render_message_html("<script>alert(1)</script>", "en")
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("<script>", report)


if __name__ == "__main__":
    unittest.main()
