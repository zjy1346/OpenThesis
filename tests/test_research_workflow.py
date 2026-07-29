from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, RunStatus
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis.research import ResearchWorkflow
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
            run = workflow.run(DEMO_COMPANY, facts)
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(provider.count, 7)
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


if __name__ == "__main__":
    unittest.main()
