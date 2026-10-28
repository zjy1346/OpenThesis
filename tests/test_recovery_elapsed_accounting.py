from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from openthesis.domain import Company, ResearchRun
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, Usage, VerificationResult, digest,
)
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryRepository
from openthesis.resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from openthesis.storage import Storage
from openthesis.vision_financials import (
    MineruFlashAdapter, VisionHttpResponse,
)
from test_resilience_core import Clock
from test_vision_financials import (
    QueueVisionTransport, _company, _config, _explicit_financial_markdown,
    _filing, _page,
)


class MeasuredClock(Clock):
    def __init__(self):
        super().__init__()
        self.sleep_seconds = 0.0
        self.transport_seconds = 0.0
        self.work_seconds = 0.0

    def sleep(self, seconds):
        self.sleep_seconds += seconds
        super().sleep(seconds)

    def transport(self, seconds):
        self.transport_seconds += seconds
        self.now += seconds

    def work(self, seconds):
        self.work_seconds += seconds
        self.now += seconds


class TimedQueueTransport(QueueVisionTransport):
    def __init__(self, clock, step, *responses):
        super().__init__(*responses)
        self.clock = clock
        self.step = step

    def request(self, method, url, *, headers=None, body=None, timeout=60.0):
        response = super().request(method, url, headers=headers, body=body, timeout=timeout)
        self.clock.transport(self.step)
        return response


class RecoveryElapsedAccountingTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "tmp"
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = Storage(Path(self.temp.name))
        self.clock = MeasuredClock()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _incident_usage(store):
        with store.connect() as db:
            rows = db.execute("SELECT usage_json FROM recovery_incidents").fetchall()
        if len(rows) != 1:
            raise AssertionError(f"expected one recovery incident, got {len(rows)}")
        return json.loads(rows[0]["usage_json"])

    def _prepare_company_run(self):
        company = _company()
        self.store.save_company(company)
        self.store.save_run(ResearchRun("run", company, "wf", "pack", "1", "", "", "2026-10-04"))
        return company

    def test_six_flash_http_requests_settle_short_elapsed_time_and_commit(self):
        company = self._prepare_company_run()
        filing, page = _filing(), _page()
        responses = (
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {
                "task_id": "elapsed-task", "file_url": "https://upload.example/page",
            }}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, b'{"state":"running"}'),
            VisionHttpResponse(200, b'{"state":"running"}'),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {
                "markdown_url": "https://download.example/result.md",
            }}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        transport = TimedQueueTransport(self.clock, 0.25, *responses)
        adapter = MineruFlashAdapter(transport, clock=self.clock, sleep=self.clock.sleep, journal=self.store)
        config = _config(provider="mineru_flash", configured_model_id="", timeout_seconds=60.0)
        wrapper = DurableCloudVisionAdapter(
            adapter, self.store, "run", clock=self.clock, sleeper=self.clock.sleep,
            limits=RecoveryLimits(seconds=180, calls=32, pages=20),
        )
        started = self.clock.now

        result = wrapper.extract(company, filing, (page,), config)

        self.assertIsNone(result.error_code)
        self.assertTrue(result.facts)
        self.assertEqual([call["method"] for call in transport.calls],
                         ["POST", "PUT", "GET", "GET", "GET", "GET"])
        usage = self._incident_usage(self.store)
        elapsed = self.clock.now - started
        self.assertGreater(self.clock.sleep_seconds, 0)
        self.assertEqual(self.clock.transport_seconds, 1.5)
        self.assertAlmostEqual(usage["seconds"], self.clock.sleep_seconds + self.clock.transport_seconds)
        self.assertAlmostEqual(usage["seconds"], elapsed)

    def test_actual_elapsed_budget_exhausts_without_publication(self):
        company = Company("issuer", "EX", "Example")
        self.store.save_company(company)
        self.store.save_run(ResearchRun("run", company, "wf", "pack", "1", "", "", "2026-10-04"))
        input_digest = digest({"source": "frozen"})
        issue = Issue("SOURCE_MISSING", "source", "financials", "run", input_digest)
        self.published = []

        def read_snapshot(db, run_id):
            row = db.execute("SELECT generation FROM latest_report_revisions WHERE run_id=?", (run_id,)).fetchone()
            return (int(row["generation"]) if row else 0), input_digest

        def publish(db, plan, payload):
            result = self.store._append_report_revision_db(
                db, plan.run_id, payload, expected_generation=plan.expected_generation,
            )
            self.published.append(payload)
            return result

        repository = RecoveryRepository(self.store, read_snapshot, publish)
        calls = []

        def produce(_context):
            calls.append(len(calls) + 1)
            self.clock.work(2 if len(calls) == 1 else 1)
            raise RecoveryError("SLOW_TRANSIENT", transient=True,
                                retry_after=12 if len(calls) == 1 else 6)

        registry = StrategyRegistry()
        registry.register(Strategy("slow", "1", "verifier", produce,
            lambda _candidate, _plan: VerificationResult(False), lambda _plan, _action: True))
        action = RecoveryAction("slow", "1", ("document",), input_digest, "verifier")
        plan = RecoveryPlan("incident-slow", "run", "financials", 0, input_digest,
            (issue,), (action,), limits=RecoveryLimits(seconds=14, transient_retries=2))
        coordinator = RecoveryCoordinator(repository, registry,
            clock=self.clock, sleeper=self.clock.sleep)

        outcome = coordinator.execute(plan, expected_generation=0, plan_hash=plan.plan_hash,
                                      authorization=AuthorizationScope())

        self.assertEqual(outcome["state"], RecoveryState.EXHAUSTED)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.clock.sleep_seconds, 12)
        self.assertAlmostEqual(outcome["usage"]["seconds"], 15)
        self.assertEqual(self.published, [])
        self.assertIsNone(self.store.latest_report_revision("run"))

    def test_unknown_flash_post_is_not_retried_after_restart_and_keeps_call_page_charges(self):
        company = self._prepare_company_run()
        filing, page = _filing(), _page()

        class UnknownPostTransport:
            def __init__(self, clock):
                self.clock = clock
                self.calls = []

            def request(self, method, url, *, headers=None, body=None, timeout=60.0):
                self.calls.append({"method": method, "url": url})
                self.clock.transport(1.25)
                if method == "POST":
                    raise TimeoutError("connection closed after submit")
                raise AssertionError("unknown submission must not progress to another HTTP request")

        transport = UnknownPostTransport(self.clock)
        config = _config(provider="mineru_flash", configured_model_id="", timeout_seconds=60.0)

        def extract(store):
            adapter = MineruFlashAdapter(transport, clock=self.clock, sleep=self.clock.sleep, journal=store)
            wrapper = DurableCloudVisionAdapter(
                adapter, store, "run", clock=self.clock, sleeper=self.clock.sleep,
                limits=RecoveryLimits(seconds=180, calls=32, pages=20),
            )
            return wrapper.extract(company, filing, (page,), config)

        first = extract(self.store)
        self.assertEqual(first.error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        before_restart = self._incident_usage(self.store)
        self.assertGreater(before_restart["calls"], 0)
        self.assertEqual(before_restart["pages"], 1)
        self.assertEqual(len(transport.calls), 1)

        restarted_store = Storage(Path(self.temp.name))
        resumed = extract(restarted_store)
        after_restart = self._incident_usage(restarted_store)

        self.assertTrue(resumed.error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])
        self.assertEqual(after_restart["calls"], before_restart["calls"])
        self.assertEqual(after_restart["pages"], before_restart["pages"])


if __name__ == "__main__":
    unittest.main()
