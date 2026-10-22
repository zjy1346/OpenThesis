from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig, ProviderError
from openthesis.research import ResearchWorkflow
from openthesis.storage import Storage


class BaseAgentParallelismTests(unittest.TestCase):
    def test_enabled_base_agents_overlap_after_disabled_workflow_shares_scheduler(self) -> None:
        class BarrierProvider:
            def __init__(self, base_agents: set[str]) -> None:
                self.base_agents = base_agents
                self.active = 0
                self.maximum = 0
                self.seen_base_agents: set[str] = set()
                self.barrier = threading.Barrier(2)
                self.barrier_broken = False
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                agent = str(json.loads(user_prompt).get("agent", ""))
                wait_at_barrier = False
                if agent in self.base_agents:
                    with self.lock:
                        self.active += 1
                        self.maximum = max(self.maximum, self.active)
                        if agent not in self.seen_base_agents and len(self.seen_base_agents) < 2:
                            self.seen_base_agents.add(agent)
                            wait_at_barrier = True
                    if wait_at_barrier:
                        try:
                            self.barrier.wait(timeout=3)
                        except threading.BrokenBarrierError:
                            self.barrier_broken = True
                    with self.lock:
                        self.active -= 1
                return {"kind": "opinion", "analysis": f"Analysis from {agent}.", "claims": []}

        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig(
                configured_model_id=f"test.base.parallel.311.{uuid.uuid4().hex}",
                role="primary",
            )
            disabled_first = ResearchWorkflow(
                storage, builtin_pack(), object(), config, parallel_agents=False,
            )
            base_agents = {
                spec.role for spec in disabled_first.stage_registry.stages if not spec.depends_on
            }
            provider = BarrierProvider(base_agents)
            enabled_second = ResearchWorkflow(
                storage, builtin_pack(), provider, config, parallel_agents=True,
            )

            self.assertIs(disabled_first.model_scheduler, enabled_second.model_scheduler)
            self.assertEqual(enabled_second.model_scheduler.limit, 2)
            enabled_second.run(DEMO_COMPANY, facts)

            self.assertEqual(len(provider.seen_base_agents), 2)
            self.assertGreaterEqual(provider.maximum, 2)
            self.assertFalse(provider.barrier_broken)

    def test_confirmed_429_reduces_shared_limit_and_resume_reuses_checkpoints(self) -> None:
        class RateLimitedProvider:
            def __init__(self, base_agents: set[str]) -> None:
                self.base_agents = base_agents
                self.active = 0
                self.maximum = 0
                self.seen_base_agents: set[str] = set()
                self.rate_limit_agent = ""
                self.rate_limit_count = 0
                self.barrier = threading.Barrier(2)
                self.calls_by_agent: dict[str, int] = {}
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                payload = json.loads(user_prompt)
                if "assertions" in payload:
                    return {"structured_output_valid": True, "results": [
                        {"assertion_id": item["assertion_id"], "state": "unresolved", "source_spans": []}
                        for item in payload["assertions"]
                    ]}

                agent = str(payload.get("agent", ""))
                wait_at_barrier = False
                with self.lock:
                    call_number = self.calls_by_agent.get(agent, 0) + 1
                    self.calls_by_agent[agent] = call_number
                    if agent in self.base_agents:
                        self.active += 1
                        self.maximum = max(self.maximum, self.active)
                        if agent not in self.seen_base_agents and len(self.seen_base_agents) < 2:
                            self.seen_base_agents.add(agent)
                            wait_at_barrier = True
                        if agent in self.base_agents and not self.rate_limit_agent:
                            self.rate_limit_agent = agent
                    should_reject = agent == self.rate_limit_agent and call_number == 1

                if wait_at_barrier:
                    self.barrier.wait(timeout=3)
                if should_reject:
                    self.rate_limit_count += 1
                    with self.lock:
                        if agent in self.base_agents:
                            self.active -= 1
                    raise ProviderError(
                        "confirmed fixture rate limit", retryable=True,
                        code="MODEL_RATE_LIMITED", http_status=429, retry_after_seconds=1,
                    )

                with self.lock:
                    if agent in self.base_agents:
                        self.active -= 1
                if agent == "growth-opportunity-analyst":
                    return {"opportunities": [{
                        "title": "Synthetic opportunity", "category": "product expansion",
                        "mechanism": "The addressable market expands.", "evidence_grade": "C",
                        "maturity_stage": "early", "time_horizon_years": 3,
                        "probability_range": [0.3, 0.5],
                        "supporting_evidence_ids": ["fact:808ac9481ae812762bdc728c"],
                        "contradicting_evidence_ids": [], "scenario_eligibility": ["base"],
                    }]}
                if agent == "research-synthesizer":
                    return {
                        "executive_summary": "Synthetic verified research summary.",
                        "business_model": "Synthetic business model.",
                        "financial_quality": "Synthetic financial quality.",
                        "balance_sheet": "Synthetic balance sheet.",
                        "competitive_position": "Synthetic competitive position.",
                        "growth_opportunities": ["Synthetic opportunity."],
                        "counterarguments": ["Synthetic counterargument."],
                        "scenarios": ["Synthetic scenario."],
                        "thesis": "Synthetic long-term thesis.",
                        "invalidation_conditions": ["Synthetic invalidation condition."],
                        "leading_indicators": ["Synthetic leading indicator."],
                        "unresolved_questions": ["Synthetic unresolved question."],
                        "implied_expectations": {"summary": "Valuation assumptions remain explicit."},
                        "claims": [{
                            "text": "Annual revenue is recorded in the official financial data.",
                            "kind": "fact", "concept": "revenue", "value": 4_200_000_000.0,
                            "unit": "USD", "currency": "USD", "fiscal_year": 2021,
                            "fiscal_period": "FY", "end_date": "2021-12-31",
                            "evidence_ids": ["fact:808ac9481ae812762bdc728c"],
                        }],
                    }
                return {"kind": "opinion", "analysis": f"Analysis from {agent}.", "claims": []}

        Path("D:/githubmax/tmp").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="D:/githubmax/tmp") as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig(
                configured_model_id=f"test.base.429.311.{uuid.uuid4().hex}", role="primary",
            )
            workflow = ResearchWorkflow(
                storage, builtin_pack(), None, config, parallel_agents=True,
            )
            provider = RateLimitedProvider({
                spec.role for spec in workflow.stage_registry.stages if not spec.depends_on
            })
            workflow.provider = provider
            rate_limit_observations: list[tuple[int, float]] = []
            original_mark_rate_limited = workflow.model_scheduler.mark_rate_limited

            def observe_rate_limit(delay: float | int | None = None) -> None:
                original_mark_rate_limited(delay)
                snapshot = workflow.model_scheduler.snapshot()
                rate_limit_observations.append((snapshot.limit, snapshot.cooldown_remaining_seconds))

            workflow.model_scheduler.mark_rate_limited = observe_rate_limit
            evidence = [{
                "evidence_id": f"filing:{topic}", "document_id": "demo-annual",
                "source_url": "https://example.test/annual", "title": topic,
                "excerpt": text,
            } for topic, text in (
                ("business", "The company sells equipment to industrial customers."),
                ("management_discussion", "Management plans new products and capacity investment."),
                ("risk_factors", "Customer concentration and refinancing expose the business to risk."),
                ("audit", "Independent auditor reports an unqualified opinion on the consolidated statements."),
            )]

            run = workflow.run(DEMO_COMPANY, facts, filing_evidence=evidence)

            self.assertEqual(len(provider.seen_base_agents), 2)
            self.assertGreaterEqual(provider.maximum, 2)
            self.assertEqual(provider.rate_limit_count, 1)
            self.assertEqual(len(rate_limit_observations), 1)
            self.assertEqual(rate_limit_observations[0][0], 1)
            self.assertGreaterEqual(rate_limit_observations[0][1], 1)
            self.assertEqual(provider.calls_by_agent[provider.rate_limit_agent], 2)
            artifacts = storage.get_artifacts(run.run_id)
            completed_stage_ids = {
                item["content"]["result"]["stage_id"]
                for item in artifacts
                if item.get("artifact_type") == "stage-checkpoint"
                and isinstance(item.get("content"), dict)
                and isinstance(item["content"].get("result"), dict)
            }
            completed_roles = {
                workflow.stage_registry.get(stage_id).role for stage_id in completed_stage_ids
            }
            self.assertEqual(
                completed_stage_ids,
                {spec.stage_id for spec in workflow.stage_registry.stages},
            )
            calls_before_resume = dict(provider.calls_by_agent)

            workflow.run(
                DEMO_COMPANY, facts, filing_evidence=evidence,
                existing_run=run, resume_artifacts=artifacts, resume_target="model-stages",
            )

            for role in completed_roles:
                self.assertEqual(provider.calls_by_agent.get(role, 0), calls_before_resume.get(role, 0))
            self.assertEqual(provider.calls_by_agent, calls_before_resume)


if __name__ == "__main__":
    unittest.main()
