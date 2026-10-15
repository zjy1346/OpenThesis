"""Business binding for local stage-candidate recovery and atomic publication.

This is the zero-provider verification lane. It never regenerates model output;
normal request journaling and authorized remote repair are separate producers.
The durable policy owns attempts, input CAS, leases and verified commits.
"""
from __future__ import annotations

from copy import deepcopy
import time
from typing import Any, Callable

from ..domain import ResearchArtifact, utc_now_iso
from ..resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, VerificationResult, canonical_json, digest,
)
from ..resilience.coordinator import RecoveryCoordinator
from ..resilience.repository import RecoveryConflict, RecoveryRepository
from ..resilience.strategies import CandidateResult, Strategy, StrategyRegistry
from ..resilience.payloads import load_payload
from .checkpoints import SCHEMA, candidate_checkpoint, completed_checkpoint, checkpoint_artifact_id
from .executor import StageResult, StageValidation


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS stage_verification_snapshots (
        run_id TEXT NOT NULL, target TEXT NOT NULL, generation INTEGER NOT NULL,
        input_digest TEXT NOT NULL, candidate_digest TEXT NOT NULL,
        execution_fingerprint TEXT NOT NULL, PRIMARY KEY(run_id,target),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id))""",
)


class DurableStageVerifier:
    """Revalidate saved content without another call or an implicit publication."""

    def __init__(self, store: Any, run_id: str, *, cancel_check: Callable[[], bool] | None = None,
                 request_generations: dict[str, int] | None = None):
        self.store, self.run_id = store, run_id
        self.cancel_check = cancel_check or (lambda: False)
        self.request_generations = dict(request_generations or {})

    def _check_request_generation(self, db, stage_id):
        if stage_id not in self.request_generations:
            return
        row = db.execute("SELECT generation FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
                         (self.run_id, stage_id)).fetchone()
        if not row or int(row["generation"]) != self.request_generations[stage_id]:
            raise RecoveryConflict("RECOVERY_REQUEST_GENERATION_STALE")

    def __call__(self, spec, stage_input: str, candidate: dict, fingerprint: str,
                 verify_local: Callable[[dict], StageResult]) -> StageResult:
        candidate_hash = digest(candidate)
        target = "stage-verification:" + spec.stage_id
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_request_generation(db, spec.stage_id)
            active = db.execute(
                "SELECT lease_until FROM recovery_target_leases WHERE run_id=? AND target=?",
                (self.run_id, target),
            ).fetchone()
            if active and active["lease_until"] > time.time():
                raise RecoveryConflict("RECOVERY_TARGET_BUSY")
            prior = db.execute(
                "SELECT * FROM stage_verification_snapshots WHERE run_id=? AND target=?",
                (self.run_id, target),
            ).fetchone()
            identity = (stage_input, candidate_hash, fingerprint)
            unchanged = prior and identity == (
                prior["input_digest"], prior["candidate_digest"], prior["execution_fingerprint"])
            generation = int(prior["generation"]) if unchanged else int(prior["generation"]) + 1 if prior else 1
            db.execute("""INSERT INTO stage_verification_snapshots VALUES(?,?,?,?,?,?)
                ON CONFLICT(run_id,target) DO UPDATE SET generation=excluded.generation,
                input_digest=excluded.input_digest,candidate_digest=excluded.candidate_digest,
                execution_fingerprint=excluded.execution_fingerprint""",
                (self.run_id, target, generation, *identity))

        def snapshot(db, run_id):
            self._check_request_generation(db, spec.stage_id)
            row = db.execute("SELECT * FROM stage_verification_snapshots WHERE run_id=? AND target=?",
                             (run_id, target)).fetchone()
            if row is None or row["candidate_digest"] != candidate_hash or row["execution_fingerprint"] != fingerprint:
                raise RecoveryConflict("RECOVERY_PLAN_STALE")
            return int(row["generation"]), row["input_digest"]

        def publish(db, plan, payload):
            if self.cancel_check():
                raise RecoveryConflict("RECOVERY_CANCELLED")
            result = payload.get("result", {})
            verified = result_holder.get("result")
            if (payload.get("schema") != SCHEMA or result.get("stage_id") != spec.stage_id
                    or result.get("stage_identity") != spec.identity or result.get("input_digest") != stage_input
                    or result.get("execution_fingerprint") != fingerprint
                    or result.get("candidate_digest") != candidate_hash
                    or digest(result.get("candidate")) != candidate_hash
                    or not isinstance(verified, StageResult) or not verified.usable
                    or verified.candidate_digest != candidate_hash
                    or digest(verified.candidate) != candidate_hash):
                raise RecoveryConflict("RECOVERY_STAGE_COMMIT_CONTRACT_INVALID")
            committed_payload = completed_checkpoint(verified)
            artifact_id = checkpoint_artifact_id(plan.run_id, committed_payload)
            encoded = canonical_json(committed_payload)
            existing = db.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if existing and canonical_json(load_payload(db, existing["payload_json"])) != encoded:
                raise RecoveryConflict("ARTIFACT_IDENTITY_CONFLICT")
            self.store._save_artifact_db(db, ResearchArtifact(
                artifact_id=artifact_id, run_id=plan.run_id, artifact_type="stage-checkpoint",
                title=spec.stage_id, content=committed_payload, model_id=fingerprint,
                agent_id="research-execution", created_at=utc_now_iso()))
            return {"revision_id": artifact_id}

        issue = Issue("STAGE_CANDIDATE_REVERIFY_REQUIRED", "validation", target,
                      self.run_id, stage_input, stage_id=spec.stage_id,
                      affected_ids=(candidate_hash,), severity="info")
        action = RecoveryAction("stage-candidate-validation", "v1", (spec.stage_id,), stage_input,
                                spec.validator_id, configuration_fingerprint=digest({
                                    "candidate": candidate_hash, "execution": fingerprint,
                                }))
        plan = RecoveryPlan("incident:" + digest({"run": self.run_id, "target": target, "input": stage_input}),
                            self.run_id, target, generation, stage_input, (issue,), (action,),
                            limits=RecoveryLimits())
        result_holder = {}

        def verify(value, _plan):
            if self.cancel_check():
                raise RecoveryConflict("RECOVERY_CANCELLED")
            source = value.payload.get("candidate")
            # A resumed action may already hold the final checkpoint envelope.
            if value.payload.get("schema") == SCHEMA:
                source = value.payload["result"]["candidate"]
            if not isinstance(source, dict) or digest(source) != candidate_hash:
                return VerificationResult(False, remaining_issue_ids=(issue.issue_id,))
            result = verify_local(deepcopy(source))
            result_holder["result"] = result
            if result.error_code == "STAGE_VERIFIER_FAILED":
                raise RuntimeError("STAGE_VERIFIER_FAILED")
            from ..report_content import trusted_snapshot_for_artifact
            snapshot_value = trusted_snapshot_for_artifact({
                "artifact_type": "stage-checkpoint", "content": completed_checkpoint(result)})
            accepted = tuple(node["node_id"] for node in snapshot_value["nodes"]) if snapshot_value else ()
            return VerificationResult(result.validation.passed,
                                      (issue.issue_id,) if result.validation.passed else (),
                                      () if result.validation.passed else (issue.issue_id,),
                                      accepted_delta=accepted,
                                      provenance=(("candidate_sha256", candidate_hash), ("input_digest", stage_input)))

        # Production remains immutable during verification. Publication must
        # carry the exact business checkpoint, not a producer-supplied success bit.
        def checkpoint_produce(context):
            context.checkpoint()
            return CandidateResult(candidate_checkpoint(spec, stage_input, candidate,
                                                        execution_fingerprint=fingerprint))

        registry = StrategyRegistry()
        registry.register(Strategy(action.strategy_id, action.strategy_version, action.verifier_id,
                                   checkpoint_produce, verify, lambda _plan, _action: True))
        # Partial analyses keep independently verified items. Reusing that
        # exact candidate is another local validation, not regeneration and
        # not permission to publish a previously rejected full result.
        repository = RecoveryRepository(self.store, snapshot, publish, revalidate_rejected=True)
        outcome = RecoveryCoordinator(repository, registry).execute(
            plan, expected_generation=generation, plan_hash=plan.plan_hash,
            authorization=AuthorizationScope(),
        )
        if ("result" in result_holder
                and (not result_holder["result"].validation.passed
                     or outcome.get("state") in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED})):
            return result_holder["result"]
        if outcome.get("state") in {RecoveryState.COMMITTED, RecoveryState.PARTIAL_COMMITTED}:
            # A read/resume still invokes the current business validator; the
            # stored committed state is never sufficient to admit facts.
            return verify_local(deepcopy(candidate))
        return StageResult(spec.stage_id, spec.identity, stage_input, deepcopy(candidate),
                           StageValidation(False, state="needs_action", issues=(outcome.get("diagnostic_code", "STAGE_RECOVERY_UNAVAILABLE"),)),
                           candidate_hash, outcome.get("diagnostic_code", "STAGE_RECOVERY_UNAVAILABLE"),
                           execution_fingerprint=fingerprint, has_candidate=True)
