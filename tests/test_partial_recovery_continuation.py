"""A verified partial result is progress, not the end of an authorized plan."""
from openthesis.demo import DEMO_COMPANY
from openthesis.domain import ResearchArtifact, ResearchRun
from openthesis.resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, VerificationResult, digest,
)
from openthesis.resilience.coordinator import RecoveryCoordinator
from openthesis.resilience.repository import RecoveryRepository
from openthesis.resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from openthesis.storage import Storage


class Interrupted(BaseException):
    pass


def recovery(store, *, fail_second=False, crash_second=False, limits=None,
             advance_generation_after_first=False, second_provider_id="",
             unknown_second_issue_ids=False):
    identity = digest({'source': 'fixed-offline-disclosure'})
    issues = tuple(Issue(code, 'validation', 'report', 'partial-chain', identity)
                   for code in ('BUSINESS_MISSING', 'RISKS_MISSING'))
    actions = tuple(RecoveryAction(
        name, 'v1', (issue.issue_id,), identity, 'source-proof-v1',
        configuration_fingerprint='paid-model-config-v1' if index == 1 and second_provider_id else '',
        provider_id=second_provider_id if index == 1 else '',
    ) for index, (name, issue) in enumerate(zip(('business', 'risks'), issues)))
    plan = RecoveryPlan('partial-chain-incident', 'partial-chain', 'report', 1, identity,
                        issues, actions, limits=limits or RecoveryLimits())
    calls = []
    generation = [1]

    def publish(db, current, value):
        store._save_artifact_db(db, ResearchArtifact(
            'partial-chain:' + value['node_id'], 'partial-chain', 'offline-source-bound-delta',
            value['node_id'], value, created_at='2026-10-09T00:00:00+00:00'))
        if advance_generation_after_first and value['node_id'] == 'business-node':
            generation[0] += 1
        return {'revision_id': value['node_id']}

    repo = RecoveryRepository(store, lambda db, run: (generation[0], identity), publish)
    registry = StrategyRegistry()
    for index, action in enumerate(actions):
        def produce(context, index=index):
            calls.append(actions[index].strategy_id)
            if index == 1:
                assert context.remaining_issue_ids == (issues[1].issue_id,)
                assert repo.status(plan.plan_id)['outcome']['accepted_delta'] == ['business-node']
                if crash_second:
                    raise Interrupted()
                if fail_second:
                    raise RecoveryError('NO_RISK_SOURCE', needs_action='provide_risk_disclosure')
            return CandidateResult({'node_id': ('business-node', 'risks-node')[index],
                                    'body': ('Verified business disclosure.', 'Verified risk disclosure.')[index]})

        def verify(candidate, frozen, index=index):
            assert frozen.input_digest == identity
            if index == 1 and unknown_second_issue_ids:
                return VerificationResult(True, ('issue:outside-frozen-plan',),
                    ('issue:other-outside-frozen-plan',),
                    accepted_delta=(candidate.payload['node_id'],),
                    provenance=(('source_sha256', identity),))
            return VerificationResult(index == 1, (issues[index].issue_id,),
                (issues[1].issue_id,) if index == 0 else (),
                accepted_delta=(candidate.payload['node_id'],),
                provenance=(('source_sha256', identity),))

        registry.register(Strategy(action.strategy_id, action.strategy_version, action.verifier_id,
                                   produce, verify, lambda plan, action: True))
    return repo, RecoveryCoordinator(repo, registry), plan, calls


def execute(coordinator, plan):
    return coordinator.execute(plan, expected_generation=1, plan_hash=plan.plan_hash,
                               authorization=AuthorizationScope())


def setup(tmp_path):
    store = Storage(tmp_path)
    store.save_company(DEMO_COMPANY)
    store.save_run(ResearchRun('partial-chain', DEMO_COMPANY, 'wf', 'pack', '1', '', '', '2025-12-31'))
    return store


def test_partial_commit_continues_untried_strategy_and_both_deltas_survive_restart(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store)
    outcome = execute(coordinator, plan)
    assert calls == ['business', 'risks']
    assert outcome['state'] == RecoveryState.COMMITTED
    assert set(outcome['accepted_delta']) == {'business-node', 'risks-node'}
    assert not outcome['remaining_issue_ids']
    reopened = Storage(tmp_path)
    assert {row['content']['node_id'] for row in reopened.get_artifacts('partial-chain')} == {
        'business-node', 'risks-node'}
    assert repo.status(plan.plan_id)['action_count'] == 2


