from __future__ import annotations

import unittest

from openthesis.report_readiness import (
    REQUIRED_REPORT_SECTIONS,
    REQUIRED_RESEARCH_ARTIFACTS,
)
from openthesis.service import _report_completion_state


class ResearchCompletionStateTests(unittest.TestCase):
    def test_partial_attempt_is_finished_but_requires_action(self) -> None:
        state = _report_completion_state(
            {"status": "partial"},
            [{
                "artifact_type": "research-report",
                "content": {"verification": {"passed": False}},
            }],
            {"latest": {"outcome": "waiting_retryable"}},
        )
        self.assertTrue(state["attempt_complete"])
        self.assertFalse(state["research_complete"])
        self.assertTrue(state["action_required"])
        self.assertEqual(state["report_readiness"]["state"], "action_required")

    def test_verification_flag_without_substantive_report_requires_action(self) -> None:
        state = _report_completion_state(
            {"status": "completed"},
            [{
                "artifact_type": "research-report",
                "content": {"verification": {"passed": True}},
            }],
            {"latest": {"outcome": "complete"}},
        )
        self.assertTrue(state["attempt_complete"])
        self.assertFalse(state["research_complete"])
        self.assertTrue(state["action_required"])
        self.assertEqual(state["report_readiness"]["state"], "action_required")

    def test_substantive_verified_report_with_prerequisites_is_complete(self) -> None:
        artifacts = [
            {"artifact_type": artifact_type, "content": {"available": True}}
            for artifact_type in REQUIRED_RESEARCH_ARTIFACTS
        ]
        artifacts[0]["content"] = {
            "evidence": [{
                "kind": "financial_fact",
                "evidence_id": "fact:annual-revenue",
            }],
        }
        report = {
            section: (
                [{"text": "核验后的重要结论。"}]
                if section == "claims"
                else f"有证据支持的{section}分析。"
            )
            for section in REQUIRED_REPORT_SECTIONS
        }
        artifacts.append({
            "artifact_type": "research-report",
            "content": {
                "mode": "synthesized",
                "report": report,
                "verification": {
                    "passed": True,
                    "verified_claim_count": 1,
                    "unsupported_fact_count": 0,
                },
            },
        })

        state = _report_completion_state(
            {"status": "completed"}, artifacts, {"latest": {"outcome": "complete"}}
        )

        self.assertTrue(state["research_complete"])
        self.assertFalse(state["action_required"])
        self.assertEqual(state["report_readiness"]["state"], "complete")


if __name__ == "__main__":
    unittest.main()
