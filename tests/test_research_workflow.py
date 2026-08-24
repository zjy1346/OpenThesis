from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, RunStatus
from openthesis.ot import compile_studio_draft, minimal_studio_draft
from openthesis.packs import builtin_pack, load_pack
from openthesis.markets import build_company
from openthesis.providers import ModelConfig, ProviderError
from openthesis.research import ResearchCancelled, ResearchWorkflow
from openthesis.storage import Storage


def _valid_growth_output() -> dict[str, object]:
    return {
        "opportunities": [
            {
                "title": "Synthetic opportunity",
                "category": "product expansion",
                "mechanism": "The addressable market expands.",
                "evidence_grade": "C",
                "maturity_stage": "early",
                "time_horizon_years": 3,
                "probability_range": [0.3, 0.5],
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "scenario_eligibility": ["base"],
            }
        ]
    }


class DeterministicWorkflowTests(unittest.TestCase):
    def test_compiled_custom_ot_executes_its_own_dependency_graph(self) -> None:
        class OtProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.assertTrue(json_mode)
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                return {
                    "executive_summary": "Custom OT workflow output.",
                    "business_model": "Bounded by supplied evidence.",
                    "financial_quality": "Deterministic calculations remain separate.",
                    "competitive_position": "Requires additional evidence.",
                    "growth_opportunities": ["Evidence-bounded scenario"],
                    "counterarguments": ["The evidence set is synthetic."],
                    "scenarios": ["Continue monitoring."],
                    "thesis": "Custom OT thesis, not investment advice.",
                    "invalidation_conditions": ["Contradicting evidence appears."],
                    "leading_indicators": ["Evidence coverage"],
                    "unresolved_questions": ["How does broader evidence change the result?"],
                    "claims": [{
                        "text": "Further interpretation is required.",
                        "kind": "inference",
                        "confidence": 0.5,
                        "evidence_ids": [],
                    }],
                }

            @staticmethod
            def assertTrue(value: bool) -> None:
                if not value:
                    raise AssertionError("structured output must be enabled")

        raw, _ = compile_studio_draft(minimal_studio_draft())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "custom.ot"
            package_path.write_bytes(raw)
            storage = Storage(root / "data")
            storage.save_company(DEMO_COMPANY)
            provider = OtProvider()
            workflow = ResearchWorkflow(
                storage,
                load_pack(package_path),
                provider,
                ModelConfig(configured_model_id="test.ot", role="primary"),
                report_language="en",
                parallel_agents=True,
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.calls, ["company-analysis", "verification"])
            artifacts = storage.get_artifacts(run.run_id)
            ot_steps = [item for item in artifacts if item["artifact_type"] == "ot-agent-analysis"]
            self.assertEqual([item["content"]["step_id"] for item in ot_steps], provider.calls)
            report = next(item for item in artifacts if item["artifact_type"] == "research-report")
            self.assertEqual(report["content"]["mode"], "ot-workflow")
            self.assertEqual(report["content"]["workflow"]["pack_id"], "my.company-research")
            self.assertTrue(report["content"]["verification"]["passed"])
            self.assertEqual(run.research_configuration["ot_workflow"]["step_ids"], provider.calls)

    def test_growth_empty_response_retries_once_and_can_be_retried_in_isolation(self) -> None:
        class GrowthRetryProvider:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.growth_calls = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.assertTrue(json_mode)
                agent = str(json.loads(user_prompt).get("agent", ""))
                self.calls.append(agent)
                if agent == "growth-opportunity-analyst":
                    self.growth_calls += 1
                    if self.growth_calls <= 2:
                        return {
                            "opportunities": [],
                            "structured_output_valid": False,
                            "_response_error": "empty_content",
                        }
                    return {
                        "opportunities": [
                            {
                                "title": "New product platform",
                                "category": "product expansion",
                                "mechanism": "The addressable market expands.",
                                "evidence_grade": "C",
                                "maturity_stage": "early",
                                "time_horizon_years": 3,
                                "probability_range": [0.3, 0.5],
                                "supporting_evidence_ids": [],
                                "contradicting_evidence_ids": [],
                                "scenario_eligibility": ["base"],
                            }
                        ]
                    }
                return {
                    "executive_summary": "Synthetic verified research.",
                    "business_model": "Synthetic business model.",
                    "financial_quality": "Synthetic financial quality.",
                    "competitive_position": "Synthetic competitive position.",
                    "growth_opportunities": [],
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
            def assertTrue(value: bool) -> None:
                if not value:
                    raise AssertionError("structured output must be enabled")

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = GrowthRetryProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="en",
            )

            run = workflow.run(DEMO_COMPANY, demo_facts())
            self.assertEqual(provider.growth_calls, 2, "initial run gets one bounded growth retry")
            before = {agent: provider.calls.count(agent) for agent in set(provider.calls)}

            workflow.retry_growth(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )

            self.assertEqual(provider.growth_calls, 3)
            self.assertEqual(provider.calls.count("research-synthesizer"), before["research-synthesizer"] + 1)
            for agent in (
                "financial-analyst",
                "business-analyst",
                "accounting-risk-analyst",
                "skeptical-analyst",
                "forecast-analyst",
            ):
                self.assertEqual(provider.calls.count(agent), before[agent])
            latest_growth = next(
                artifact
                for artifact in reversed(storage.get_artifacts(run.run_id))
                if artifact["artifact_type"] == "growth-opportunities"
            )
            self.assertEqual(
                latest_growth["content"]["opportunities"][0]["title"],
                "New product platform",
            )

    def test_workflow_completes_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            facts = demo_facts()
            storage.save_facts([FinancialFact(**item) for item in facts])
            config = ModelConfig()
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
            config = ModelConfig()
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
                ModelConfig(),
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
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
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
            config = ModelConfig(configured_model_id="test.fake", role="primary")
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
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.count += 1
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if self.count == 7:
                    return {
                        "narrative": "",
                        "structured_output_valid": False,
                        "_response_error": "empty_content",
                    }
                # The workflow gets one bounded repair attempt (call 8), which
                # is deliberately still malformed; the explicit retry seam
                # below succeeds on call 9.
                if self.count == 9:
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
                ModelConfig(configured_model_id="test.fake", role="primary"),
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
            self.assertEqual(report["content"]["diagnostics"]["initial"]["parse_error_class"], "empty_content")
            self.assertEqual(report["content"]["diagnostics"]["repair"]["parse_error_class"], "invalid_schema")
            self.assertNotIn("prompt", report["content"]["diagnostics"])
            fallback = report["content"]["report"]
            required = {
                "executive_summary", "business_model", "financial_quality",
                "competitive_position", "growth_opportunities", "counterarguments",
                "scenarios", "thesis", "invalidation_conditions",
                "leading_indicators", "unresolved_questions", "claims",
            }
            self.assertTrue(required.issubset(fallback))
            self.assertTrue(fallback["claims"])
            self.assertNotIn("claims", fallback["business_model"])
            self.assertEqual(storage.list_thesis_versions(DEMO_COMPANY.cik), [])
            self.assertEqual(provider.count, 8, "run performs one bounded final repair call")

            retried = workflow.retry_synthesis(
                run,
                storage.get_artifacts(run.run_id),
                demo_facts(),
            )
            self.assertEqual(provider.count, 9, "bounded repair plus retry must make two final-only calls")
            self.assertEqual(retried.status, RunStatus.COMPLETED)
            retried_report = next(
                item
                for item in reversed(storage.get_artifacts(run.run_id))
                if item["artifact_type"] == "research-report"
            )
            self.assertEqual(retried_report["content"]["mode"], "synthesized")
            self.assertFalse(retried_report["content"]["retryable"])

    def test_repair_provider_error_keeps_completed_stages_partial(self) -> None:
        class RepairUnavailableProvider:
            def __init__(self) -> None:
                self.count = 0

            def test_connection(self) -> str:
                return "ok"

            def generate(self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> dict[str, object]:
                self.count += 1
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                if self.count == 7:
                    return {"narrative": "malformed", "structured_output_valid": False, "_response_error": "invalid_json"}
                if self.count == 8:
                    raise ProviderError("rate limited", retryable=False)
                return {"analysis": "stage", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RepairUnavailableProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
            )
            run = workflow.run(DEMO_COMPANY, demo_facts())
            self.assertEqual(run.status, RunStatus.PARTIAL)
            self.assertEqual(provider.count, 8)
            report = next(item for item in storage.get_artifacts(run.run_id) if item["artifact_type"] == "research-report")
            self.assertEqual(report["content"]["diagnostics"]["parse_error_class"], "provider_error")
            self.assertEqual(report["content"]["diagnostics"]["repair"]["parse_error_class"], "provider_error")

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
            config = ModelConfig(configured_model_id="test.failing", role="primary")
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
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {"executive_summary": "English output", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
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
            self.assertEqual(len(provider.calls), 8)
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

    def test_traditional_language_is_injected_into_every_agent_and_persisted(self) -> None:
        class RecordingProvider:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                self.calls.append((system_prompt, user_prompt))
                if json.loads(user_prompt).get("agent") == "growth-opportunity-analyst":
                    return _valid_growth_output()
                return {"executive_summary": "繁體中文輸出", "claims": []}

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RecordingProvider()
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                report_language="zh-Hant",
                ui_language="zh-Hant",
            )
            progress: list[str] = []
            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, _percent: progress.append(message),
            )
            self.assertEqual(run.report_language, "zh-Hant")
            self.assertEqual(len(provider.calls), 8)
            for system_prompt, user_prompt in provider.calls:
                self.assertIn(
                    "Write every natural-language value in Traditional Chinese",
                    system_prompt,
                )
                payload = json.loads(user_prompt)
                self.assertEqual(payload["output_language"], "zh-Hant")
                self.assertIn("Traditional Chinese", payload["output_language_instruction"])
                self.assertIn("research_context", payload)
            self.assertTrue(
                any("正在依序執行" in message or "研究完成" in message for message in progress)
            )
            saved = storage.get_run(run.run_id)
            self.assertIsNotNone(saved)
            payload = json.loads(saved["payload_json"])
            self.assertEqual(payload["report_language"], "zh-Hant")

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
                    ModelConfig(configured_model_id="test.fake", role="primary"),
                    parallel_agents=parallel,
                )
                workflow.run(DEMO_COMPANY, demo_facts())
                return provider.maximum

        self.assertEqual(run_with(False), 1)
        self.assertEqual(run_with(True), 2)

    def test_parallel_base_agent_retries_only_temporary_failure_sequentially(self) -> None:
        class RetryProvider:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.calls: dict[str, int] = {}
                self.lock = threading.Lock()

            def test_connection(self) -> str:
                return "ok"

            def generate(
                self, _system_prompt: str, user_prompt: str, *, json_mode: bool = True
            ) -> dict[str, object]:
                agent = str(json.loads(user_prompt)["agent"])
                with self.lock:
                    self.calls[agent] = self.calls.get(agent, 0) + 1
                    call_number = self.calls[agent]
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                threading.Event().wait(0.02)
                with self.lock:
                    self.active -= 1
                if agent == "financial-analyst" and call_number == 1:
                    raise ProviderError("temporary timeout", retryable=True)
                return {
                    "executive_summary": "Summary",
                    "business_model": "Business model",
                    "financial_quality": "Financial quality",
                    "competitive_position": "Competitive position",
                    "growth_opportunities": ["Opportunity"],
                    "counterarguments": ["Counterargument"],
                    "scenarios": ["Scenario"],
                    "thesis": "Thesis",
                    "invalidation_conditions": ["Invalidation"],
                    "leading_indicators": ["Indicator"],
                    "unresolved_questions": ["Question"],
                    "claims": [
                        {
                            "text": "Supported inference",
                            "kind": "inference",
                            "confidence": 0.8,
                            "evidence_ids": [],
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            provider = RetryProvider()
            progress: list[tuple[str, int]] = []
            workflow = ResearchWorkflow(
                storage,
                builtin_pack(),
                provider,
                ModelConfig(configured_model_id="test.fake", role="primary"),
                parallel_agents=True,
            )

            run = workflow.run(
                DEMO_COMPANY,
                demo_facts(),
                progress=lambda message, percent: progress.append((message, percent)),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.maximum, 2)
            self.assertEqual(provider.calls["financial-analyst"], 2)
            self.assertEqual(provider.calls["business-analyst"], 1)
            self.assertEqual(provider.calls["accounting-risk-analyst"], 1)
            self.assertTrue(any("单独重试" in message for message, _ in progress))

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
                ModelConfig(configured_model_id="test.fake", role="primary"),
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