def test_later_failure_keeps_partial_delta_and_specific_next_action(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, fail_second=True)
    outcome = execute(coordinator, plan)
    assert calls == ['business', 'risks']
    assert outcome['accepted_delta'] == ['business-node']
    assert len(outcome['remaining_issue_ids']) == 1
    assert tuple(outcome['next_actions']) == ('provide_risk_disclosure',)
    assert len(Storage(tmp_path).get_artifacts('partial-chain')) == 1


def test_interruption_resumes_remaining_without_reproducing_committed_delta(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, crash_second=True)
    try:
        execute(coordinator, plan)
    except Interrupted:
        pass
    else:
        raise AssertionError('interruption was not injected into the remaining strategy')
    assert calls == ['business', 'risks']
    reopened = Storage(tmp_path)
    new_repo, new_coordinator, new_plan, new_calls = recovery(reopened)
    outcome = execute(new_coordinator, new_plan)
    assert new_calls == ['risks']
    assert set(outcome['accepted_delta']) == {'business-node', 'risks-node'}
    assert new_repo.status(plan.plan_id)['action_count'] == 3


def test_action_budget_exhaustion_does_not_erase_committed_partial(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, limits=RecoveryLimits(automatic_actions=1))
    outcome = execute(coordinator, plan)
    assert calls == ['business']
    assert outcome['state'] == RecoveryState.EXHAUSTED
    assert outcome['accepted_delta'] == ['business-node']
    assert len(outcome['remaining_issue_ids']) == 1
    assert len(Storage(tmp_path).get_artifacts('partial-chain')) == 1


def test_generation_advance_after_first_publish_preserves_delta_and_requires_new_plan(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, advance_generation_after_first=True)

    outcome = execute(coordinator, plan)

    assert calls == ['business']
    assert outcome['state'] == RecoveryState.PARTIAL_COMMITTED
    assert outcome['accepted_delta'] == ['business-node']
    assert outcome['remaining_issue_ids'] == [plan.issues[1].issue_id]
    assert tuple(outcome['next_actions']) == ('refresh_plan_from_current_snapshot',)
    assert {row['content']['node_id'] for row in Storage(tmp_path).get_artifacts('partial-chain')} == {
        'business-node'}
    assert repo.status(plan.plan_id)['outcome']['accepted_delta'] == ['business-node']


def test_partial_commit_does_not_expand_scope_for_unapproved_cloud_strategy(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, second_provider_id='paid-cloud-model')

    outcome = execute(coordinator, plan)

    assert calls == ['business']
    assert outcome['diagnostic_code'] == 'RECOVERY_AUTHORIZATION_REQUIRED'
    assert tuple(outcome['next_actions']) == ('approve_provider_pages_and_budget',)
    assert outcome['accepted_delta'] == ['business-node']
    assert outcome['remaining_issue_ids'] == [plan.issues[1].issue_id]
    assert plan.authorization_scope.providers == ()
    assert len(Storage(tmp_path).get_artifacts('partial-chain')) == 1
    assert repo.status(plan.plan_id)['action_count'] == 1


def test_verifier_issue_ids_outside_frozen_plan_cannot_publish_second_delta(tmp_path):
    store = setup(tmp_path)
    repo, coordinator, plan, calls = recovery(store, unknown_second_issue_ids=True)

    outcome = execute(coordinator, plan)

    assert calls == ['business', 'risks']
    assert outcome['accepted_delta'] == ['business-node']
    assert outcome['resolved_issue_ids'] == [plan.issues[0].issue_id]
    assert outcome['remaining_issue_ids'] == [plan.issues[1].issue_id]
    assert outcome['diagnostic_code'] == 'RECOVERY_CANDIDATE_REJECTED'
    assert tuple(outcome['next_actions']) == ('provide_evidence_for_failed_invariant',)
    assert {row['content']['node_id'] for row in Storage(tmp_path).get_artifacts('partial-chain')} == {
        'business-node'}
    assert repo.status(plan.plan_id)['outcome']['accepted_delta'] == ['business-node']
