from __future__ import annotations

import unittest

from openthesis.report_html import render_research_html
from openthesis.reporting import render_research_run
from openthesis.report_projection import project_report_diagnostics


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
