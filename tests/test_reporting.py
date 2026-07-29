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


if __name__ == "__main__":
    unittest.main()
