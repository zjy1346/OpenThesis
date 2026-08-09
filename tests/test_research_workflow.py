from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, RunStatus
from openthesis.packs import builtin_pack
from openthesis.markets import build_company
from openthesis.providers import ModelConfig
from openthesis.research import ResearchCancelled, ResearchWorkflow
from openthesis.storage import Storage


class DeterministicWorkflowTests(unittest.TestCase):
    def test_workflow_completes_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig(provider="none", model="", base_url="")
            workflow = ResearchWorkflow(storage, builtin_pack(), None, config)
            run = workflow.run(DEMO_COMPANY, facts)
            self.assertEqual(run.status, RunStatus.PARTIAL)
            artifacts = storage.get_artifacts(run.run_id)
            self.assertEqual(len(artifacts), 2)
            self.assertEqual(artifacts[-1]["artifact_type"], "research-report")

    def test_financial_beta_skips_standard_reverse_dcf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company("600036.SH", "招商银行")
            storage.save_company(company)
            config = ModelConfig(provider="none", model="", base_url="")
            workflow = ResearchWorkflow(storage, builtin_pack(), None, config)

            run = workflow.run(
                company,
                demo_facts(),
                valuation_inputs={"market_cap": 1_000_000_000, "discount_rate": 0.1, "terminal_growth": 0.03},
                market_snapshot={"source": "manual", "market_cap": 1_000_000_000, "currency": "CNY", "as_of": "2026-08-09"},
            )

            valuation = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "deterministic-valuation"
            )
            self.assertEqual(valuation["content"]["status"], "not_applicable")
            self.assertEqual(company.industry_support, "financial_beta")

    def test_currency_mismatch_is_not_silently_valued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            company = build_company(
                "00700.HK",
                "Tencent",
                reporting_currency="CNY",
                accounting_standard="IFRS",
            )
            storage.save_company(company)
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                None,
                ModelConfig(provider="none", model="", base_url=""),
            )

            run = workflow.run(
                company,
                demo_facts(),
                valuation_inputs={"market_cap": 1_000_000_000, "discount_rate": 0.1, "terminal_growth": 0.03},
                market_snapshot={"source": "manual", "market_cap": 1_000_000_000, "currency": "HKD", "as_of": "2026-08-09"},
            )

            valuation = next(
                item for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "deterministic-valuation"
            )
            self.assertEqual(valuation["content"]["status"], "currency_mismatch")

    def test_multi_agent_workflow_with_fake_provider(self) -> None:
        class FakeProvider:
            def __init__(self) -> None:
                self.count = 0
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                with self.lock:
                    self.count += 1
                self.assertions(system_prompt, user_prompt, json_mode)
                return {
                    "executive_summary": "Synthetic verified research.",
                    "business_model": "Synthetic business model.",
                    "financial_quality": "Synthetic financial quality.",
                    "competitive_position": "Synthetic competitive position.",
                    "growth_opportunities": ["Synthetic opportunity."],
                    "counterarguments": ["Synthetic counterargument."],
                    "scenarios": ["Synthetic scenario."],
                    "thesis": "Synthetic thesis.",
                    "invalidation_conditions": ["Synthetic invalidation condition."],
                    "leading_indicators": ["Synthetic leading indicator."],
                    "unresolved_questions": ["Synthetic unresolved question."],
                    "claims": [
                        {
                            "text": "The supplied data needs further interpretation.",
                            "kind": "inference",
                            "evidence_ids": [],
                        }
                    ],
                }

            @staticmethod
            def assertions(system_prompt: str, user_prompt: str, json_mode: bool) -> None:
                if "Never invent" not in system_prompt or "research_context" not in user_prompt:
                    raise AssertionError("Research prompts lost their evidence policy")
                if not json_mode:
                    raise AssertionError("Structured output must be enabled")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            provider = FakeProvider()
            config = ModelConfig(
                provider="openai-compatible",
                model="fake",
                base_url="https://example.test/v1",
            )
            workflow = ResearchWorkflow(storage, builtin_pack(), provider, config)
            progress: list[tuple[str, int]] = []
            run = workflow.run(
                DEMO_COMPANY,
                facts,
                progress=lambda message, percent: progress.append((message, percent)),
            )
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.count, 7)
            messages = [message for message, _ in progress]
            self.assertTrue(
                any("Agent 已完成 1/3" in message for message in messages)
            )
            self.assertIn("正在合成最终长期研究报告", messages)
            self.assertEqual(progress[-1], ("研究完成", 100))
            artifacts = storage.get_artifacts(run.run_id)
            self.assertEqual(len(artifacts), 10)
            theses = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual(len(theses), 1)

    def test_empty_final_synthesis_is_partial_and_preserves_stage_outputs(self) -> None:
        class EmptyFinalProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                if self.count == 7:
                    return {
                        "narrative": "",
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                    }
                if self.count == 8:
                    return {
                        "executive_summary": "Recovered synthesis.",
                        "business_model": "Business model.",
                        "financial_quality": "Financial quality.",
                        "competitive_position": "Competitive position.",
                        "growth_opportunities": ["Opportunity."],
                        "counterarguments": ["Counterargument."],
                        "scenarios": ["Scenario."],
                        "thesis": "Thesis.",
                        "invalidation_conditions": ["Invalidation."],
                        "leading_indicators": ["Indicator."],
                        "unresolved_questions": ["Question."],
                        "claims": [
                            {
                                "text": "Recovered inference",
                                "kind": "inference",
                                "evidence_ids": [],
                            }
                        ],
                    }
                return {
                    "analysis": f"stage {self.count}",
                    "claims": [
                        {
                            "text": "stage inference",
                            "kind": "inference",
                            "evidence_ids": [],
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = EmptyFinalProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(
                    provider="openai-compatible",
                    model="fake",
                    base_url="https://example.test/v1",
                ),
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())

            self.assertEqual(run.status, RunStatus.PARTIAL)
            report = next(
                item
                for item in storage.get_artifacts(run.run_id)
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(report["content"]["mode"], "staged-fallback")
            self.assertTrue(report["content"]["retryable"])
            self.assertIn("business_model", report["content"]["report"])
            self.assertEqual(storage.list_thesis_versions(DEMO_COMPANY.cik), [])

            retried = workflow.retry_synthesis(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )
            self.assertEqual(provider.count, 8, "retry must make exactly one model call")
            self.assertEqual(retried.status, RunStatus.COMPLETED)
            retried_report = next(
                item
                for item in reversed(storage.get_artifacts(run.run_id))
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(retried_report["content"]["mode"], "synthesized")
            self.assertFalse(retried_report["content"]["retryable"])

    def test_failed_provider_persists_failed_run(self) -> None:
        class FailingProvider:
            def test_connection(self) -> str:
                return "never"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                raise RuntimeError("intentional provider failure")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            config = ModelConfig(
                provider="openai-compatible",
                model="failing",
                base_url="https://example.test/v1",
            )
            workflow = ResearchWorkflow(
                storage, builtin_pack(), FailingProvider(), config
            )
            with self.assertRaisesRegex(RuntimeError, "intentional provider failure"):
                workflow.run(DEMO_COMPANY, facts)
            runs = storage.list_runs()
            self.assertEqual(runs[0]["status"], RunStatus.FAILED.value)

    def test_english_language_is_injected_into_every_agent(self) -> None:
        class RecordingProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.calls.append((system_prompt, user_prompt))
                return {"executive_summary": "English output", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(
                    provider="openai-compatible",
                    model="fake",
                    base_url="https://example.test/v1",
                ),
                report_language="en",
                ui_language="en",
            )
            progress: list[str] = []
            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "en")
            self.assertEqual(len(provider.calls), 7)
            for system_prompt, user_prompt in provider.calls:
                self.assertIn(
                    "Write every natural-language value in English",
                    system_prompt,
                )
                payload = json.loads(user_prompt)
                self.assertEqual(payload["output_language"], "en")
                self.assertIn("English", payload["output_language_instruction"])
                self.assertIn("research_context", payload)
            self.assertTrue(any("Synthesizing" in message for message in progress))
            saved = storage.get_run(run.run_id)
            self.assertIsNotNone(saved)
            payload = json.loads(saved["payload_json"])
            self.assertEqual(payload["report_language"], "en")

    def test_parallel_agent_switch_controls_concurrency(self) -> None:
        class ConcurrencyProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, _user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                threading.Event().wait(0.03)
                with self.lock:
                    self.active -= 1
                return {"claims": []}

        def run_with(parallel: bool) -> int:
            with tempfile.TemporaryDirectory() as directory:
                provider = ConcurrencyProvider()
                storage = Storage(Path(directory))
                storage.save_company(DEMO_COMPANY)
                storage.save_facts([FinancialFact(**item) for item in demo_facts()])
                workflow = ResearchWorkflow(
                    storage,
                    builtin_pack(),
                    provider,
                    ModelConfig(
                        provider="openai-compatible",
                        model="fake",
                        base_url="https://example.test/v1",
                    ),
                    parallel_agents=parallel,
                )
                workflow.run(DEMO_COMPANY, demo_facts())
                return provider.maximum

        self.assertEqual(run_with(False), 1)
        self.assertGreaterEqual(run_with(True), 2)

    def test_cancellation_is_persisted_without_calling_provider(self) -> None:
        class CountingProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                return {}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = CountingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(
                    provider="openai-compatible",
                    model="fake",
                    base_url="https://example.test/v1",
                ),
                cancel_check=lambda: True,
            )
            with self.assertRaises(ResearchCancelled) as caught:
                workflow.run(DEMO_COMPANY, demo_facts())
            self.assertTrue(caught.exception.run_id)
            self.assertEqual(provider.count, 0)
            self.assertEqual(
                storage.list_runs()[0]["status"], RunStatus.CANCELLED.value
            )


if __name__ == "__main__":
    unittest.main()
