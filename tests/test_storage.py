from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openthesis.demo import DEMO_COMPANY, demo_facts
from openthesis.domain import FinancialFact, ResearchRun, RunStatus, utc_now_iso
from openthesis.storage import Storage


class StorageTests(unittest.TestCase):
    def test_round_trip_company_and_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            storage.save_facts([FinancialFact(**item) for item in demo_facts()])
            saved = storage.get_facts(DEMO_COMPANY.cik)
            self.assertEqual(len(saved), len(demo_facts()))
            self.assertEqual(saved[0]["company_cik"], DEMO_COMPANY.cik)

    def test_setting_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_setting("model", "example")
            self.assertEqual(storage.get_setting("model"), "example")
            self.assertEqual(storage.get_setting("missing", "fallback"), "fallback")

    def test_language_settings_are_independent_and_do_not_create_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.set_setting("ui_language", "en")
            storage.set_setting("report_language", "zh-CN")
            self.assertEqual(storage.get_setting("ui_language"), "en")
            self.assertEqual(storage.get_setting("report_language"), "zh-CN")
            self.assertEqual(storage.get_setting("api_key", ""), "")

    def test_running_runs_are_recovered_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            run = ResearchRun(
                run_id="interrupted-run",
                company=DEMO_COMPANY,
                workflow_id="test",
                research_pack_id="test",
                research_pack_version="1",
                provider_id="openai-compatible",
                model_id="test",
                data_as_of="2026-01-01T00:00:00+00:00",
                status=RunStatus.RUNNING,
            )
            storage.save_run(run)
            self.assertEqual(storage.interrupt_running_runs(), 1)
            recovered = storage.list_runs()[0]
            self.assertEqual(recovered["status"], RunStatus.CANCELLED.value)
            self.assertTrue(recovered["completed_at"])
            self.assertEqual(storage.interrupt_running_runs(), 0)

    def test_thesis_versions_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory))
            storage.save_company(DEMO_COMPANY)
            first = storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "first"},
                created_by="test",
                created_at=utc_now_iso(),
            )
            second = storage.save_thesis_version(
                DEMO_COMPANY.cik,
                {"thesis": "second"},
                created_by="user",
                created_at=utc_now_iso(),
            )
            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            versions = storage.list_thesis_versions(DEMO_COMPANY.cik)
            self.assertEqual(len(versions), 2)
            restored = storage.get_thesis_version(second["thesis_version_id"])
            self.assertIsNotNone(restored)
            self.assertEqual(restored["content"]["thesis"], "second")


if __name__ == "__main__":
    unittest.main()
