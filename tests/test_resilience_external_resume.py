from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from openthesis.domain import Company, ResearchRun
from openthesis.resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, Usage, VerificationResult, digest,
)
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryRepository
from openthesis.resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from openthesis.storage import Storage
from test_resilience_core import Clock


class ExternalTaskResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tmp")
        self.store = Storage(Path(self.temp.name))
        company = Company("issuer", "EX", "Example")
        self.store.save_company(company)
        self.store.save_run(ResearchRun("run", company, "wf", "pack", "1", "", "", "2026-10-04"))
        self.input_digest = digest({"document": "fixed", "page": 12})
        self.clock = Clock()
        self.published, self.calls = [], []
        self.repository = RecoveryRepository(self.store, lambda db, run: (0, self.input_digest),
            lambda db, plan, payload: self.published.append(payload) or {"revision_id": "verified-page"})
        self.registry = StrategyRegistry()
        self.scope = AuthorizationScope(("free-cloud",), ("doc:12",), ("ocr-v3",))
        self.issue = Issue("PAGE_UNREADABLE", "document", "page", "run", self.input_digest)
        self.action = RecoveryAction("free-ocr", "v3", ("doc:12",), self.input_digest,
            "financial-source-verifier", provider_id="free-cloud", page_ids=("doc:12",),
            configuration_fingerprint="ocr-v3", estimated_usage=Usage(calls=4, pages=1, seconds=10),
            external_submission=True)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, *, calls=10):
        return RecoveryPlan("incident", "run", "page:12", 0, self.input_digest,
            (self.issue,), (self.action,), limits=RecoveryLimits(calls=calls, pages=1),
            authorization_scope=self.scope)

    def execute(self, plan):
        return RecoveryCoordinator(self.repository, self.registry, clock=self.clock,
            sleeper=self.clock.sleep).execute(plan, expected_generation=0,
                plan_hash=plan.plan_hash, authorization=self.scope)

    def register(self, *, known=True, pending=False, submit_seconds=0.0, observe_seconds=0.0):
        def submit(context):
            self.calls.append("POST")
            context.consume(Usage(calls=1, pages=1))
            # The repository settles wall time from the clock at action
            # completion; do not also put the same duration in Usage.seconds.
            self.clock.now += submit_seconds
            context.record_external_intent()
            if known:
                context.record_external_task("remote-task")
            raise RecoveryError("REMOTE_PENDING", waiting_external=known)

        def observe(context, task_id):
            self.assertEqual(task_id, "remote-task")
            self.calls.append("GET")
            context.consume(Usage(calls=1))
            self.clock.now += observe_seconds
            if pending:
                raise RecoveryError("REMOTE_PENDING", waiting_external=True)
            return CandidateResult({"source": "doc:12", "value": 10})

        self.registry.register(Strategy("free-ocr", "v3", "financial-source-verifier", submit,
            lambda candidate, plan: VerificationResult(True, (self.issue.issue_id,)),
            lambda plan, action: True, resume_external=observe,
            observation_usage=Usage(calls=2, seconds=10)))

    def test_restart_queries_known_task_without_reupload_or_page_budget_reset(self):
        self.register()
        plan = self.plan()
        self.assertEqual(self.execute(plan)["state"], RecoveryState.WAITING_EXTERNAL)
        self.repository = RecoveryRepository(Storage(Path(self.temp.name)),
            lambda db, run: (0, self.input_digest),
            lambda db, plan, payload: self.published.append(payload) or {"revision_id": "verified-page"})
        self.assertEqual(self.execute(plan)["state"], RecoveryState.COMMITTED)
        status = self.repository.status(plan.plan_id)
        self.assertEqual(self.calls, ["POST", "GET"])
        self.assertEqual(status["action_count"], 2)
        self.assertEqual(status["usage"]["pages"], 1)
        self.assertEqual(status["usage"]["calls"], 6)
        self.assertEqual(len(self.published), 1)

    def test_unknown_submission_cannot_be_reposted_or_observed(self):
        self.register(known=False)
        plan = self.plan()
        self.assertEqual(self.execute(plan)["diagnostic_code"], "RECOVERY_SUBMISSION_UNKNOWN")
        second = self.execute(plan)
        self.assertEqual(second["state"], RecoveryState.NEEDS_ACTION)
        self.assertEqual(self.calls, ["POST"])
        self.assertFalse(self.published)

    def test_pending_observations_exhaust_the_same_persisted_call_budget(self):
        self.register(pending=True)
        plan = self.plan(calls=6)
        self.assertEqual(self.execute(plan)["state"], RecoveryState.WAITING_EXTERNAL)
        self.assertEqual(self.execute(plan)["state"], RecoveryState.WAITING_EXTERNAL)
        self.assertEqual(self.execute(plan)["state"], RecoveryState.EXHAUSTED)
        self.assertEqual(self.calls, ["POST", "GET"])
        self.assertEqual(self.repository.status(plan.plan_id)["usage"]["pages"], 1)
        self.assertFalse(self.published)

    def test_observation_cannot_declare_a_new_external_submission(self):
        self.register()
        original = self.registry._strategies[("free-ocr", "v3")]

        def wrong_observer(context, task_id):
            context.record_external_intent()
            raise AssertionError("must reject before any new POST")

        self.registry._strategies[("free-ocr", "v3")] = Strategy(
            original.strategy_id, original.version, original.verifier_id, original.produce,
            original.verify, original.applicable, resume_external=wrong_observer,
            observation_usage=Usage(calls=2, seconds=10))
        plan = self.plan()
        self.execute(plan)
        outcome = self.execute(plan)
        self.assertEqual(outcome["state"], RecoveryState.FAILED_INTERNAL)
        self.assertFalse(self.published)
        self.assertEqual(self.calls, ["POST"])

    def test_automatic_observation_wait_is_charged_without_another_submission(self):
        self.register()
        key = ("free-ocr", "v3")
        self.registry._strategies[key] = replace(self.registry._strategies[key], auto_observe=True)
        plan = self.plan()
        self.assertEqual(self.execute(plan)["state"], RecoveryState.COMMITTED)
        self.assertEqual(self.calls, ["POST", "GET"])
        self.assertEqual(self.clock.now, 1002.0)
        self.assertEqual(self.repository.status(plan.plan_id)["usage"]["seconds"], 2.0)

    def test_automatic_observation_charges_actual_submit_query_and_wait_seconds(self):
        self.register(submit_seconds=3.0, observe_seconds=4.0)
        key = ("free-ocr", "v3")
        self.registry._strategies[key] = replace(self.registry._strategies[key], auto_observe=True)
        plan = self.plan()
        self.assertEqual(self.execute(plan)["state"], RecoveryState.COMMITTED)
        self.assertEqual(self.calls, ["POST", "GET"])
        self.assertEqual(self.clock.now, 1009.0)
        self.assertEqual(self.repository.status(plan.plan_id)["usage"]["seconds"], 9.0)

    def test_proven_rate_limit_can_retry_but_unknown_server_error_cannot_clear_intent(self):
        attempts = []

        def create(context):
            context.consume(Usage(calls=1))
            context.record_external_intent()
            attempts.append(1)
            if len(attempts) == 1:
                with self.assertRaisesRegex(ValueError, "UNPROVEN"):
                    context.record_external_rejection(503)
                context.record_external_rejection(429)
                raise RecoveryError("RATE_LIMIT", transient=True, retry_after=3)
            context.record_external_task("task")
            return CandidateResult({"source": "doc:12", "value": 10})

        action = replace(self.action, estimated_usage=Usage(calls=2, seconds=10))
        self.registry.register(Strategy("free-ocr", "v3", "financial-source-verifier", create,
            lambda candidate, plan: VerificationResult(True, (self.issue.issue_id,)),
            lambda plan, action: True))
        plan = replace(self.plan(), actions=(action,))
        self.assertEqual(self.execute(plan)["state"], RecoveryState.COMMITTED)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.clock.now, 1003.0)
        self.assertEqual(self.repository.status(plan.plan_id)["action_count"], 2)
