from __future__ import annotations

import unittest

from openthesis.research_recovery import ResearchRecoveryPlanner


def _deterministic_summary():
    """Minimum source-backed summary required by the current recovery gate."""
    return {
        "company": {"cik": "0000123456", "ticker": "TEST"},
        "evidence": [{
            "evidence_id": "filing:body:1",
            "kind": "filing_text",
            "document_id": "official-report",
            "source_url": "https://official.example/report.pdf",
            "excerpt": "The company describes its markets and operations.",
            "topic": "unknown",
        }],
    }


class ResearchRecoveryPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ResearchRecoveryPlanner()

    def test_zero_base_success_plans_one_dependency_ordered_resume(self) -> None:
        artifacts = [{
            "artifact_id": "run:base-outcome",
            "artifact_type": "stage-outcome",
            "content": {
                "stage": "base-agents", "completed": 0, "retryable": True,
            },
        }]
        plan = self.planner.plan(artifacts, target="model-stages")
        self.assertTrue(plan.available)
        self.assertEqual(plan.stages[:3], (
            "financial-analyst", "business-analyst", "accounting-risk-analyst",
        ))
        self.assertEqual(plan.stages[-1], "final-synthesis")
        self.assertEqual(plan.plan_hash, self.planner.plan(artifacts, target="model-stages").plan_hash)

    def test_successful_base_artifact_is_never_planned_for_replay(self) -> None:
        artifacts = [
            {
                "artifact_id": "run:financial",
                "artifact_type": "agent-analysis",
                "agent_id": "financial-analyst",
                "content": {"trusted_state": "completed_verified"},
            },
            {
                "artifact_id": "run:base-outcome",
                "artifact_type": "stage-outcome",
                "content": {"stage": "base-agents", "completed": 1, "retryable": True},
            },
        ]
        self.assertFalse(self.planner.plan(artifacts, target="model-stages").available)

    def test_unbound_agent_artifact_blocks_unsafe_base_replay(self) -> None:
        artifacts = [
            {
                "artifact_id": "run:unbound-analysis",
                "artifact_type": "agent-analysis",
                "content": {"claims": []},
            },
            {
                "artifact_id": "run:base-outcome",
                "artifact_type": "stage-outcome",
                "content": {"stage": "base-agents", "completed": 0, "retryable": True},
            },
        ]

        plan = self.planner.plan(artifacts, target="model-stages")

        self.assertFalse(plan.available)
        self.assertEqual(plan.reason, "unbound_agent_artifact_requires_review")

    def test_partial_base_success_plans_only_missing_agents_without_replaying_success(self) -> None:
        artifacts = [
            {
                "artifact_id": "run:business",
                "artifact_type": "agent-analysis",
                "agent_id": "business-analyst",
                "content": {"trusted_state": "completed_verified"},
            },
            {
                "artifact_id": "run:base-outcome",
                "artifact_type": "stage-outcome",
                "content": {
                    "agent_id": "financial-analyst",
                    "outcome": "waiting_retryable",
                    "retryable": True,
                },
            },
        ]

        plan = self.planner.plan(artifacts, target="model-stages")

        self.assertTrue(plan.available)
        self.assertIn("financial-analyst", plan.stages)
        self.assertIn("accounting-risk-analyst", plan.stages)
        self.assertNotIn("business-analyst", plan.stages)

    def test_growth_and_synthesis_plans_require_declared_dependencies(self) -> None:
        dossier = [{
            "artifact_id": "dossier", "artifact_type": "verified-research-dossier",
            "content": {"verified_analyses": {"financial-analyst": {"_verification_state": "completed_verified"}}},
        }]
        growth = self.planner.plan(dossier, target="growth")
        self.assertFalse(growth.available)
        ready = dossier + [
            {"artifact_id": "det", "artifact_type": "deterministic-financial-summary",
             "content": _deterministic_summary()},
        ]
        self.assertEqual(
            self.planner.plan(ready, target="growth").stages,
            ("growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis"),
        )
        complete = ready + [
            {"artifact_id": "growth", "artifact_type": "growth-opportunities", "content": {"opportunities": [{"title": "verified"}], "_validation": {"passed": True}}},
            {"artifact_id": "counter", "artifact_type": "counter-analysis", "content": {"_audit": {"verification": {"passed": True}}}},
            {"artifact_id": "forecast", "artifact_type": "forecast-scenarios", "content": {"_audit": {"verification": {"passed": True}}}},
        ]
        self.assertEqual(
            self.planner.plan(complete, target="synthesis").stages,
            ("final-synthesis",),
        )

    def test_counter_analysis_failure_plans_only_failed_node_and_downstream(self) -> None:
        artifacts = [
            *[
                {
                    "artifact_id": f"agent:{agent}",
                    "artifact_type": "agent-analysis",
                    "agent_id": agent,
                    "content": {"trusted_state": "completed_verified"},
                }
                for agent in (
                    "financial-analyst", "business-analyst", "accounting-risk-analyst",
                )
            ],
            {"artifact_id": "financial", "artifact_type": "deterministic-financial-summary",
             "content": _deterministic_summary()},
            {"artifact_id": "dossier", "artifact_type": "verified-research-dossier", "content": {"verified_analyses": {"financial-analyst": {"_verification_state": "completed_verified"}}}},
            {"artifact_id": "growth", "artifact_type": "growth-opportunities", "content": {"opportunities": [{"title": "verified"}], "_validation": {"passed": True}}},
            {
                "artifact_id": "counter-failure",
                "artifact_type": "stage-outcome",
                "content": {
                    "stage": "counterarguments",
                    "agent_id": "skeptical-analyst",
                    "outcome": "waiting_retryable",
                    "error_code": "MODEL_CONTEXT_CAPACITY",
                    "retryable": True,
                },
            },
        ]

        plan = self.planner.plan(artifacts)

        self.assertTrue(plan.available)
        self.assertEqual(plan.target, "counter-analysis")
        self.assertEqual(
            plan.stages,
            ("counter-analysis", "forecast-scenarios", "final-synthesis"),
        )

    def test_earlier_unverified_stage_becomes_minimum_recovery_target(self) -> None:
        artifacts = [
            {"artifact_id": "financial", "artifact_type": "deterministic-financial-summary",
             "content": _deterministic_summary()},
            {"artifact_id": "dossier", "artifact_type": "verified-research-dossier", "content": {"verified_analyses": {"business-analyst": {}}}},
            {"artifact_id": "growth-good", "artifact_type": "growth-opportunities", "content": {"opportunities": [{"title": "verified"}], "_validation": {"passed": True}}},
            {"artifact_id": "growth-new-failed", "artifact_type": "growth-opportunities", "content": {"opportunities": [], "_validation": {"passed": False}}},
            {"artifact_id": "counter-error", "artifact_type": "stage-outcome", "content": {"stage": "counterarguments", "retryable": True, "outcome": "waiting_retryable", "error_code": "MODEL_RESPONSE_INVALID"}},
        ]

        plan = self.planner.plan(artifacts)

        self.assertTrue(plan.available)
        self.assertEqual(plan.target, "growth")
        self.assertEqual(
            plan.stages,
            ("growth-opportunities", "counter-analysis", "forecast-scenarios", "final-synthesis"),
        )

    def test_internal_stage_failure_is_manually_resumable_without_replaying_successful_base(self) -> None:
        """Internal/checkpoint failures expose the same narrow manual resume target."""
        successful = [
            {
                "artifact_id": f"agent:{agent}",
                "artifact_type": "agent-analysis",
                "agent_id": agent,
                "content": {"trusted_state": "completed_verified"},
            }
            for agent in ("business-analyst", "accounting-risk-analyst")
        ]
        summary = {"artifact_id": "financial", "artifact_type": "deterministic-financial-summary",
                   "content": _deterministic_summary()}
        for failed in (
            {
                "artifact_id": "failed-stage",
                "artifact_type": "stage-outcome",
                "content": {"agent_id": "financial-analyst", "outcome": "needs_action",
                            "error_code": "STAGE_INTERNAL_ValueError", "retryable": False},
            },
            {
                "artifact_id": "failed-stage-internal",
                "artifact_type": "stage-outcome",
                "content": {"agent_id": "financial-analyst", "outcome": "failed_internal",
                            "error_code": "STAGE_INTERNAL_ValueError", "retryable": False},
            },
            {
                "artifact_id": "failed-checkpoint",
                "artifact_type": "stage-checkpoint",
                "content": {"result": {"stage_id": "financial-analyst",
                                           "error_code": "STAGE_INTERNAL_ValueError",
                                           "validation": {"passed": False}}},
            },
        ):
            with self.subTest(artifact_type=failed["artifact_type"],
                              outcome=failed["content"].get("outcome")):
                plan = self.planner.plan([summary, *successful, failed], target="model-stages")
                self.assertTrue(plan.available)
                self.assertEqual(plan.target, "model-stages")
                self.assertIn("financial-analyst", plan.stages)
                self.assertNotIn("business-analyst", plan.stages)
                self.assertNotIn("accounting-risk-analyst", plan.stages)
                description = self.planner.describe([summary, *successful, failed], target="model-stages")
                self.assertEqual(description["operations"][0]["code"], "resume_missing_analysis")

    def test_empty_deterministic_content_cannot_authorize_downstream_resume(self) -> None:
        artifacts = [{
            "artifact_id": "det-empty", "artifact_type": "deterministic-financial-summary",
            "content": {"company": {"cik": "0000123456", "ticker": "TEST"}, "evidence": []},
        }]
        plan = self.planner.plan(artifacts, target="growth")
        self.assertFalse(plan.available)
        self.assertEqual(plan.reason, "stage_capabilities_unavailable")


if __name__ == "__main__":
    unittest.main()
