from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from openthesis.domain import Company, ResearchRun
from openthesis.resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, Usage, VerificationResult, digest,
)
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryConflict, RecoveryRepository
from openthesis.resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from openthesis.storage import SCHEMA_VERSION, Storage, StorageSchemaError


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class ResilienceCoreTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "tmp/core-resilience-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.directory = Path(self.temp.name)
        self.store = Storage(self.directory)
        self.company = Company("issuer", "EX", "Example")
        self.store.save_company(self.company)
        self.store.save_run(ResearchRun("run", self.company, "wf", "pack", "1", "", "", "2026-10-01"))
        self.clock = Clock()
        self.input_digest = digest({"frozen_source": "original"})
        self.issue = Issue("SOURCE_MISSING", "source", "document", "run", self.input_digest)
        self.generation = 0
        self.publications = []
        self.registry = StrategyRegistry()
        self.repository = RecoveryRepository(self.store, self.read_snapshot, self.publish)
        self.coordinator = RecoveryCoordinator(self.repository, self.registry, clock=self.clock, sleeper=self.clock.sleep)

    def tearDown(self):
        self.temp.cleanup()

    def read_snapshot(self, db, run_id):
        row = db.execute("SELECT generation FROM latest_report_revisions WHERE run_id=?", (run_id,)).fetchone()
        return int(row["generation"]) if row else 0, self.input_digest

    def publish(self, db, plan, payload):
        result = self.store._append_report_revision_db(db, plan.run_id, payload, expected_generation=plan.expected_generation)
        self.publications.append(payload)
        return result

    def plan(self, *actions, **kwargs):
        return RecoveryPlan("incident", "run", "financials", 0, self.input_digest, (self.issue,), tuple(actions), **kwargs)

    def action(self, name="fetch", **kwargs):
        return RecoveryAction(name, "1", ("doc",), self.input_digest, "financial-invariants", **kwargs)

    def register(self, name="fetch", produce=None, verify=None, *, max_remote_calls=None):
        self.registry.register(Strategy(name, "1", "financial-invariants",
            produce or (lambda context: CandidateResult({"fact": "correct", "source": "original"}, ("candidate-1",))),
            verify or (lambda candidate, plan: VerificationResult(True, tuple(issue.issue_id for issue in plan.issues))),
            lambda plan, action: True, max_remote_calls=max_remote_calls))

    def execute(self, plan, authorization=AuthorizationScope()):
        return self.coordinator.execute(plan, expected_generation=plan.expected_generation,
                                        plan_hash=plan.plan_hash, authorization=authorization)

    def test_preview_is_read_only_and_round_trip_hash_is_checked(self):
        self.register()
        plan = self.plan(self.action())
        self.assertEqual(self.coordinator.preview(plan)["plan_hash"], plan.plan_hash)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recovery_plans").fetchone()[0], 0)
        self.assertEqual(RecoveryPlan.from_dict(plan.to_dict()), plan)
        tampered = plan.to_dict()
        tampered["expected_generation"] = 7
        with self.assertRaisesRegex(ValueError, "HASH_OR_VERSION"):
            RecoveryPlan.from_dict(tampered)

    def test_verified_candidate_commits_atomically_and_repeated_execute_has_no_call(self):
        calls = []
        self.register(produce=lambda context: calls.append("fetch") or CandidateResult({"fact": 10}))
        plan = self.plan(self.action())
        outcome = self.execute(plan)
        self.assertEqual(outcome["state"], RecoveryState.COMMITTED)
        self.assertTrue(outcome["committed_revision_id"])
        self.assertEqual(self.store.latest_report_revision("run")["payload"], {"fact": 10})
        # Execution against an old generation must not issue another request.
        with self.assertRaisesRegex(RecoveryConflict, "STALE"):
            self.execute(plan)
        self.assertEqual(calls, ["fetch"])
        self.assertEqual(self.repository.status(plan.plan_id)["action_count"], 1)

    def test_successful_adapter_with_failed_invariant_never_publishes(self):
        self.register(verify=lambda candidate, plan: VerificationResult(False, invariant_failures=("currency",)))
        plan = self.plan(self.action())
        result = self.execute(plan)
        self.assertEqual(result["state"], RecoveryState.BLOCKED_INTEGRITY)
        self.assertEqual(self.publications, [])
        self.assertIsNone(self.store.latest_report_revision("run"))
        self.assertTrue(result["next_actions"])

    def test_alternate_strategy_really_repairs_after_validation_failure(self):
        self.register("bad", verify=lambda candidate, plan: VerificationResult(False, invariant_failures=("unit",)))
        self.register("good")
        plan = self.plan(self.action("bad"), self.action("good"))
        self.assertEqual(self.execute(plan)["state"], RecoveryState.COMMITTED)
        self.assertEqual(self.repository.status(plan.plan_id)["action_count"], 2)
        self.assertEqual(len(self.publications), 1)

    def test_budget_persists_across_process_model_and_strategy_changes(self):
        self.register("bad", verify=lambda candidate, plan: VerificationResult(False))
        limits = RecoveryLimits(automatic_actions=1)
        first = self.plan(self.action("bad"), limits=limits)
        self.execute(first)
        restarted = Storage(self.directory)
        self.repository = RecoveryRepository(restarted, self.read_snapshot, self.publish)
        self.coordinator = RecoveryCoordinator(self.repository, self.registry, clock=self.clock, sleeper=self.clock.sleep)
        self.register("good")
        second = self.plan(self.action("good", configuration_fingerprint="new-model"), limits=RecoveryLimits(automatic_actions=8))
        result = self.execute(second)
        self.assertEqual(result["state"], RecoveryState.EXHAUSTED)
        self.assertEqual(self.publications, [])
        self.assertEqual(self.repository.status(second.plan_id)["action_count"], 1)

    def test_authorization_failure_never_calls_provider(self):
        calls = []
        self.register(produce=lambda context: calls.append(1) or CandidateResult({"fact": 10}))
        action = self.action(provider_id="vision", page_ids=("doc:1",), configuration_fingerprint="config-v1", estimated_usage=Usage(calls=1, pages=1))
        plan = self.plan(action, limits=RecoveryLimits(calls=1, pages=1))
        result = self.execute(plan)
        self.assertEqual(result["state"], RecoveryState.NEEDS_ACTION)
        self.assertEqual(result["diagnostic_code"], "RECOVERY_AUTHORIZATION_REQUIRED")
        self.assertEqual(calls, [])

    def test_metered_extension_is_debited_before_wire_and_retained_after_restart(self):
        seen = []
        def produce(context):
            context.consume(Usage(calls=1))
            seen.append(self.repository.status(context.plan.plan_id)["usage"]["calls"])
            context.consume(Usage(calls=1))
            seen.append(self.repository.status(context.plan.plan_id)["usage"]["calls"])
            context.record_external_intent()
            raise RecoveryError("TIMEOUT", transient=True)
        self.register(produce=produce, max_remote_calls=2)
        auth = AuthorizationScope(("wire",), (), ("cfg",))
        action = self.action(external_submission=True, estimated_usage=Usage(calls=1),
                             provider_id="wire", configuration_fingerprint="cfg")
        plan = self.plan(action, limits=RecoveryLimits(calls=2), authorization_scope=auth)
        self.assertEqual(self.execute(plan, auth)["diagnostic_code"], "RECOVERY_SUBMISSION_UNKNOWN")
        self.assertEqual(seen, [1, 2])
        restarted = RecoveryRepository(Storage(self.directory), self.read_snapshot, self.publish)
        self.assertEqual(restarted.status(plan.plan_id)["usage"]["calls"], 2)
        self.assertEqual(json.loads(restarted.status(plan.plan_id)["actions"][0]["reservation_json"])["calls"], 2)

    def test_metered_extension_does_not_precharge_or_exceed_the_plan_budget(self):
        calls = []
        def produce(context):
            context.consume(Usage(calls=1))
            calls.append(1)
            context.consume(Usage(calls=1))
            calls.append(2)
            return CandidateResult({"fact": 10})
        self.register(produce=produce, max_remote_calls=2)
        auth = AuthorizationScope(("wire",), (), ("cfg",))
        plan = self.plan(self.action(estimated_usage=Usage(calls=1), provider_id="wire",
                                   configuration_fingerprint="cfg"),
                         limits=RecoveryLimits(calls=1), authorization_scope=auth)
        result = self.execute(plan, auth)
        self.assertEqual(calls, [1])
        self.assertEqual(result["diagnostic_code"], "RECOVERY_INCIDENT_BUDGET_EXHAUSTED")
        self.assertEqual(self.repository.status(plan.plan_id)["usage"]["calls"], 1)
        self.assertEqual(self.publications, [])

    def test_registered_wire_ceiling_does_not_authorize_a_third_call(self):
        seen = []
        def produce(context):
            for _ in range(3):
                context.consume(Usage(calls=1))
                seen.append(1)
            return CandidateResult({"fact": 10})
        self.register(produce=produce, max_remote_calls=2)
        auth = AuthorizationScope(("wire",), (), ("cfg",))
        plan = self.plan(self.action(estimated_usage=Usage(calls=1), provider_id="wire",
                                   configuration_fingerprint="cfg"),
                         limits=RecoveryLimits(calls=3), authorization_scope=auth)
        self.assertEqual(self.execute(plan, auth)["diagnostic_code"], "RECOVERY_ACTION_USAGE_EXHAUSTED")
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.repository.status(plan.plan_id)["usage"]["calls"], 2)
        self.assertEqual(self.publications, [])

    def test_plain_400_cannot_clear_intent_without_explicit_format_proof(self):
        plan = self.plan(self.action(external_submission=True))
        self.repository.save_plan(plan, self.clock())
        self.repository.acquire(plan, "owner", self.clock())
        action_id = self.repository.begin_action(plan, plan.actions[0], "owner", self.clock())
        self.repository.external_intent(plan, action_id, "owner", self.clock())
        with self.assertRaisesRegex(ValueError, "REJECTION_UNPROVEN"):
            self.repository.external_rejection(plan, action_id, "owner", self.clock(), 400)
        self.assertEqual(self.repository.status(plan.plan_id)["actions"][0]["external_intent"], 1)
        self.repository.external_rejection(plan, action_id, "owner", self.clock(), 400,
                                           reason="response_format_unsupported")
        self.assertEqual(self.repository.status(plan.plan_id)["actions"][0]["external_intent"], 0)

    def test_registered_wire_policy_cannot_be_smaller_than_its_reservation(self):
        self.register(max_remote_calls=1)
        with self.assertRaisesRegex(ValueError, "CALL_POLICY_INVALID"):
            self.coordinator.preview(self.plan(self.action(estimated_usage=Usage(calls=2))))

    def test_external_unknown_submission_is_not_blindly_retried(self):
        auth = AuthorizationScope(("vision",), ("doc:1",), ("config-v1",))
        calls = []
        def produce(context):
            context.consume(Usage(calls=1, pages=1))
            context.record_external_intent()
            calls.append(1)
            raise RecoveryError("NETWORK_TIMEOUT", transient=True)
        self.register(produce=produce)
        action = self.action(provider_id="vision", page_ids=("doc:1",), configuration_fingerprint="config-v1", estimated_usage=Usage(calls=1, pages=1), external_submission=True)
        plan = self.plan(action, limits=RecoveryLimits(calls=3, pages=3), authorization_scope=auth)
        result = self.execute(plan, auth)
        self.assertEqual(result["diagnostic_code"], "RECOVERY_SUBMISSION_UNKNOWN")
        self.assertEqual(calls, [1])
        self.assertEqual(self.repository.status(plan.plan_id)["actions"][0]["external_intent"], 1)

    def test_transient_retries_are_bounded_and_retry_after_is_counted(self):
        calls = []
        def produce(context):
            calls.append(1)
            raise RecoveryError("HTTP_429", transient=True, retry_after=45)
        self.register(produce=produce)
        plan = self.plan(self.action())
        result = self.execute(plan)
        self.assertEqual(calls, [1, 1, 1])
        self.assertNotEqual(result["state"], RecoveryState.COMMITTED)
        status = self.repository.status(plan.plan_id)
        self.assertEqual(status["action_count"], 3)
        self.assertEqual(status["usage"]["seconds"], 90)

    def test_cancellation_fences_a_running_worker(self):
        def produce(context):
            self.coordinator.cancel(context.plan.plan_id)
            return CandidateResult({"unsafe_late_result": 10})
        self.register(produce=produce)
        plan = self.plan(self.action())
        self.assertEqual(self.execute(plan)["state"], RecoveryState.CANCELLED)
        self.assertEqual(self.publications, [])

    def test_circuit_survives_restart_and_allows_one_probe(self):
        for count in range(3):
            self.assertTrue(self.repository.allow_provider_request("cloud", "cfg", f"worker-{count}", self.clock()))
            self.repository.provider_result("cloud", "cfg", f"worker-{count}", self.clock(), success=False, transient=True)
        restarted = RecoveryRepository(Storage(self.directory), self.read_snapshot, self.publish)
        self.assertFalse(restarted.allow_provider_request("cloud", "cfg", "early", self.clock()))
        self.clock.sleep(60)
        self.assertTrue(restarted.allow_provider_request("cloud", "cfg", "probe", self.clock()))
        self.assertFalse(restarted.allow_provider_request("cloud", "cfg", "competitor", self.clock()))
        restarted.provider_result("cloud", "cfg", "probe", self.clock(), success=True, transient=False)
        self.assertTrue(restarted.allow_provider_request("cloud", "cfg", "normal", self.clock()))

    def test_different_models_for_same_target_cannot_acquire_two_leases(self):
        first = self.plan(self.action(configuration_fingerprint="model-1"))
        second = self.plan(self.action(configuration_fingerprint="model-2"))
        self.repository.save_plan(first, self.clock())
        self.repository.save_plan(second, self.clock())
        self.repository.acquire(first, "owner-1", self.clock())
        with self.assertRaisesRegex(RecoveryConflict, "TARGET_BUSY"):
            self.repository.acquire(second, "owner-2", self.clock())
        self.clock.sleep(31)
        self.assertTrue(self.repository.acquire(second, "owner-2", self.clock()))
        with self.assertRaisesRegex(RecoveryConflict, "LEASE_LOST"):
            self.repository.heartbeat(first, "owner-1", self.clock())

    def test_publication_exception_rolls_back_revision_and_completion(self):
        def broken(db, plan, payload):
            self.store._append_report_revision_db(db, plan.run_id, payload, expected_generation=0)
            raise RuntimeError("disk_full")
        self.register()
        self.repository.publisher = broken
        result = self.execute(self.plan(self.action()))
        self.assertEqual(result["state"], RecoveryState.FAILED_INTERNAL)
        self.assertIsNone(self.store.latest_report_revision("run"))

    def test_schema_upgrade_backup_is_idempotent_and_newer_schema_is_read_only(self):
        with self.store.connect() as db:
            db.execute("UPDATE metadata SET value='13' WHERE key='schema_version'")
        upgraded = Storage(self.directory)
        self.assertTrue(upgraded.migration_backup and upgraded.migration_backup.exists())
        with closing(sqlite3.connect(upgraded.migration_backup)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "13")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0], 1)
        self.assertIsNone(Storage(self.directory).migration_backup)
        with upgraded.connect() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
        with self.assertRaisesRegex(StorageSchemaError, "NEWER_THAN_BINARY"):
            Storage(self.directory)


if __name__ == "__main__":
    unittest.main()
