from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, RunStatus
from openthesis.packs import builtin_pack
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
