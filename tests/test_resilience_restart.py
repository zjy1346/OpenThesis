from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from openthesis.resilience.contracts import (
    AuthorizationScope,
    Issue,
    MAX_CANDIDATE_JSON_BYTES,
    RecoveryAction,
    RecoveryLimits,
    RecoveryPlan,
    RecoveryState,
    Usage,
    VerificationResult,
    candidate_json,
    digest,
)
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryConflict, RecoveryRepository, SCHEMA_STATEMENTS
from openthesis.resilience.strategies import CandidateResult, Strategy, StrategyRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class MemoryStore:
    """Persistent-across-repository SQLite journal without filesystem writes."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE research_runs(run_id TEXT PRIMARY KEY);
            INSERT INTO research_runs(run_id) VALUES ('run');
            CREATE TABLE latest_report_revisions(
                run_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                revision_id TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            """
        )
        for statement in SCHEMA_STATEMENTS:
            self.connection.execute(statement)
        self.connection.commit()

    def connect(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()


class InjectedProcessCrash(BaseException):
    pass


class RecoveryRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.clock = Clock()
        self.input_digest = digest({"frozen_source": "original"})
        self.issue = Issue("SOURCE_MISSING", "source", "document", "run", self.input_digest)
        self.registry = StrategyRegistry()
        self.repository = self._repository()
        self.coordinator = self._coordinator(self.repository)

    def tearDown(self) -> None:
        self.store.close()

    def _read_snapshot(self, db: sqlite3.Connection, run_id: str) -> tuple[int, str]:
        row = db.execute(
            "SELECT generation FROM latest_report_revisions WHERE run_id=?", (run_id,)
        ).fetchone()
        return (int(row["generation"]) if row else 0, self.input_digest)

    @staticmethod
    def _publish(db: sqlite3.Connection, plan: RecoveryPlan, payload: dict) -> dict:
        current = db.execute(
            "SELECT generation FROM latest_report_revisions WHERE run_id=?",
            (plan.run_id,),
        ).fetchone()
        generation = int(current["generation"]) if current else 0
        if generation != plan.expected_generation:
            raise RuntimeError("stale publication generation")
        next_generation = generation + 1
        revision_id = f"revision:{next_generation}"
        db.execute(
            """INSERT INTO latest_report_revisions(run_id,generation,revision_id,payload_json)
               VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
               generation=excluded.generation,revision_id=excluded.revision_id,
               payload_json=excluded.payload_json""",
            (plan.run_id, next_generation, revision_id, json.dumps(payload, sort_keys=True)),
        )
        return {"revision_id": revision_id, "generation": next_generation}

    def _repository(self) -> RecoveryRepository:
        return RecoveryRepository(self.store, self._read_snapshot, self._publish)

    def _coordinator(self, repository: RecoveryRepository) -> RecoveryCoordinator:
        return RecoveryCoordinator(repository, self.registry, clock=self.clock, sleeper=self.clock.sleep)

    def _plan(self, *actions: RecoveryAction, limits: RecoveryLimits | None = None,
              authorization_scope: AuthorizationScope = AuthorizationScope()) -> RecoveryPlan:
        return RecoveryPlan(
            "incident", "run", "financials", 0, self.input_digest,
            (self.issue,), tuple(actions), limits=limits or RecoveryLimits(),
            authorization_scope=authorization_scope,
        )

    def _action(self, name: str, **kwargs) -> RecoveryAction:
        return RecoveryAction(
            name, "1", ("doc",), self.input_digest, "financial-invariants", **kwargs
        )

    def _register(self, name: str, produce, verify=None) -> None:
        self.registry.register(Strategy(
            name, "1", "financial-invariants", produce,
            verify or (lambda candidate, plan: VerificationResult(
                True, tuple(issue.issue_id for issue in plan.issues)
            )),
            lambda plan, action: True,
        ))

    def _execute(self, plan: RecoveryPlan, authorization: AuthorizationScope = AuthorizationScope()):
        return self.coordinator.execute(
            plan, expected_generation=plan.expected_generation,
            plan_hash=plan.plan_hash, authorization=authorization,
        )

    def test_saved_validating_candidate_is_reverified_and_committed_after_restart(self) -> None:
        produced: list[str] = []
        verified: list[dict] = []

        def produce(context):
            produced.append("once")
            context.consume(Usage(calls=1))
            return CandidateResult({"fact": "persisted-value", "source": "original"}, ("artifact-1",))

        def verify(candidate, plan):
            verified.append(candidate.payload)
            return VerificationResult(True, tuple(issue.issue_id for issue in plan.issues))

        self._register("fetch", produce, verify)
        action = self._action(
            "fetch", provider_id="provider", configuration_fingerprint="cfg",
            estimated_usage=Usage(calls=1),
        )
        authorization = AuthorizationScope(("provider",), (), ("cfg",))
        plan = self._plan(
            action, limits=RecoveryLimits(calls=1), authorization_scope=authorization,
        )
        with patch.object(self.repository, "finish", side_effect=InjectedProcessCrash):
            with self.assertRaises(InjectedProcessCrash):
                self._execute(plan, authorization)

        interrupted = self.repository.status(plan.plan_id)["actions"][0]
        self.assertEqual(interrupted["state"], RecoveryState.VALIDATING)
        saved = json.loads(interrupted["result_json"])
        self.assertEqual(saved["candidate"], {"fact": "persisted-value", "source": "original"})

        self.clock.advance(31)
        self.repository = self._repository()
        self.coordinator = self._coordinator(self.repository)
        outcome = self._execute(plan, authorization)

        self.assertEqual(outcome["state"], RecoveryState.COMMITTED)
        self.assertEqual(produced, ["once"])
        self.assertEqual(verified, [
            {"fact": "persisted-value", "source": "original"},
            {"fact": "persisted-value", "source": "original"},
        ])
        status = self.repository.status(plan.plan_id)
        self.assertEqual(status["action_count"], 1)
        self.assertEqual(status["usage"]["calls"], 1)
        self.assertEqual(json.loads(status["actions"][0]["usage_json"])["calls"], 1)
        published = self.store.connection.execute(
            "SELECT payload_json FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone()
        self.assertEqual(json.loads(published["payload_json"]), saved["candidate"])

    def test_crash_inside_validator_keeps_candidate_and_does_not_repeat_paid_production(self) -> None:
        produced, verified = [], []

        def produce(context):
            produced.append("once")
            context.consume(Usage(calls=1))
            return CandidateResult({"fact": "original-value"}, ("artifact-1",))

        def verify(candidate, plan):
            verified.append(candidate.payload)
            if len(verified) == 1:
                raise InjectedProcessCrash()
            return VerificationResult(True, tuple(issue.issue_id for issue in plan.issues))

        self._register("fetch-before-validation", produce, verify)
        action = self._action("fetch-before-validation", provider_id="provider",
                              configuration_fingerprint="cfg", estimated_usage=Usage(calls=1))
        authorization = AuthorizationScope(("provider",), (), ("cfg",))
        plan = self._plan(action, limits=RecoveryLimits(calls=1), authorization_scope=authorization)
        with self.assertRaises(InjectedProcessCrash):
            self._execute(plan, authorization)
        interrupted = self.repository.status(plan.plan_id)["actions"][0]
        self.assertEqual(interrupted["state"], RecoveryState.VALIDATING)
        self.assertEqual(json.loads(interrupted["result_json"])["candidate"], {"fact": "original-value"})
        self.clock.advance(plan.limits.lease_seconds + 1)
        result = self._coordinator(self._repository()).execute(
            plan, expected_generation=plan.expected_generation,
            plan_hash=plan.plan_hash, authorization=authorization)
        self.assertEqual(result["state"], RecoveryState.COMMITTED)
        self.assertEqual(produced, ["once"])
        self.assertEqual(len(verified), 2)
        self.assertEqual(result["usage"]["calls"], 1)

    def test_pending_candidate_is_not_publishable_before_independent_validation(self) -> None:
        guarded = []

        def produce(_context):
            return CandidateResult({"unverified": "source-value"}, ("artifact-1",))

        def verify(_candidate, plan):
            row = self.store.connection.execute(
                "SELECT lease_token FROM recovery_target_leases WHERE plan_id=?", (plan.plan_id,)
            ).fetchone()
            with self.assertRaisesRegex(RecoveryConflict, "VALIDATION_REQUIRED"):
                self.repository.finish(plan, row["lease_token"], self.clock(), {},
                                       candidate={"unverified": "source-value"})
            guarded.append(True)
            self.clock.advance(3)
            return VerificationResult(False, invariant_failures=("currency",))

        self._register("pending-guard", produce, verify)
        result = self._execute(self._plan(self._action("pending-guard")))
        self.assertEqual(result["state"], RecoveryState.BLOCKED_INTEGRITY)
        self.assertEqual(result["usage"]["seconds"], 3)
        self.assertEqual(guarded, [True])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM latest_report_revisions").fetchone()[0], 0)

    def test_verifier_exception_keeps_pending_candidate_without_recharging_production(self) -> None:
        produced, verified = [], []

        def produce(context):
            produced.append("once")
            context.consume(Usage(calls=1))
            return CandidateResult({"fact": "original-value"}, ("artifact-1",))

        def verify(candidate, plan):
            verified.append(candidate.payload)
            self.clock.advance(2)
            if len(verified) == 1:
                raise RuntimeError("arbitrary error detail must not enter the journal")
            return VerificationResult(True, tuple(issue.issue_id for issue in plan.issues))

        self._register("verifier-exception", produce, verify)
        action = self._action("verifier-exception", provider_id="provider",
                              configuration_fingerprint="cfg", estimated_usage=Usage(calls=1))
        authorization = AuthorizationScope(("provider",), (), ("cfg",))
        plan = self._plan(action, limits=RecoveryLimits(calls=1), authorization_scope=authorization)
        failed = self._execute(plan, authorization)
        self.assertEqual(failed["state"], RecoveryState.FAILED_INTERNAL)
        status = self.repository.status(plan.plan_id)
        self.assertEqual(status["usage"]["calls"], 1)
        self.assertEqual(status["usage"]["seconds"], 2)
        action_row = status["actions"][0]
        self.assertEqual(action_row["state"], RecoveryState.VALIDATING)
        self.assertEqual(json.loads(action_row["result_json"])["candidate"], {"fact": "original-value"})
        self.assertTrue(json.loads(action_row["validation_json"])["pending"])
        self.assertNotIn("arbitrary error detail", json.dumps(status))
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM latest_report_revisions").fetchone()[0], 0)

        # A fresh coordinator may retry independent validation immediately,
        # without an expired lease or another paid producer call.
        outcome = self._coordinator(self._repository()).execute(
            plan, expected_generation=0, plan_hash=plan.plan_hash, authorization=authorization)
        self.assertEqual(outcome["state"], RecoveryState.COMMITTED)
        self.assertEqual(produced, ["once"])
        self.assertEqual(outcome["usage"]["calls"], 1)
        self.assertEqual(outcome["usage"]["seconds"], 4)

    def test_mutating_verifier_cannot_publish_a_different_candidate(self) -> None:
        def verify(candidate, plan):
            candidate.payload["fact"] = "substituted"
            return VerificationResult(True, tuple(issue.issue_id for issue in plan.issues))

        self._register("mutating-verifier", lambda _context: CandidateResult({"fact": "original"}), verify)
        plan = self._plan(self._action("mutating-verifier"))
        outcome = self._execute(plan)
        self.assertEqual(outcome["state"], RecoveryState.FAILED_INTERNAL)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM latest_report_revisions").fetchone()[0], 0)
        saved = json.loads(self.repository.status(plan.plan_id)["actions"][0]["result_json"])
        self.assertEqual(saved["candidate"], {"fact": "original"})

    def test_restart_skips_deterministic_failure_and_runs_untried_alternative(self) -> None:
        calls: list[str] = []
        self._register(
            "bad", lambda _context: calls.append("bad") or CandidateResult({"value": "bad"}),
            lambda _candidate, _plan: VerificationResult(False, invariant_failures=("unit",)),
        )
        first_plan = self._plan(self._action("bad"))
        self.assertEqual(self._execute(first_plan)["state"], RecoveryState.BLOCKED_INTEGRITY)

        self._register(
            "alternative",
            lambda _context: calls.append("alternative") or CandidateResult({"value": "correct"}),
        )
        continued_plan = self._plan(self._action("bad"), self._action("alternative"))
        self.repository = self._repository()
        self.coordinator = self._coordinator(self.repository)
        outcome = self._execute(continued_plan)

        self.assertEqual(outcome["state"], RecoveryState.COMMITTED)
        self.assertEqual(calls, ["bad", "alternative"])
        self.assertEqual(self.repository.status(continued_plan.plan_id)["action_count"], 2)

    def test_saved_candidate_is_rejected_when_generation_changes(self) -> None:
        produced: list[str] = []
        self._register("fetch", lambda _context: produced.append("once") or CandidateResult({"value": 1}))
        plan = self._plan(self._action("fetch"))
        with patch.object(self.repository, "finish", side_effect=InjectedProcessCrash):
            with self.assertRaises(InjectedProcessCrash):
                self._execute(plan)

        self._publish(self.store.connection, plan, {"writer": "newer-generation"})
        self.store.connection.commit()
        with self.assertRaisesRegex(RecoveryConflict, "RECOVERY_PLAN_STALE"):
            self._execute(plan)
        latest = self.store.connection.execute(
            "SELECT payload_json FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone()
        self.assertEqual(json.loads(latest["payload_json"]), {"writer": "newer-generation"})
        self.assertEqual(produced, ["once"])

    def test_saved_candidate_is_rejected_when_frozen_input_changes(self) -> None:
        produced: list[str] = []
        self._register("fetch", lambda _context: produced.append("once") or CandidateResult({"value": 1}))
        plan = self._plan(self._action("fetch"))
        with patch.object(self.repository, "finish", side_effect=InjectedProcessCrash):
            with self.assertRaises(InjectedProcessCrash):
                self._execute(plan)

        self.input_digest = digest({"frozen_source": "changed"})
        with self.assertRaisesRegex(RecoveryConflict, "RECOVERY_PLAN_STALE"):
            self._execute(plan)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone()[0], 0)
        self.assertEqual(produced, ["once"])

    def test_expired_lease_fences_old_worker_after_competing_worker_commits(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        stale_outcomes: list[dict] = []

        def slow(_context):
            entered.set()
            release.wait(timeout=5)
            return CandidateResult({"writer": "stale"})

        self._register("slow", slow)
        self._register("fast", lambda _context: CandidateResult({"writer": "current"}))
        stale_plan = self._plan(self._action("slow"))
        current_plan = self._plan(self._action("fast"))

        def old_worker() -> None:
            stale_outcomes.append(self._execute(stale_plan))

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_worker)
            self.assertTrue(entered.wait(timeout=2))
            self.clock.advance(31)
            current = self._execute(current_plan)
            release.set()
            future.result(timeout=3)

        latest = self.store.connection.execute(
            "SELECT payload_json FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone()
        self.assertEqual(current["state"], RecoveryState.COMMITTED)
        self.assertEqual(json.loads(latest["payload_json"]), {"writer": "current"})
        self.assertNotEqual(stale_outcomes[0]["state"], RecoveryState.COMMITTED)

    def test_generation_changed_takeover_keeps_late_observation_append_only(self) -> None:
        """A stale owner may leave an observation, never overwrite the new owner."""
        action = self._action("receipt-observation")
        old_plan = self._plan(action, limits=RecoveryLimits(calls=2))
        old_token, new_token = "old-owner", "new-owner"
        self.repository.save_plan(old_plan, self.clock())
        self.assertTrue(self.repository.acquire(old_plan, old_token, self.clock()))
        action_id = self.repository.begin_action(old_plan, action, old_token, self.clock())
        self.repository.observe_candidate(
            old_plan, action, old_token, self.clock(),
            candidate={"receipt": "old-owner"}, artifact_ids=("old-artifact",),
        )

        # Freeze a newer report head and let the original lease be taken over.
        self._publish(self.store.connection, old_plan, {"writer": "new-head"})
        self.store.connection.commit()
        new_plan = replace(old_plan, expected_generation=1)
        self.repository.save_plan(new_plan, self.clock())
        self.clock.advance(old_plan.limits.lease_seconds + 1)
        self.assertTrue(self.repository.acquire(new_plan, new_token, self.clock()))
        self.assertEqual(
            self.repository.begin_action(new_plan, action, new_token, self.clock()), action_id
        )
        self.repository.observe_candidate(
            new_plan, action, new_token, self.clock(),
            candidate={"receipt": "new-owner"}, artifact_ids=("new-artifact",),
        )

        # The old worker returns after takeover. Its durable start event proves
        # ownership, so the observation is retained as an audit event only.
        self.repository.observe_candidate(
            old_plan, action, old_token, self.clock(),
            candidate={"receipt": "late-old-owner"}, artifact_ids=("late-artifact",),
        )
        row = self.store.connection.execute(
            "SELECT result_json,usage_json,lease_token FROM recovery_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()
        self.assertEqual(json.loads(row["result_json"])["candidate"], {"receipt": "new-owner"})
        self.assertEqual(row["lease_token"], new_token)
        observations = self.store.connection.execute(
            "SELECT payload_json FROM recovery_events WHERE state=?",
            (RecoveryState.RUNNING,),
        ).fetchall()
        observed = [json.loads(item[0]) for item in observations
                    if json.loads(item[0]).get("code") == "RECOVERY_CANDIDATE_OBSERVED"]
        self.assertEqual([item["result"]["candidate"] for item in observed], [
            {"receipt": "old-owner"}, {"receipt": "new-owner"}, {"receipt": "late-old-owner"},
        ])
        latest = self.store.connection.execute(
            "SELECT payload_json FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone()
        self.assertEqual(json.loads(latest["payload_json"]), {"writer": "new-head"})

    def test_unowned_receipt_observation_is_rejected_without_journal_mutation(self) -> None:
        action = self._action("receipt-observation-unowned")
        plan = self._plan(action)
        self.repository.save_plan(plan, self.clock())
        self.assertTrue(self.repository.acquire(plan, "registered-owner", self.clock()))
        action_id = self.repository.begin_action(plan, action, "registered-owner", self.clock())
        with self.assertRaisesRegex(RecoveryConflict, "LEASE_LOST"):
            self.repository.observe_candidate(
                plan, action, "forged-owner", self.clock(),
                candidate={"receipt": "forged"}, artifact_ids=("forged-artifact",),
            )
        row = self.store.connection.execute(
            "SELECT result_json,lease_token FROM recovery_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()
        self.assertIsNone(row["result_json"])
        self.assertEqual(row["lease_token"], "registered-owner")
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM recovery_events WHERE state=? AND payload_json LIKE '%RECOVERY_CANDIDATE_OBSERVED%'",
            (RecoveryState.RUNNING,),
        ).fetchone()[0], 0)

    def test_cancel_persists_usage_and_cannot_publish(self) -> None:
        def cancel_during_production(context):
            context.consume(Usage(calls=1))
            self.coordinator.cancel(context.plan.plan_id)
            return CandidateResult({"writer": "cancelled"})

        self._register("cancel", cancel_during_production)
        action = self._action(
            "cancel", provider_id="provider", configuration_fingerprint="cfg",
            estimated_usage=Usage(calls=2),
        )
        authorization = AuthorizationScope(("provider",), (), ("cfg",))
        plan = self._plan(action, limits=RecoveryLimits(calls=2), authorization_scope=authorization)
        outcome = self._execute(plan, authorization)
        status = self.repository.status(plan.plan_id)

        self.assertEqual(outcome["state"], RecoveryState.CANCELLED)
        self.assertIsNotNone(status["outcome"])
        self.assertEqual(status["outcome"]["usage"], status["usage"])
        self.assertEqual(json.loads(status["actions"][0]["usage_json"]), status["usage"])
        self.assertIsNone(self.store.connection.execute(
            "SELECT revision_id FROM latest_report_revisions WHERE run_id='run'"
        ).fetchone())

    def test_recovery_limits_and_scopes_reject_unsafe_runtime_types(self) -> None:
        for kwargs in (
            {"automatic_actions": True},
            {"automatic_actions": 1.5},
            {"transient_retries": False},
            {"lease_seconds": True},
            {"seconds": float("inf")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RecoveryLimits(**kwargs)
        with self.assertRaises(ValueError):
            AuthorizationScope(page_ids=["doc:1"])

        unscoped_page_action = self._action(
            "upload", page_ids=("doc:1",), estimated_usage=Usage(pages=1),
        )
        self.assertFalse(AuthorizationScope().permits(unscoped_page_action))
        self.assertTrue(AuthorizationScope().permits(
            self._action("local-config", configuration_fingerprint="local-profile")
        ))

    def test_candidate_journal_payload_is_bounded_and_rejects_secret_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "RECOVERY_CANDIDATE_SECRET_FIELD"):
            candidate_json({"nested": [{"api_token": "never-persist-this-secret"}]})
        with self.assertRaisesRegex(ValueError, "RECOVERY_CANDIDATE_TOO_LARGE"):
            candidate_json({"blob": "x" * MAX_CANDIDATE_JSON_BYTES})

        for strategy_id, payload, sensitive_value in (
            ("secret", {"credential": "never-persist-this-secret"}, "never-persist-this-secret"),
        ):
            with self.subTest(strategy_id=strategy_id):
                self._register(strategy_id, lambda _context, payload=payload: CandidateResult(payload))
                plan = self._plan(self._action(strategy_id))
                outcome = self._execute(plan)
                self.assertEqual(outcome["state"], RecoveryState.FAILED_INTERNAL)
                status = self.repository.status(plan.plan_id)
                self.assertIsNone(status["actions"][0]["result_json"])
                journal_text = json.dumps(status, sort_keys=True)
                if sensitive_value:
                    self.assertNotIn(sensitive_value, journal_text)

        # The inline bound still applies to inline callers above. The formal
        # journal now preserves larger bounded documents using checked chunks.
        payload = {"blob": "x" * MAX_CANDIDATE_JSON_BYTES}
        self._register("chunked", lambda _context: CandidateResult(payload))
        plan = self._plan(self._action("chunked"))
        outcome = self._execute(plan)
        self.assertEqual(outcome["state"], RecoveryState.COMMITTED)
        from openthesis.resilience.payloads import load_payload
        saved = self.repository.status(plan.plan_id)["actions"][0]["result_json"]
        self.assertEqual(load_payload(self.store.connection, saved)["candidate"], payload)

    def test_plan_deserialization_rejects_coerced_generation_and_malformed_scope(self) -> None:
        action = self._action("deserialize")
        plan = self._plan(action)
        serialized = plan.to_dict()
        serialized["expected_generation"] = "0"
        with self.assertRaisesRegex(ValueError, "RECOVERY_PLAN_IDENTITY_INVALID"):
            RecoveryPlan.from_dict(serialized)

        serialized = plan.to_dict()
        serialized["authorization_scope"]["providers"] = "provider"
        with self.assertRaisesRegex(ValueError, "RECOVERY_AUTHORIZATION_SCOPE_INVALID"):
            RecoveryPlan.from_dict(serialized)


if __name__ == "__main__":
    unittest.main()
