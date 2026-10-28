"""Separate target leases must not reset one repair incident's budget."""
from dataclasses import replace

import pytest

from openthesis.resilience.contracts import AuthorizationScope, IncidentScope, RecoveryLimits, RecoveryPlan, RecoveryState, Usage, VerificationResult, digest
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryConflict, RecoveryRepository
from openthesis.resilience.strategies import CandidateResult, StrategyRegistry
import test_resilience_core as _resilience


def test_distinct_pages_and_new_configurations_share_persisted_budget_after_restart():
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        productions = []
        scope = IncidentScope("financial-evidence", digest({"captured_research": "source-cohort"}))
        plans = []
        for index in range(3):
            input_digest = digest({"page": index, "configuration": index})
            issue = replace(harness.issue, input_digest=input_digest)
            action = replace(harness.action(), input_digest=input_digest, target_ids=(f"page-{index}",))
            plan = RecoveryPlan("cohort-incident", "run", f"page-{index}", 0, input_digest, (issue,), (action,),
                limits=RecoveryLimits(automatic_actions=2), budget_scope=scope)
            assert RecoveryPlan.from_dict(plan.to_dict()) == plan
            plans.append(plan)
            # Re-create the coordinator/repository just as on process restart.
            repository = RecoveryRepository(harness.store, lambda db, run: (0, input_digest),
                lambda db, _plan, candidate: {"revision_id": f"candidate-{index}"})
            harness.registry = StrategyRegistry()
            harness.register(produce=lambda context: productions.append(context.plan.target) or CandidateResult({"page_data": True}),
                verify=lambda candidate, _plan: VerificationResult(True, tuple(item.issue_id for item in _plan.issues)))
            coordinator = RecoveryCoordinator(repository, harness.registry, clock=harness.clock, sleeper=harness.clock.sleep)
            outcome = coordinator.execute(plan, expected_generation=0, plan_hash=plan.plan_hash,
                authorization=plan.authorization_scope)
            assert outcome["state"] == (RecoveryState.COMMITTED if index < 2 else RecoveryState.EXHAUSTED)
        assert productions == ["page-0", "page-1"]
        assert repository.status(plans[-1].plan_id)["action_count"] == 2
        with harness.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM recovery_incidents").fetchone()[0] == 1
    finally:
        harness.tearDown()


def test_incident_id_cannot_bind_a_different_run_target_or_input():
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        harness.register()
        first = harness.plan(harness.action())
        harness.repository.save_plan(first, harness.clock())
        other = replace(first, target="unrelated-target")
        with pytest.raises(RecoveryConflict, match="RECOVERY_INCIDENT_IDENTITY_CONFLICT"):
            harness.repository.save_plan(other, harness.clock())
        assert harness.repository.status(first.plan_id)["state"] == "planned"
        with harness.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM recovery_plans").fetchone()[0] == 1
    finally:
        harness.tearDown()


@pytest.mark.parametrize("restriction", [
    {"pages": 1}, {"calls": 4}, {"automatic_actions": 1}, {"seconds": 5.0},
])
def test_current_plan_cannot_spend_the_incidents_older_larger_allowance(restriction):
    harness = _resilience.ResilienceCoreTests()
    harness.setUp()
    try:
        productions = []
        scope = IncidentScope("financial-evidence", digest({"captured_research": "source-cohort"}))
        original_limits = RecoveryLimits(calls=32, pages=20)
        for index in range(2):
            frozen = digest({"page": index})
            issue = replace(harness.issue, input_digest=frozen)
            authorization = AuthorizationScope(("vision",), (f"page-{index}",), (f"config-{index}",))
            action = replace(harness.action(), input_digest=frozen, target_ids=(f"page-{index}",),
                provider_id="vision", page_ids=(f"page-{index}",),
                configuration_fingerprint=f"config-{index}", estimated_usage=Usage(calls=4, pages=1, seconds=10))
            limits = original_limits if index == 0 else replace(original_limits, **restriction)
            plan = RecoveryPlan("cohort-incident", "run", f"page-{index}", 0, frozen, (issue,), (action,),
                limits=limits, budget_scope=scope, authorization_scope=authorization)
            repository = RecoveryRepository(harness.store, lambda db, run: (0, frozen),
                lambda db, _plan, candidate: {"revision_id": f"candidate-{index}"})
            harness.registry = StrategyRegistry()
            harness.register(produce=lambda context: productions.append(context.plan.target) or CandidateResult({"page_data": True}),
                verify=lambda candidate, _plan: VerificationResult(True, tuple(item.issue_id for item in _plan.issues)))
            outcome = RecoveryCoordinator(repository, harness.registry, clock=harness.clock, sleeper=harness.clock.sleep).execute(
                plan, expected_generation=0, plan_hash=plan.plan_hash, authorization=authorization)
            assert outcome["state"] == (RecoveryState.COMMITTED if index == 0 else RecoveryState.EXHAUSTED)
        assert productions == ["page-0"]
        assert repository.status(plan.plan_id)["action_count"] == 1
        assert repository.status(plan.plan_id)["usage"]["pages"] == 1
    finally:
        harness.tearDown()
