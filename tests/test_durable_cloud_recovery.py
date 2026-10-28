from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from openthesis.domain import ResearchRun, RunStatus
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.resilience.cloud_repository import CloudCandidateRepository
from openthesis.resilience.contracts import IncidentScope, Usage, RecoveryLimits, digest
from openthesis.resilience.repository import RecoveryConflict
from openthesis.storage import Storage
from openthesis.vision_financials import MineruFlashAdapter, VisionHttpResponse, VisionExtractionResult, parse_vision_markdown
from test_resilience_core import Clock
from test_vision_financials import QueueVisionTransport, _company, _config, _explicit_financial_markdown, _filing, _page


class CountingCompiler(FinancialFactCompiler):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def compile_facts(self, *args, **kwargs):
        self.calls += 1
        return super().compile_facts(*args, **kwargs)


class ScriptedAdapter:
    def __init__(self, producer):
        self.producer, self.calls = producer, []

    def extract(self, company, filing, pages, config, *, cancel_check=None,
                recovery_context=None, remote_task_id=""):
        self.calls.append(remote_task_id or "submit")
        return self.producer(recovery_context, remote_task_id)


class DurableCloudRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp")
        self.store = Storage(Path(self.temp.name))
        self.store.save_company(_company())
        self.store.save_run(ResearchRun("run", _company(), "wf", "pack", "1", "", "", "2026-10-04"))
        self.clock, self.compiler = Clock(), CountingCompiler()
        self.config = _config(provider="mineru_flash", configured_model_id="")
        self.page, self.filing = _page(), _filing()
        self.candidate = parse_vision_markdown(_explicit_financial_markdown("Revenue 1").decode("utf-8"),
            _company(), self.filing, (self.page,))
        self.assertTrue(self.candidate.facts)

    def tearDown(self):
        self.temp.cleanup()

    def wrapper(self, adapter, **kwargs):
        return DurableCloudVisionAdapter(adapter, self.store, "run", compiler=self.compiler,
            clock=self.clock, sleeper=self.clock.sleep, **kwargs)

    def extract(self, wrapper):
        return wrapper.extract(_company(), self.filing, (self.page,), self.config)

    def artifacts(self):
        return [item for item in self.store.get_artifacts("run") if item["artifact_type"] == "vision-candidate-set"]

    def test_known_pending_task_is_only_observed_and_reuse_revalidates_without_adapter_calls(self):
        def produce(context, task):
            context.consume(Usage(calls=1))
            if not task:
                context.record_external_intent()
                context.record_external_task("remote-task")
                return VisionExtractionResult(error_code="VISION_PENDING")
            self.assertEqual(task, "remote-task")
            return self.candidate

        adapter = ScriptedAdapter(produce)
        first = self.extract(self.wrapper(adapter))
        self.assertIsNone(first.error_code)
        self.assertEqual(adapter.calls, ["submit", "remote-task"])
        self.assertEqual(len(self.artifacts()), 1)
        count = self.compiler.calls
        reused = self.extract(self.wrapper(adapter))
        self.assertIsNone(reused.error_code)
        self.assertEqual(adapter.calls, ["submit", "remote-task"])
        self.assertGreater(self.compiler.calls, count)
        self.assertFalse(self.store.get_facts(_company().security_id))

    def test_unknown_post_outcome_never_creates_a_second_task(self):
        def uncertain(context, task):
            context.record_external_intent()
            return VisionExtractionResult(error_code="VISION_NETWORK_ERROR")

        adapter = ScriptedAdapter(uncertain)
        self.assertEqual(self.extract(self.wrapper(adapter)).error_code, "RECOVERY_SUBMISSION_UNKNOWN")
        self.extract(self.wrapper(adapter))
        self.assertEqual(adapter.calls, ["submit"])
        self.assertEqual(self.artifacts(), [])

    def test_wrong_page_candidate_is_blocked_before_commit(self):
        wrong = replace(self.candidate, facts=tuple(replace(fact, source_page=999) for fact in self.candidate.facts))
        result = self.extract(self.wrapper(ScriptedAdapter(lambda context, task: wrong)))
        self.assertEqual(result.error_code, "RECOVERY_CANDIDATE_REJECTED")
        self.assertEqual(self.artifacts(), [])

    def test_changed_amount_without_matching_source_literal_is_blocked(self):
        wrong = replace(self.candidate, facts=tuple(replace(fact, value=fact.value * 999) for fact in self.candidate.facts))
        result = self.extract(self.wrapper(ScriptedAdapter(lambda context, task: wrong)))
        self.assertEqual(result.error_code, "RECOVERY_CANDIDATE_REJECTED")
        self.assertEqual(self.artifacts(), [])

    def test_corrupt_saved_candidate_cannot_be_returned_as_success(self):
        adapter = ScriptedAdapter(lambda context, task: self.candidate)
        self.assertIsNone(self.extract(self.wrapper(adapter)).error_code)
        artifact = self.artifacts()[0]
        payload = artifact["content"]
        payload["result"]["facts"][0]["value"] *= 2
        with self.store.connect() as db:
            db.execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?",
                (json.dumps(payload), artifact["artifact_id"]))
        self.assertEqual(self.extract(self.wrapper(adapter)).error_code, "VISION_SAVED_CANDIDATE_INVALID")
        self.assertEqual(adapter.calls, ["submit"])

    def test_revoked_configuration_stops_before_any_cloud_request(self):
        adapter = ScriptedAdapter(lambda context, task: self.candidate)
        result = self.extract(self.wrapper(adapter, authorization_check=lambda: False))
        self.assertEqual(result.error_code, "RECOVERY_CANCELLED")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.artifacts(), [])

    def test_current_page_cap_applies_to_a_shared_incident_with_older_larger_limits(self):
        adapter = ScriptedAdapter(lambda context, task: self.candidate)
        scope = IncidentScope("financial-evidence", digest({"source_cohort": self.filing.content_hash}))
        wrapper = self.wrapper(adapter, budget_scope=scope, limits=RecoveryLimits(calls=32, pages=20))
        self.assertIsNone(self.extract(wrapper).error_code)
        second_page = replace(self.page, original_page=self.page.original_page + 1)
        result = wrapper.extract(_company(), self.filing, (second_page,), replace(self.config, max_pages=1))
        self.assertEqual(result.error_code, "RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
        self.assertEqual(adapter.calls, ["submit"])
        self.assertEqual(len(self.artifacts()), 1)

    def test_cancelled_run_stops_before_any_cloud_request(self):
        with self.store.connect() as db:
            db.execute("UPDATE research_runs SET status=? WHERE run_id=?", (RunStatus.CANCELLED, "run"))
        adapter = ScriptedAdapter(lambda context, task: self.candidate)
        self.assertEqual(self.extract(self.wrapper(adapter)).error_code, "RECOVERY_CANCELLED")
        self.assertEqual(adapter.calls, [])

    def test_old_generation_cannot_read_a_committed_candidate(self):
        repo = CloudCandidateRepository(self.store, "run", "page", digest({"source": "old"}),
            cancel_check=lambda: False, admit_candidate=lambda value: value)
        newer = CloudCandidateRepository(self.store, "run", "page", digest({"source": "new"}),
            cancel_check=lambda: False, admit_candidate=lambda value: value)
        self.assertGreater(newer.generation, repo.generation)
        with self.assertRaisesRegex(RecoveryConflict, "RECOVERY_PLAN_STALE"):
            repo.committed_result("unused")

    def test_real_adapter_pending_task_uses_one_creation_and_validated_result_cache(self):
        transport = QueueVisionTransport(
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "actual-task", "file_url": "https://upload.example/page"}}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, b'{"state":"running"}'),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {"markdown_url": "https://download.example/result.md"}}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        adapter = MineruFlashAdapter(transport, clock=self.clock, sleep=self.clock.sleep, journal=self.store)
        result = self.extract(self.wrapper(adapter))
        self.assertIsNone(result.error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET", "GET", "GET"])
        posted = json.loads(transport.calls[0]["body"])
        self.assertIs(posted["is_ocr"], True)
        self.assertIs(posted["enable_table"], True)
        count = self.compiler.calls
        self.assertIsNone(self.extract(self.wrapper(adapter)).error_code)
        self.assertEqual(len(transport.calls), 5)
        self.assertGreater(self.compiler.calls, count)

    def test_real_adapter_rate_limit_delay_is_honored_inside_same_incident(self):
        transport = QueueVisionTransport(
            VisionHttpResponse(429, b"rate limited", {"Retry-After": "7"}),
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "actual-task", "file_url": "https://upload.example/page"}}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {"markdown_url": "https://download.example/result.md"}}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        adapter = MineruFlashAdapter(transport, clock=self.clock, sleep=self.clock.sleep, journal=self.store)
        self.assertIsNone(self.extract(self.wrapper(adapter)).error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "POST", "PUT", "GET", "GET"])
        self.assertEqual(self.clock.now, 1007.0)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM recovery_incidents").fetchall()
        self.assertEqual(len(rows), 1)

    def test_waiting_restart_queries_saved_task_without_budget_reset(self):
        transport = QueueVisionTransport(
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "saved-task", "file_url": "https://upload.example/page"}}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, b'{"state":"running"}'),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {"markdown_url": "https://download.example/result.md"}}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        adapter = MineruFlashAdapter(transport, clock=self.clock, sleep=self.clock.sleep, journal=self.store)
        wrapper = self.wrapper(adapter, limits=RecoveryLimits(calls=32, pages=20))
        # Simulate process termination during the wait, after the durable task
        # id was written but before the next query. No HTTP request is in flight.
        def interrupt(_seconds):
            raise KeyboardInterrupt()
        wrapper.sleeper = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.extract(wrapper)
        self.clock.now += 31  # let the previous owner's lease expire
        self.store = Storage(Path(self.temp.name))
        adapter.journal = self.store
        self.assertIsNone(self.extract(self.wrapper(adapter)).error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET", "GET", "GET"])

    def test_valid_slow_request_does_not_outlive_its_own_recovery_lease(self):
        clock = self.clock

        class SlowCreation(QueueVisionTransport):
            def request(self, method, *args, **kwargs):
                response = super().request(method, *args, **kwargs)
                if method == "POST":
                    clock.now += 35
                return response

        transport = SlowCreation(
            VisionHttpResponse(200, json.dumps({"code": 0, "data": {"task_id": "slow-task", "file_url": "https://upload.example/page"}}).encode()),
            VisionHttpResponse(200),
            VisionHttpResponse(200, json.dumps({"state": "done", "data": {"markdown_url": "https://download.example/result.md"}}).encode()),
            VisionHttpResponse(200, _explicit_financial_markdown("Revenue 1")),
        )
        adapter = MineruFlashAdapter(transport, clock=clock, sleep=clock.sleep, journal=self.store)
        self.assertIsNone(self.extract(self.wrapper(adapter)).error_code)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "GET", "GET"])
