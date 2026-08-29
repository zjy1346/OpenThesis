from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from openthesis.ot import minimal_studio_draft
from openthesis.service import AppService, _FinancialReportRefreshError
from openthesis.sidecar import JsonLineServer


class JsonLineServerTests(unittest.TestCase):
    def test_sidecar_spec_declares_windows_version_resource(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spec = (root / "OpenThesisSidecar.spec").read_text(encoding="utf-8")
        version = root / "OpenThesisSidecar.version.txt"
        self.assertTrue(version.is_file())
        version_text = version.read_text(encoding="utf-8")
        self.assertIn('version=str(version_file)', spec)
        self.assertIn('StringStruct("CompanyName"', version_text)
        self.assertIn('StringStruct("ProductName"', version_text)
        self.assertIn('StringStruct("FileVersion"', version_text)

    def test_retry_financials_method_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = JsonLineServer(AppService(Path(directory)))
            with self.assertRaises(Exception):
                server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "research.retry_financials", "params": {"run_id": "missing"}})
    def test_hello_uses_versioned_json_rpc_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            server = JsonLineServer(AppService(Path(directory)))

            server.serve(
                io.StringIO(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "request-1",
                            "method": "system.hello",
                            "params": {},
                        }
                    )
                    + "\n"
                ),
                output,
            )

            response = json.loads(output.getvalue())
            self.assertEqual(response["jsonrpc"], "2.0")
            self.assertEqual(response["id"], "request-1")
            self.assertEqual(response["result"]["contract_version"], "2.0")

    def test_errors_do_not_echo_secret_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            server = JsonLineServer(AppService(Path(directory)))

            server.serve(
                io.StringIO(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": "settings.update",
                            "params": {"preferences": {"api_key": "sk-private-value"}},
                        }
                    )
                    + "\n"
                ),
                output,
            )

            response_text = output.getvalue()
            response = json.loads(response_text)
            self.assertNotIn("sk-private-value", response_text)
            self.assertEqual(response["error"]["code"], -32602)

    def test_research_job_methods_share_the_same_sidecar_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = JsonLineServer(AppService(Path(directory)))

            started = server.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "research.start",
                    "params": {"mode": "demo"},
                }
            )
            status = server.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "research.status",
                    "params": {"job_id": started["job_id"]},
                }
            )

            self.assertEqual(status["job_id"], started["job_id"])
            self.assertIn(status["state"], {"queued", "running", "completed"})
            deadline = time.monotonic() + 5
            while status["state"] not in {"completed", "failed", "cancelled"}:
                self.assertLess(time.monotonic(), deadline, "demo research timed out")
                time.sleep(0.01)
                status = server.dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "research.status",
                        "params": {"job_id": started["job_id"]},
                    }
                )

    def test_retry_financials_is_exposed_without_model_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class Service(AppService):
                def retry_financials(self, run_id: str):
                    self.seen_run_id = run_id
                    return {"run_id": run_id, "model_called": False}

            service = Service(Path(directory))
            self.assertIn("research.retry_financials", service.hello()["capabilities"])
            result = JsonLineServer(service).dispatch({
                "jsonrpc": "2.0", "id": 11, "method": "research.retry_financials",
                "params": {"run_id": "run-1"},
            })
            self.assertEqual(result["run_id"], "run-1")
            self.assertFalse(result["model_called"])

    def test_refresh_financial_report_is_exposed_as_a_separate_deterministic_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class Service(AppService):
                def refresh_financial_report(self, run_id: str, *, language: str | None = None):
                    self.seen = (run_id, language)
                    return {"run_id": run_id, "financial_report_refresh": {"status": "succeeded"}}

            service = Service(Path(directory))
            self.assertIn("research.refresh_financial_report", service.hello()["capabilities"])
            result = JsonLineServer(service).dispatch({
                "jsonrpc": "2.0", "id": 14,
                "method": "research.refresh_financial_report",
                "params": {"run_id": "run-1", "language": "en"},
            })
            self.assertEqual(service.seen, ("run-1", "en"))
            self.assertEqual(result["financial_report_refresh"]["status"], "succeeded")

    def test_refresh_financial_report_failure_keeps_stable_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class Service(AppService):
                def refresh_financial_report(self, run_id: str, *, language: str | None = None):
                    raise _FinancialReportRefreshError({
                        "status": "failed", "error": "FILING_REPORT_REFRESH_FAILED",
                    })

            response = JsonLineServer(Service(Path(directory)))._handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 15,
                "method": "research.refresh_financial_report",
                "params": {"run_id": "run-1"},
            }))
            self.assertEqual(response["error"]["code"], -32020)
            self.assertEqual(response["error"]["message"], "FILING_REPORT_REFRESH_FAILED")

    def test_rebuild_financials_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            class Service(AppService):
                def rebuild_financials(self, run_id: str, *, confirmed: bool = False):
                    self.seen = (run_id, confirmed)
                    return {"run_id": run_id, "confirmed": confirmed}

            service = Service(Path(directory))
            self.assertIn("research.rebuild_financials", service.hello()["capabilities"])
            with self.assertRaises(Exception):
                JsonLineServer(service).dispatch({
                    "jsonrpc": "2.0", "id": 12,
                    "method": "research.rebuild_financials",
                    "params": {"run_id": "run-1"},
                })
            result = JsonLineServer(service).dispatch({
                "jsonrpc": "2.0", "id": 13,
                "method": "research.rebuild_financials",
                "params": {"run_id": "run-1", "confirmed": True},
            })
            self.assertEqual(service.seen, ("run-1", True))
            self.assertTrue(result["confirmed"])

    def test_ot_validation_is_available_over_json_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = JsonLineServer(AppService(Path(directory)))
            result = server.dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "ot.validate",
                    "params": {"draft": minimal_studio_draft()},
                }
            )
            self.assertTrue(result["valid"])
            with self.assertRaises(Exception):
                server.dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "models.catalog",
                        "params": {},
                    }
                )

if __name__ == "__main__":
    unittest.main()
