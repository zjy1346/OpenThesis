from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from openthesis.ot import minimal_studio_draft
from openthesis.service import AppService
from openthesis.sidecar import JsonLineServer


class JsonLineServerTests(unittest.TestCase):
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
