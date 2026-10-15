"""One bounded execution loop. Successful adapters still require validation."""
from __future__ import annotations

from dataclasses import asdict
import copy
import time
from threading import RLock
from typing import Any, Callable
import uuid

from .contracts import (
    AuthorizationScope, RecoveryAction, RecoveryOutcome, RecoveryPlan,
    RecoveryState, Usage, candidate_fingerprint,
    VerificationResult,
)
from .repository import RecoveryBudgetExceeded, RecoveryConflict, RecoveryRepository
from .strategies import CandidateResult, RecoveryError, StrategyRegistry


class _Context:
    def __init__(self, coordinator: RecoveryCoordinator, plan: RecoveryPlan, action: RecoveryAction,
                 token: str, action_id: str, prior: dict[str, Any] | None, *,
                 observation_usage: Usage | None = None, max_remote_calls: int | None = None):
        self.coordinator, self.plan, self.action = coordinator, plan, action
        self.token, self.action_id = token, action_id
        self.prior_candidate = copy.deepcopy(prior)
        saved_outcome = coordinator.repository.status(plan.plan_id).get("outcome") or {}
        self.remaining_issue_ids = tuple(saved_outcome.get("remaining_issue_ids", ())) if saved_outcome.get(
            "accepted_delta") else tuple(issue.issue_id for issue in plan.issues)
        self.usage = Usage()
        self.started_at = coordinator.clock()
        self.observation_usage = observation_usage
        self.max_remote_calls = max_remote_calls
        self.external_intent = observation_usage is not None
        self.external_task_id = ""
        self.response_received = False
        self._usage_lock = RLock()

    def checkpoint(self) -> None:
        now = self.coordinator.clock()
        status = self.coordinator.repository.status(self.plan.plan_id)
        remaining = float(status["limits"]["seconds"]) - float(status["usage"].get("seconds", 0.0))
        current = next((item for item in status["actions"] if item["action_id"] == self.action_id), None)
        if current and current["state"] == RecoveryState.RUNNING:
            remaining += (self.observation_usage or self.action.estimated_usage).seconds
            elapsed = now - self.started_at
        else:
            # Production has already settled its reservation. Validation is
            # charged separately from the latest journal checkpoint, without
            # adding the transport ceiling or counting production twice.
            elapsed = now - (float(current["updated_at"]) if current else self.started_at)
        if elapsed > remaining:
            raise RecoveryBudgetExceeded("RECOVERY_INCIDENT_TIME_EXHAUSTED")
        self.coordinator.repository.heartbeat(self.plan, self.token, now)

    def consume(self, usage: Usage) -> None:
        """Call before *every* nested remote request, including HTTP retries."""
        with self._usage_lock:
            self._consume_locked(usage)

    def _consume_locked(self, usage: Usage) -> None:
        self.checkpoint()
        total = self.usage.plus(usage)
        ceiling = self.observation_usage or self.action.estimated_usage
        call_ceiling = (self.max_remote_calls if self.observation_usage is None
                        and self.max_remote_calls is not None else ceiling.calls)
        if (total.calls > call_ceiling or total.tokens > ceiling.tokens
                or total.pages > ceiling.pages or total.seconds > ceiling.seconds):
            raise RecoveryBudgetExceeded("RECOVERY_ACTION_USAGE_EXHAUSTED")
        if usage.calls and self.action.provider_id and not self.coordinator.repository.allow_provider_request(
                self.action.provider_id, self.action.configuration_fingerprint, self.token, self.coordinator.clock()):
            raise RecoveryError("RECOVERY_PROVIDER_CIRCUIT_OPEN", waiting_external=True,
                                needs_action="wait_for_provider_cooldown_or_select_alternative")
        if self.observation_usage is None and self.max_remote_calls is not None:
            self.coordinator.repository.reserve_remote_calls(
                self.plan, self.action_id, self.token, self.coordinator.clock(),
                calls=total.calls, max_calls=self.max_remote_calls)
        self.usage = total

    def record_external_intent(self) -> None:
        if self.observation_usage is not None:
            raise ValueError("RECOVERY_OBSERVATION_CANNOT_SUBMIT")
        if not self.action.external_submission:
            raise ValueError("RECOVERY_EXTERNAL_SUBMISSION_NOT_DECLARED")
        self.checkpoint()
        self.coordinator.repository.external_intent(self.plan, self.action_id, self.token, self.coordinator.clock())
        self.external_intent = True

    def wait(self, seconds: float) -> None:
        """An adapter's Retry-After uses the incident's persisted wait debit."""
        self.coordinator._wait(self.plan, self.token, seconds, context=self)
        self.checkpoint()

    def record_external_task(self, task_id: str) -> None:
        if self.observation_usage is not None:
            raise ValueError("RECOVERY_OBSERVATION_CANNOT_REPLACE_TASK")
        if not self.external_intent:
            raise ValueError("RECOVERY_EXTERNAL_INTENT_REQUIRED")
        self.coordinator.repository.external_task(self.plan, self.action_id, task_id, self.token, self.coordinator.clock())
        self.external_task_id = task_id

    def record_external_rejection(self, status: int, *, reason: str = "") -> None:
        if self.observation_usage is not None or not self.external_intent:
            raise ValueError("RECOVERY_EXTERNAL_REJECTION_UNPROVEN")
        self.coordinator.repository.external_rejection(
            self.plan, self.action_id, self.token, self.coordinator.clock(), status, reason=reason)
        self.external_intent = False

    def record_external_response(self, receipt: dict) -> None:
        """A host-observed reply is not an unknown external submission."""
        if self.observation_usage is not None or not self.external_intent:
            raise ValueError("RECOVERY_RESPONSE_RECEIPT_UNPROVEN")
        self.coordinator.repository.external_response(
            self.plan, self.action_id, self.token, self.coordinator.clock(), receipt)
        self.response_received = True


