from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from openthesis.service import AppService, PreferenceValidationError


class AppServiceTests(unittest.TestCase):
    def test_bootstrap_exposes_stable_contract_and_safe_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory), app_version="1.0.0-alpha.1")

            result = service.bootstrap()

            self.assertEqual(result["contract_version"], "1.0")
            self.assertEqual(result["app_version"], "1.0.0-alpha.1")
            self.assertEqual(result["preferences"]["ui_language"], "zh-CN")
            self.assertEqual(result["preferences"]["report_language"], "zh-CN")
            self.assertEqual(result["recent_runs"], [])

    def test_preferences_persist_only_allowlisted_non_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            service = AppService(data_dir)

            saved = service.update_preferences(
                {"ui_language": "en", "report_language": "zh-CN"}
            )

            self.assertEqual(saved["ui_language"], "en")
            self.assertEqual(AppService(data_dir).bootstrap()["preferences"], saved)
            with self.assertRaises(PreferenceValidationError):
                service.update_preferences({"api_key": "never-store-this"})
            self.assertEqual(service.storage.get_setting("api_key", ""), "")

    def test_unknown_report_has_a_stable_not_found_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research run not found"):
                service.get_report("missing")

    def test_demo_research_job_reports_progress_and_produces_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            started = service.start_research({"mode": "demo"})
            self.assertEqual(started["state"], "queued")
            self.assertNotIn("api_key", str(started))

            deadline = time.monotonic() + 5
            status = started
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "demo research timed out")
                time.sleep(0.01)
                status = service.get_research_status(started["job_id"])

            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["percent"], 100)
            self.assertTrue(status["run_id"])
            report = service.get_report(status["run_id"])
            self.assertIn("OpenThesis", report["markdown"])

    def test_unknown_research_job_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AppService(Path(directory))

            with self.assertRaisesRegex(KeyError, "research job not found"):
                service.get_research_status("missing")


if __name__ == "__main__":
    unittest.main()
