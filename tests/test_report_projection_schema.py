from __future__ import annotations

import unittest
from unittest.mock import patch

from openthesis.report_html import render_research_html
from openthesis.reporting import _render_value, render_research_run
from openthesis.report_projection import project_report_diagnostics, project_report_value


def _artifacts() -> list[dict[str, object]]:
    return [
        {
            "artifact_type": "research-report",
            "title": "Long-term report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {
                "report": {
                    "unexpected_protocol_key": "DO_NOT_RENDER",
                    "counterarguments": [
                        {
                            "title": "现金流风险",
                            "counterargument": "利润增长没有转化为现金。",
                            "severity": "high",
                            "evidence_ids": ["fact:secret"],
                        }
                    ],
                    "claims": [
                        {
                            "text": "收入增长",
                            "kind": "calculation",
                            "confidence": 0.7,
                            "evidence_ids": ["fact:secret"],
                        }
                    ],
                },
                "verification": {"passed": True},
            },
        }
    ]


class ReportProjectionSchemaTests(unittest.TestCase):
    def test_claim_identity_metadata_never_changes_claim_body_shape(self) -> None:
        body = "美团2025年度营业收入为3675亿元。"
        projected = project_report_value(
            {
                "claims": [{
                    "text": body,
                    "kind": "fact",
                    "company_cik": "HK:SEHK:03690.HK",
                    "concept": "revenue",
                    "period": "2025-12-31",
                    "unit": "CNY",
                    "entity": "美团",
                }],
            },
            include_technical=False,
        )

        claim = projected["claims"][0]
        self.assertEqual(claim["text"], body)
        self.assertIsInstance(claim["text"], str)
        markdown = render_research_run(
            "run-safe-id",
            [{
                "artifact_type": "research-report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {"report": projected},
            }],
            "zh-CN",
            include_technical=False,
        )
        self.assertIn(body, markdown)
        self.assertNotIn("HK:SEHK:03690.HK", markdown)

    def test_moat_projection_is_path_aware_and_renderer_is_total(self) -> None:
        payload = {
            "business_model": {
                "possible_moats": [
                    {
                        "kind": "cost_advantage",
                        "description": "规模降低单位成本。",
                        "durability": "high",
                    }
                ]
            }
        }

        projected = project_report_value(
            payload, include_technical=False, section="business_model"
        )

        moat = projected["business_model"]["possible_moats"][0]
        self.assertEqual(moat["kind"], "cost_advantage")
        self.assertEqual(moat["description"], "规模降低单位成本。")
        self.assertEqual(moat["durability"], "high")
        self.assertTrue(_render_value([{}], "zh-CN", 0))

    def test_direct_html_resolves_structured_company_identity(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "确定性财务概览",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "company": {"name": "宁德时代", "ticker": "300750.SZ"},
                    "metrics": [],
                    "evidence": [],
                },
            }
        ]

        html = render_research_html("run-identity", artifacts, "zh-CN")

        self.assertIn("宁德时代", html)
        self.assertIn("300750.SZ", html)
        self.assertNotIn(">公司研究报告<", html)

    def test_markdown_resolves_structured_company_identity(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "title": "确定性财务概览",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {
                    "company": {"name": "宁德时代", "ticker": "300750.SZ"},
                    "metrics": [],
                    "evidence": [],
                },
            }
        ]

        markdown = render_research_run("run-identity", artifacts, "zh-CN")

        self.assertIn("宁德时代", markdown)
        self.assertIn("300750.SZ", markdown)

    def test_html_isolates_one_malformed_section(self) -> None:
        original = project_report_value

        def fail_one(value: object, **kwargs: object) -> object:
            if kwargs.get("section") == "business_model":
                raise ValueError("fixture failure")
            return original(value, **kwargs)

        with patch("openthesis.reporting.project_report_value", side_effect=fail_one):
            html = render_research_html("run-isolated", _artifacts(), "en-US")

        self.assertIn("This section could not be displayed", html)
        self.assertIn("收入增长", html)

    def test_nontechnical_financial_sections_keep_public_accounting_risk_fields(self) -> None:
        payload = {
            "financial_quality": {
                "summary": "质量摘要",
                "risk_flags": ["应收账款增长快于收入"],
                "benign_explanations": ["季节性备货"],
                "follow_up_questions": ["回款是否在下一季度恢复？"],
            },
            "balance_sheet": {
                "summary": "资产负债表摘要",
                "risk_flags": ["短债增加"],
                "benign_explanations": ["在手现金充足"],
                "follow_up_questions": ["债务期限结构如何？"],
            },
        }

        projected = project_report_value(payload, include_technical=False)

        for section in ("financial_quality", "balance_sheet"):
            self.assertIn("risk_flags", projected[section])
            self.assertIn("benign_explanations", projected[section])
            self.assertIn("follow_up_questions", projected[section])
    def test_three_locales_render_the_same_canonical_claims_without_protocol_leaks(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {
                "business_model": {
                    "summary": "CANONICAL_SUMMARY",
                    "possible_moats": ["CANONICAL_MOAT"],
                    "risks": ["CANONICAL_RISK"],
                },
                "claims": [
                    {"text": "CANONICAL_FACT", "kind": "fact", "confidence": 0.9},
                    {"text": "CANONICAL_CALCULATION", "kind": "calculation", "confidence": 0.9},
                    {"text": "CANONICAL_INFERENCE", "kind": "inference", "confidence": 0.4},
                ],
                "scenarios": [{"summary": "CANONICAL_SCENARIO", "base": "base", "bear": "bear", "bull": "bull"}],
            }},
        }]

        rendered = {
            language: render_research_run(
                f"run-{language}", artifacts, language, include_technical=False
            )
            for language in ("zh-CN", "zh-Hant", "en")
        }
        canonical_markers = (
            "CANONICAL_SUMMARY", "CANONICAL_MOAT", "CANONICAL_RISK",
            "CANONICAL_FACT", "CANONICAL_CALCULATION", "CANONICAL_INFERENCE",
            "CANONICAL_SCENARIO",
        )
        for report in rendered.values():
            for marker in canonical_markers:
                self.assertEqual(report.count(marker), 1)
            for raw_key in ("business_model", "claims", "kind"):
                self.assertNotIn(raw_key, report)
        for report in (rendered["zh-CN"], rendered["zh-Hant"]):
            for raw_value in ("calculation", "inference", "fact", "base", "bear", "bull"):
                self.assertNotIn(raw_value, report)
        self.assertIn("主要结论", rendered["zh-CN"])
        self.assertIn("主要結論", rendered["zh-Hant"])
        self.assertIn("Key Claims", rendered["en"])
        self.assertIn("事實", rendered["zh-Hant"])
        self.assertIn("計算", rendered["zh-Hant"])
        self.assertIn("推論", rendered["zh-Hant"])
        self.assertIn("基準", rendered["zh-Hant"])
        self.assertIn("悲觀", rendered["zh-Hant"])
        self.assertIn("樂觀", rendered["zh-Hant"])

    def test_scenarios_projection_preserves_structured_values(self) -> None:
        projected = project_report_value(
            {"scenarios": [{"summary": "Base case", "probability": 0.6}]},
            include_technical=False,
            section="scenarios",
        )
        self.assertEqual(projected["scenarios"][0]["summary"], "Base case")
        self.assertEqual(projected["scenarios"][0]["probability"], 0.6)

    def test_identifier_only_bracket_groups_are_removed_as_a_unit(self) -> None:
        projected = project_report_value(
            {"summary": "Growth [fact:revenue, fact:margin] remains supported."},
            include_technical=False,
            section="executive_summary",
        )
        self.assertNotIn("fact:", projected["summary"])
        self.assertNotIn("[, ]", projected["summary"])
        self.assertNotIn("[ , ]", projected["summary"])
        self.assertIn("Growth", projected["summary"])

    def test_unknown_text_extension_is_kept_in_extension_container(self) -> None:
        projected = project_report_value(
            {"scenarios": [{"summary": "Base", "analyst_note": "Watch demand elasticity."}]},
            include_technical=False,
            section="scenarios",
        )
        self.assertEqual(projected["scenarios"][0]["summary"], "Base")
        self.assertEqual(
            projected["scenarios"][0]["extensions"],
            [{"field": "analyst note", "text": "Watch demand elasticity.", "context": "Base"}],
        )

    def test_explicit_body_field_is_supported_without_unknown_field_fallback(self) -> None:
        projected = project_report_value(
            {"business_model": {"body": "The company earns revenue from subscriptions."}},
            include_technical=False,
            section="business_model",
        )
        self.assertEqual(
            projected["business_model"]["body"],
            "The company earns revenue from subscriptions.",
        )

    def test_typed_sections_keep_registered_prose_and_record_evidence(self) -> None:
        projected = project_report_value(
            {
                "counterarguments": [{
                    "title": "Capital intensity",
                    "counterargument": "Expansion consumes substantial capital.",
                    "evidence_ids": ["filing:valid", "fact:missing"],
                    "source_note": "The filing attributes spending to new capacity.",
                    "analyst_context": "The effect may persist for two years.",
                }],
            },
            include_technical=False,
            section="counterarguments",
            available_evidence={"filing:valid"},
        )
        item = projected["counterarguments"][0]
        self.assertEqual(item["counterargument"], "Expansion consumes substantial capital.")
        self.assertNotIn("source_note", item)
        self.assertNotIn("analyst_context", item)
        self.assertEqual(item["evidence_count"], 1)

    def test_implied_expectations_project_and_render_semantic_percentage_ranges(self) -> None:
        report = {
            "implied_expectations": {
                "summary": "The valuation assumes durable growth.",
                "revenue_cagr_range": [0.12, 0.16],
                "operating_margin_range": [0.24, 0.28],
                "probability_range": [0.4, 0.6],
                "market_assumption": "Demand remains resilient.",
                "sensitivity_note": "Higher reinvestment lowers near-term cash flow.",
            }
        }
        markdown = render_research_run(
            "expectations-run",
            [{
                "artifact_type": "research-report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {"report": report},
            }],
            "en",
        )
        html = render_research_html(
            "expectations-run",
            [{
                "artifact_type": "research-report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {"report": report},
            }],
            "en",
        )
        for rendered in (markdown, html):
            self.assertIn("12%–16%", rendered)
            self.assertIn("24%–28%", rendered)
            self.assertIn("40%–60%", rendered)
            self.assertIn("Demand remains resilient.", rendered)
            self.assertIn("Higher reinvestment lowers near-term cash flow.", rendered)

    def test_nested_implied_expectation_analysis_survives_projection_to_markdown(self) -> None:
        nested_analysis = "The base case assumes revenue growth normalizes after capacity expansion."
        report = {
            "implied_expectations": {
                "summary": "The valuation embeds sustained growth.",
                "scenarios": {
                    "base": {
                        "analysis": {
                            "revenue_path": nested_analysis,
                            "evidence_basis": "Company guidance and disclosed capacity ramp.",
                        }
                    }
                },
            }
        }
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": report},
        }]

        markdown = render_research_run("nested-expectations", artifacts, "en")
        html = render_research_html("nested-expectations", artifacts, "en")

        self.assertIn(nested_analysis, markdown)
        self.assertIn(nested_analysis, html)

    def test_required_sections_have_localized_missing_content_in_all_languages(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {}},
        }]
        for language, marker in (("zh-CN", "当前研究阶段未返回可验证"), ("zh-Hant", "本研究階段未返回可驗證"), ("en", "This section was not returned with verifiable content")):
            for rendered in (
                render_research_run("run-required", artifacts, language, include_technical=False),
                render_research_html("run-required", artifacts, language, include_technical=False),
            ):
                self.assertIn(marker, rendered)
                visible = rendered.split("</style>", 1)[-1]
                self.assertNotIn("None", visible)
                self.assertNotIn("unknown", visible.casefold())
                self.assertNotIn("fact:", visible)
                self.assertNotIn("evidence:", visible)

    def test_nontechnical_projection_cleans_identifier_values_and_typed_sections(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {
                "executive_summary": {"summary": "增长来自 fact:ingest:revenue-2025"},
                "claims": [{"text": "Supported by evidence:ingest:secret", "kind": "private_protocol", "confidence": 0.8}],
                "counterarguments": [{"title": "Risk", "counterargument": "See 12345678-1234-1234-1234-123456789abc", "severity": "internal_severity"}],
                "invalidation_conditions": [{"condition": "Revenue falls"}],
                "leading_indicators": [{"indicator": "Retention"}],
                "unresolved_questions": [{"question": "What changes?"}],
            }},
        }]
        markdown = render_research_run("run-clean-values", artifacts, "en-US")
        html = render_research_html("run-clean-values", artifacts, "en-US")
        for rendered in (markdown, html):
            self.assertNotIn("fact:ingest:", rendered)
            self.assertNotIn("evidence:ingest:", rendered)
            self.assertNotIn("12345678-1234-1234-1234-123456789abc", rendered)
            self.assertNotIn("private_protocol", rendered)
            self.assertNotIn("internal_severity", rendered)
        self.assertIn("Revenue falls", markdown)
        self.assertIn("What changes?", html)

    def test_real_chinese_compound_payload_has_typed_sections_and_confidence_groups(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {
                "executive_summary": "公司保持增长，但现金转换仍需验证。",
                "business_model": {"summary": "通过软件订阅获得收入。"},
                "claims": [
                    {"text": "收入增长稳定", "kind": "calculation", "confidence": 0.92},
                    {"text": "竞争优势可能减弱", "kind": "inference", "confidence": 0.35},
                ],
                "counterarguments": [{"title": "现金流风险", "counterargument": "利润尚未充分转化为现金。", "severity": "high"}],
                "unknowns": ["客户集中度尚未披露"],
                "unresolved_questions": ["未来需求是否持续？"],
                "secret_protocol": "不应显示的值",
            }, "verification": {"passed": True}}}
        ]
        markdown = render_research_run("run-cn", artifacts, "zh-CN", include_technical=False)
        html = render_research_html("run-cn", artifacts, "zh-CN", include_technical=False)
        for rendered in (markdown, html):
            self.assertIn("反方观点", rendered)
            self.assertNotIn("counterargument", rendered)
            self.assertNotIn("severity", rendered)
            self.assertNotIn("secret_protocol", rendered)
            self.assertNotIn("不应显示的值", rendered)
        self.assertIn("高置信度", markdown)
        self.assertIn("低置信度", markdown)
        self.assertNotIn("High confidence", html)
        self.assertIn("secret_protocol", project_report_diagnostics(artifacts[0]["content"]["report"]))

    def test_malformed_claim_text_list_never_leaks_python_repr(self) -> None:
        narrative = "美团2021年度营业收入为1791.28亿元人民币。"
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {"claims": [{
                "text": [narrative, "HK:SEHK:03690.HK", "revenue", "2021-12-31", "CNY", "FY"],
                "kind": "fact",
                "confidence": 0.9,
            }]}},
        }]

        markdown = render_research_run("malformed-claim", artifacts, "zh-CN")
        html = render_research_html("malformed-claim", artifacts, "zh-CN")

        for rendered in (markdown, html):
            self.assertNotIn(narrative, rendered)
            self.assertNotIn("['美团", rendered)
            self.assertNotIn("HK:SEHK:03690.HK", rendered)
            self.assertNotIn("'revenue'", rendered)
            self.assertIn("正文文本字段类型错误，内容未显示", rendered)

    def test_removed_internal_references_do_not_leave_empty_parentheses(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {
                "counterarguments": [{
                    "title": "Cash flow risk",
                    "counterargument": "Operating cash flow turned negative (filing:private-2025, ).",
                    "severity": "high",
                }],
            }},
        }]
        markdown = render_research_run("empty-citation-shell", artifacts, "en")
        html = render_research_html("empty-citation-shell", artifacts, "en")
        for rendered in (markdown, html):
            self.assertNotIn("filing:private-2025", rendered)
            self.assertNotIn("()", rendered)
            self.assertNotIn("(, )", rendered)
        self.assertNotIn("- - **", markdown)

    def test_english_projection_uses_english_labels_and_technical_mode_keeps_audit_fields(self) -> None:
        artifacts = [{"artifact_type": "research-report", "agent_id": "research-synthesizer", "model_id": "test:model", "content": {"report": {
            "claims": [{"text": "Revenue is growing", "kind": "calculation", "confidence": 0.9, "evidence_ids": ["fact:revenue"]}],
            "counterarguments": [{"title": "Cash risk", "counterargument": "Conversion is unproven", "severity": "high"}],
        }}}]
        english = render_research_run("run-en", artifacts, "en-US", include_technical=False)
        technical = render_research_run("run-en", artifacts, "en-US", include_technical=True)
        self.assertIn("Key Claims", english)
        self.assertNotIn("主要结论", english)
        self.assertNotIn("evidence_ids", english)
        self.assertIn("Counterarguments", technical)
    def test_markdown_localizes_counterargument_and_severity_without_internal_keys(self) -> None:
        report = render_research_run(
            "run-schema",
            _artifacts(),
            "zh-CN",
            include_technical=False,
        )
        self.assertIn("反方观点", report)
        self.assertNotIn("counterargument", report)
        self.assertNotIn("severity", report)
        self.assertNotIn("calculation", report)
        self.assertNotIn("evidence_ids", report)

    def test_html_localizes_counterargument_and_severity_without_internal_keys(self) -> None:
        report = render_research_html(
            "run-schema",
            _artifacts(),
            "zh-CN",
            include_technical=False,
        )
        self.assertIn("反方观点", report)
        self.assertNotIn("counterargument", report)
        self.assertNotIn("severity", report)
        self.assertNotIn("calculation", report)
        self.assertNotIn("evidence_ids", report)

    def test_unknown_protocol_key_and_value_are_not_rendered(self) -> None:
        report = render_research_run("run-schema", _artifacts(), "zh-CN", include_technical=False)
        self.assertNotIn("DO_NOT_RENDER", report)
        self.assertNotIn("unexpected_protocol_key", report)

    def test_nontechnical_growth_counts_valid_evidence_before_hiding_ids(self) -> None:
        artifacts = [{
            "artifact_type": "deterministic-financial-summary",
            "agent_id": "calculation-engine",
            "model_id": "deterministic",
            "content": {"metrics": [], "evidence": [{"evidence_id": "fact:revenue"}]},
        }, {
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {"growth_opportunities": [{
                "title": "海外扩张",
                "mechanism": "新增市场带动收入。",
                "evidence_grade": "B",
                "time_horizon_years": 3,
                "probability_range": [0.4, 0.6],
                "supporting_evidence_ids": ["fact:revenue", "fact:revenue"],
                "contradicting_evidence_ids": ["fact:missing"],
                "unknown_evidence_ids": ["fact:missing"],
            }]}, "verification": {"passed": True}},
        }]

        markdown = render_research_run(
            "run-growth-count", artifacts, "zh-CN", include_technical=False
        )
        html = render_research_html(
            "run-growth-count", artifacts, "en-US", include_technical=False
        )

        self.assertIn("证据：1 条支持证据 · 0 条相反证据", markdown)
        self.assertIn("1 supporting evidence", html)
        self.assertIn("0 contradicting evidence", html)
        for rendered in (markdown, html):
            self.assertNotIn("fact:revenue", rendered)
            self.assertNotIn("fact:missing", rendered)

    def test_legacy_narrative_is_sectioned_and_missing_sections_are_explicit(self) -> None:
        artifacts = [{
            "artifact_type": "research-report",
            "agent_id": "research-synthesizer",
            "model_id": "test:model",
            "content": {"report": {"narrative": "Only a partial synthesis was returned."}},
        }]

        markdown = render_research_run("run-narrative", artifacts, "en-US")
        html = render_research_html("run-narrative", artifacts, "zh-Hant")

        self.assertIn("## Executive Summary", markdown)
        self.assertIn("Only a partial synthesis was returned.", markdown)
        self.assertIn("## Business Model", markdown)
        self.assertIn("This section was not returned with verifiable content", markdown)
        self.assertNotIn("Original Model Research", markdown)
        self.assertIn("商業模式", html)
        self.assertIn("本研究階段未返回可驗證的此章節內容", html)

    def test_growth_count_uses_actual_evidence_registry_even_without_unknown_list(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {"metrics": [], "evidence": [{"evidence_id": "fact:registered"}]},
            },
            {
                "artifact_type": "research-report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {"report": {"growth_opportunities": [{
                    "title": "Expansion",
                    "mechanism": "New markets",
                    "supporting_evidence_ids": ["fact:registered", "fact:not-registered"],
                    "contradicting_evidence_ids": ["fact:not-registered"],
                }]}},
            },
        ]

        report = render_research_run("run-registry", artifacts, "en-US")

        self.assertIn("Evidence: 1 supporting evidence · 0 contradicting evidence", report)
        self.assertNotIn("fact:registered", report)
        self.assertNotIn("fact:not-registered", report)

    def test_contradicting_only_and_empty_registry_never_trust_model_count(self) -> None:
        artifacts = [
            {
                "artifact_type": "deterministic-financial-summary",
                "agent_id": "calculation-engine",
                "model_id": "deterministic",
                "content": {"metrics": [], "evidence": []},
            },
            {
                "artifact_type": "research-report",
                "agent_id": "research-synthesizer",
                "model_id": "test:model",
                "content": {"report": {"growth_opportunities": [{
                    "title": "Expansion",
                    "mechanism": "New markets",
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": ["fact:not-registered"],
                    "supporting_evidence_count": 8,
                    "contradicting_evidence_count": 9,
                }]}},
            },
        ]
        markdown = render_research_run("run-empty-evidence", artifacts, "en")
        html = render_research_html("run-empty-evidence", artifacts, "en")
        for rendered in (markdown, html):
            self.assertIn("No verified evidence cited", rendered)
            self.assertNotIn("9 contradicting", rendered)
            self.assertNotIn("8 supporting", rendered)



if __name__ == "__main__":
    unittest.main()