class RecoveryCoordinator:
    def __init__(self, repository: RecoveryRepository, registry: StrategyRegistry, *,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep):
        self.repository, self.registry, self.clock, self.sleeper = repository, registry, clock, sleeper

    def _wait(self, plan: RecoveryPlan, token: str, delay: float, *, context: _Context | None = None) -> None:
        status = self.repository.status(plan.plan_id)
        used = float(status["usage"].get("seconds", 0.0))
        if context is not None:
            # In-producer waiting belongs to that action's wall time. It is
            # already reserved and will settle once at its checkpoint. Only
            # inter-attempt waits need an additional charge_wait debit.
            reservation = context.observation_usage or context.action.estimated_usage
            used = max(0.0, used - reservation.seconds) + max(0.0, self.clock() - context.started_at)
        if used + delay > float(status["limits"]["seconds"]):
            raise RecoveryBudgetExceeded("RECOVERY_INCIDENT_TIME_EXHAUSTED")
        remaining = delay
        while remaining > 0:
            self.repository.heartbeat(plan, token, self.clock())
            chunk = min(remaining, plan.limits.lease_seconds / 3)
            self.sleeper(chunk)
            if context is None:
                self.repository.charge_wait(plan, token, self.clock(), chunk)
            else:
                context.checkpoint()
            remaining -= chunk

    def _verify_candidate(self, strategy: Any, candidate: CandidateResult,
                          context: _Context, remaining: tuple[str, ...]) -> VerificationResult | None:
        """Independently validate the journaled value, never mutate/reproduce it.

        A verifier exception is not a network failure. Preserve the pending
        candidate and charge validation time only so a later attempt can
        validate the same paid production result.
        """
        plan, action = context.plan, context.action
        candidate_hash = candidate_fingerprint(candidate.payload, candidate.artifact_ids,
                                               self.repository.candidate_identity(plan, action))
        verifier_copy = CandidateResult(copy.deepcopy(candidate.payload), candidate.artifact_ids)
        try:
            context.checkpoint()
            verification = strategy.verify(verifier_copy, plan)
            if (not isinstance(verification, VerificationResult)
                    or candidate_fingerprint(verifier_copy.payload, verifier_copy.artifact_ids,
                                             self.repository.candidate_identity(plan, action)) != candidate_hash):
                raise ValueError("RECOVERY_VERIFIER_CONTRACT_INVALID")
        except (RecoveryBudgetExceeded, RecoveryConflict):
            raise
        except Exception:
            self.repository.record_candidate_validation(
                plan, action, context.token, self.clock(),
                {"passed": False, "pending": True, "candidate_sha256": candidate_hash},
                passed=False, pending=True, diagnostic_code="RECOVERY_VERIFIER_FAILED",
            )
            return None
        context.checkpoint()
        valid = (verification.passed and not verification.invariant_failures
                 and not verification.remaining_issue_ids
                 and set(remaining).issubset(verification.resolved_issue_ids))
        delta_valid = (bool(verification.accepted_delta) and not verification.invariant_failures
                       and set(verification.resolved_issue_ids).isdisjoint(verification.remaining_issue_ids)
                       and set(verification.remaining_issue_ids) == set(remaining) - set(verification.resolved_issue_ids))
        known_issues = {issue.issue_id for issue in plan.issues}
        if (not set(verification.resolved_issue_ids).issubset(known_issues)
                or not set(verification.remaining_issue_ids).issubset(set(remaining))):
            delta_valid = valid = False
        if not isinstance(verification.passed, bool) or any(
                not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values)
                for values in (verification.accepted_delta, verification.invalidated_dependencies,
                               verification.resolved_issue_ids, verification.remaining_issue_ids)):
            delta_valid = valid = False
        self.repository.record_candidate_validation(
            plan, action, context.token, self.clock(),
            {**asdict(verification), "passed": valid, "delta_verified": delta_valid,
             "candidate_sha256": candidate_hash},
            passed=valid,
        )
        return verification if valid or delta_valid else VerificationResult(False, remaining_issue_ids=remaining)

    def preview(self, plan: RecoveryPlan) -> dict[str, Any]:
        """Pure preview: registration/plan checks, no journal or provider writes."""
        for action in plan.actions:
            self.registry.resolve(action)
        return {**plan.to_dict(), "state": RecoveryState.PLANNED,
                "requires_authorization": [action.identity(plan.run_id, plan.target)
                    for action in plan.actions if not plan.authorization_scope.permits(action)]}

    def execute(self, plan: RecoveryPlan, *, expected_generation: int, plan_hash: str,
                authorization: AuthorizationScope) -> dict[str, Any]:
        if expected_generation != plan.expected_generation or plan_hash != plan.plan_hash:
            raise RecoveryConflict("RECOVERY_PLAN_STALE")
        self.preview(plan)
        self.repository.save_plan(plan, self.clock())
        previous_outcome = self.repository.status(plan.plan_id)["outcome"]
        token = uuid.uuid4().hex
        if not self.repository.acquire(plan, token, self.clock()):
            return self.repository.status(plan.plan_id)["outcome"]
        remaining = (tuple(previous_outcome["remaining_issue_ids"])
                     if previous_outcome and previous_outcome.get("accepted_delta")
                     else tuple(issue.issue_id for issue in plan.issues))
        prior: dict[str, Any] | None = None
        final_state = RecoveryState.NEEDS_ACTION
        next_actions: tuple[str, ...] = ("provide_required_source_or_configuration",)
        diagnostic_code = "RECOVERY_NO_APPLICABLE_STRATEGY"
        try:
            for action_index, action in enumerate(plan.actions):
                strategy = self.registry.resolve(action)
                if not strategy.applicable(plan, action):
                    continue
                if not (plan.authorization_scope.permits(action) and authorization.permits(action)):
                    diagnostic_code, next_actions = "RECOVERY_AUTHORIZATION_REQUIRED", ("approve_provider_pages_and_budget",)
                    continue
                saved = self.repository.resume_candidate(plan, action, token, self.clock())
                if saved is not None:
                    action_id = action.identity(plan.run_id, plan.target)
                    context = _Context(self, plan, action, token, action_id, prior)
                    candidate = CandidateResult(copy.deepcopy(saved["candidate"]), saved["artifact_ids"])
                    valid = self._verify_candidate(strategy, candidate, context, remaining)
                    if valid is None:
                        diagnostic_code, final_state = "RECOVERY_VERIFIER_FAILED", RecoveryState.FAILED_INTERNAL
                        next_actions = ("retry_saved_candidate_validation_or_export_diagnostic",)
                        continue
                    if valid.passed or valid.accepted_delta:
                        outcome = RecoveryOutcome(
                            RecoveryState.VALIDATING,
                            resolved_issue_ids=valid.resolved_issue_ids,
                            remaining_issue_ids=valid.remaining_issue_ids,
                            accepted_delta=valid.accepted_delta,
                            invalidated_dependencies=valid.invalidated_dependencies,
                            provenance=valid.provenance,
                            next_actions=("continue_unresolved_targets_with_new_evidence",) if valid.remaining_issue_ids else (),
                            candidate_artifact_ids=candidate.artifact_ids,
                            usage=Usage(**self.repository.status(plan.plan_id)["usage"]),
                        )
                        committed = self.repository.finish(
                            plan, token, self.clock(), outcome.to_dict(), candidate=candidate.payload,
                            continue_plan=bool(valid.remaining_issue_ids and action_index + 1 < len(plan.actions)),
                        )
                        if committed["state"] != RecoveryState.RUNNING:
                            return committed
                        remaining = tuple(committed["remaining_issue_ids"])
                        prior = copy.deepcopy(candidate.payload)
                        final_state = RecoveryState.PARTIAL_COMMITTED
                        diagnostic_code = "RECOVERY_PARTIAL_TARGETS_REMAIN"
                        next_actions = ("continue_unresolved_targets_with_new_evidence",)
                        continue
                    prior = saved["candidate"]
                    diagnostic_code, final_state = "RECOVERY_CANDIDATE_REJECTED", RecoveryState.BLOCKED_INTEGRITY
                    next_actions = ("provide_evidence_for_failed_invariant",)
                    continue
                retry = False
                while True:
                    existing = next((item for item in self.repository.status(plan.plan_id)["actions"]
                                     if item["action_id"] == action.identity(plan.run_id, plan.target)), None)
                    task_id = str(existing.get("external_task_id") or "") if existing else ""
                    observing = bool(task_id and strategy.resume_external is not None and existing
                                     and existing["state"] in {RecoveryState.WAITING_EXTERNAL, RecoveryState.NEEDS_ACTION})
                    try:
                        action_id = self.repository.begin_action(
                            plan, action, token, self.clock(), transient_retry=retry,
                            external_observation=observing,
                            observation_usage=strategy.observation_usage if observing else None,
                        )
                    except RecoveryConflict as exc:
                        if str(exc) == "RECOVERY_ACTION_ALREADY_ATTEMPTED":
                            diagnostic_code = "RECOVERY_ACTION_ALREADY_ATTEMPTED"
                            final_state = RecoveryState.NEEDS_ACTION
                            next_actions = ("select_an_untried_recovery_strategy",)
                            # Skipping a deterministic attempt is not a new
                            # failure. Preserve its classified cause/action,
                            # while still trying later unattempted strategies.
                            if existing and existing.get("diagnostic_code"):
                                diagnostic_code = existing["diagnostic_code"]
                                final_state = RecoveryState(existing["state"])
                                if (previous_outcome
                                        and previous_outcome.get("diagnostic_code") == diagnostic_code
                                        and previous_outcome.get("next_actions")):
                                    next_actions = tuple(previous_outcome["next_actions"])
                            break
                        raise
                    context = _Context(self, plan, action, token, action_id, prior,
                                       observation_usage=strategy.observation_usage if observing else None,
                                       max_remote_calls=strategy.max_remote_calls if not observing else None)
                    try:
                        context.checkpoint()
                        candidate = (strategy.resume_external(context, task_id) if observing
                                     else strategy.produce(context))
                        if not isinstance(candidate, CandidateResult):
                            raise TypeError("RECOVERY_CANDIDATE_CONTRACT_INVALID")
                        # Verify a detached copy. A producer cannot mutate an
                        # accepted payload while another thread publishes it.
                        candidate = CandidateResult(copy.deepcopy(candidate.payload), tuple(candidate.artifact_ids))
                        # The response exists even when the input head changed
                        # during I/O. Audit it before any current-head/lease or
                        # budget check, without making it publishable.
                        self.repository.observe_candidate(
                            plan, action, token, self.clock(), candidate=candidate.payload,
                            artifact_ids=candidate.artifact_ids,
                        )
                        if action.provider_id:
                            self.repository.provider_result(action.provider_id, action.configuration_fingerprint, token, self.clock(), success=True, transient=False)
                        context.checkpoint()
                        self.repository.checkpoint_candidate(
                            plan, action, token, self.clock(), usage=context.usage,
                            candidate=candidate.payload, artifact_ids=candidate.artifact_ids,
                        )
                        valid = self._verify_candidate(strategy, candidate, context, remaining)
                        if valid is None:
                            diagnostic_code, final_state = "RECOVERY_VERIFIER_FAILED", RecoveryState.FAILED_INTERNAL
                            next_actions = ("retry_saved_candidate_validation_or_export_diagnostic",)
                            break
                        if valid.passed or valid.accepted_delta:
                            outcome = RecoveryOutcome(RecoveryState.VALIDATING,
                                resolved_issue_ids=valid.resolved_issue_ids, remaining_issue_ids=valid.remaining_issue_ids,
                                accepted_delta=valid.accepted_delta,
                                invalidated_dependencies=valid.invalidated_dependencies, provenance=valid.provenance,
                                next_actions=("continue_unresolved_targets_with_new_evidence",) if valid.remaining_issue_ids else (),
                                candidate_artifact_ids=candidate.artifact_ids,
                                usage=Usage(**self.repository.status(plan.plan_id)["usage"]))
                            committed = self.repository.finish(
                                plan, token, self.clock(), outcome.to_dict(), candidate=candidate.payload,
                                continue_plan=bool(valid.remaining_issue_ids and action_index + 1 < len(plan.actions)),
                            )
                            if committed["state"] != RecoveryState.RUNNING:
                                return committed
                            remaining = tuple(committed["remaining_issue_ids"])
                            prior = copy.deepcopy(candidate.payload)
                            final_state = RecoveryState.PARTIAL_COMMITTED
                            diagnostic_code = "RECOVERY_PARTIAL_TARGETS_REMAIN"
                            next_actions = ("continue_unresolved_targets_with_new_evidence",)
                            break
                        prior = candidate.payload
                        diagnostic_code, final_state = "RECOVERY_CANDIDATE_REJECTED", RecoveryState.BLOCKED_INTEGRITY
                        next_actions = ("provide_evidence_for_failed_invariant",)
                        break
                    except RecoveryError as exc:
                        if action.provider_id and exc.code != "RECOVERY_PROVIDER_CIRCUIT_OPEN":
                            self.repository.provider_result(action.provider_id, action.configuration_fingerprint, token, self.clock(), success=False, transient=exc.transient)
                        diagnostic_code = exc.code
                        final_state = RecoveryState.WAITING_EXTERNAL if exc.waiting_external else RecoveryState.NEEDS_ACTION
                        next_actions = (exc.needs_action or ("query_saved_external_task" if exc.waiting_external else "change_configuration_or_source"),)
                        self.repository.finish_action(plan, action_id, token, self.clock(), state=final_state,
                                                      usage=context.usage, diagnostic_code=diagnostic_code)
                        action_status = next(item for item in self.repository.status(plan.plan_id)["actions"]
                                             if item["action_id"] == action_id)
                        if context.external_intent or exc.waiting_external:
                            # An unknown external submission is not a safe
                            # transient retry, even when the HTTP error is.
                            if (exc.waiting_external and action_status["external_task_id"]
                                    and strategy.resume_external is not None and strategy.auto_observe):
                                delay = max(exc.retry_after, min(30.0, 2.0 ** min(action_status["attempt"], 5)))
                                self._wait(plan, token, delay)
                                continue
                            if (context.external_intent and not context.response_received
                                    and not exc.waiting_external and not action_status["external_task_id"]):
                                diagnostic_code, next_actions = "RECOVERY_SUBMISSION_UNKNOWN", ("query_provider_before_resubmission",)
                            break
                        if exc.transient:
                            retries = next(item["retry_count"] for item in self.repository.status(plan.plan_id)["actions"] if item["action_id"] == action_id)
                            if retries < plan.limits.transient_retries:
                                delay = max(exc.retry_after, min(30.0, 2.0 ** retries))
                                self._wait(plan, token, delay)
                                retry = True
                                continue
                        break
                    except (RecoveryBudgetExceeded, RecoveryConflict):
                        raise
                    except Exception as exc:
                        diagnostic_code = "RECOVERY_INTERNAL_" + type(exc).__name__
                        final_state, next_actions = RecoveryState.FAILED_INTERNAL, ("export_diagnostic_and_retry_with_new_plan",)
                        self.repository.finish_action(plan, action_id, token, self.clock(), state=final_state,
                                                      usage=context.usage, diagnostic_code=diagnostic_code,
                                                      preserve_candidate=True)
                        break
                # An attempted deterministic strategy must not prevent trying
                # a later, unattempted alternative in the same approved plan.
                if diagnostic_code == "RECOVERY_ACTION_ALREADY_ATTEMPTED":
                    continue
            outcome = RecoveryOutcome(final_state, remaining_issue_ids=remaining,
                                      next_actions=next_actions, diagnostic_code=diagnostic_code,
                                      usage=Usage(**self.repository.status(plan.plan_id)["usage"]))
            return self.repository.finish(plan, token, self.clock(), outcome.to_dict())
        except RecoveryBudgetExceeded as exc:
            outcome = RecoveryOutcome(RecoveryState.EXHAUSTED, remaining_issue_ids=remaining,
                                      next_actions=("explicitly_extend_budget_or_supply_source",), diagnostic_code=str(exc))
            return self.repository.abandon(plan, token, self.clock(), outcome.to_dict())
        except RecoveryConflict as exc:
            outcome = RecoveryOutcome(RecoveryState.NEEDS_ACTION, remaining_issue_ids=remaining,
                                      next_actions=("refresh_plan_from_current_snapshot",), diagnostic_code=str(exc))
            return self.repository.abandon(plan, token, self.clock(), outcome.to_dict())
        finally:
            # Includes orderly BaseException unwinding. Candidates and external
            # intent remain journaled; only this worker's lease is expired.
            self.repository.release_owned_lease(plan, token, self.clock())

    def status(self, plan_id: str) -> dict[str, Any]:
        return self.repository.status(plan_id)

    def cancel(self, plan_id: str) -> None:
        self.repository.cancel(plan_id, self.clock())
