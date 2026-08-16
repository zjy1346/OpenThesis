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


if __name__ == "__main__":
    unittest.main()
