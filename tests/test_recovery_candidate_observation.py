"""Candidate observations survive recovery fences without publication rights."""

import json

import pytest

from openthesis.resilience.contracts import digest
from openthesis.resilience.repository import RecoveryConflict
from openthesis.resilience.strategies import CandidateResult

import test_resilience_core as core_fixture


class _Harness:
    """Reuse the production resilience fixture without collecting its tests."""

    def __init__(self):
        self.fixture = core_fixture.ResilienceCoreTests("runTest")
        self.fixture.setUp()

    def close(self):
        self.fixture.tearDown()

    def __getattr__(self, name):
        return getattr(self.fixture, name)


def _saved_action(harness):
    with harness.store.connect() as db:
        return db.execute(
            "SELECT state,result_json,validation_json FROM recovery_actions"
        ).fetchone()


def _observation_events(harness):
    with harness.store.connect() as db:
        rows = db.execute(
            "SELECT state,payload_json FROM recovery_events ORDER BY event_id"
        ).fetchall()
    return [
        (row["state"], json.loads(row["payload_json"]))
        for row in rows
        if json.loads(row["payload_json"]).get("code") == "RECOVERY_CANDIDATE_OBSERVED"
    ]


def test_snapshot_generation_change_observes_exact_candidate_but_never_publishes(tmp_path):
    harness = _Harness()
    try:
        candidate = {"fact": "snapshot-stale"}
        plan = harness.plan(harness.action())

        def produce(_context):
            with harness.store.connect() as db:
                harness.store._append_report_revision_db(
                    db, "run", {"mutation": "new-head"}, expected_generation=0
                )
            return CandidateResult(candidate, ("candidate-snapshot-stale",))

        harness.register(produce=produce)
        outcome = harness.execute(plan)
        assert outcome["state"] != "committed"
        saved = _saved_action(harness)
        assert json.loads(saved["result_json"])["candidate"] == candidate
        assert json.loads(saved["validation_json"])["pending"] is True
        observations = _observation_events(harness)
        assert len(observations) == 1
        assert observations[0][0] == "running"
        assert harness.store.latest_report_revision("run")["payload"] == {"mutation": "new-head"}
        assert candidate not in harness.publications
    finally:
        harness.close()


def test_cancel_after_provider_return_observes_exact_candidate_without_publication(tmp_path):
    harness = _Harness()
    try:
        candidate = {"fact": "cancelled-return"}
        plan = harness.plan(harness.action())

        def produce(context):
            harness.coordinator.cancel(context.plan.plan_id)
            return CandidateResult(candidate, ("candidate-cancelled",))

        harness.register(produce=produce)
        outcome = harness.execute(plan)
        assert outcome["state"] == "cancelled"
        saved = _saved_action(harness)
        assert json.loads(saved["result_json"])["candidate"] == candidate
        assert json.loads(saved["validation_json"])["pending"] is True
        observations = _observation_events(harness)
        assert len(observations) == 1
        assert observations[0][0] == "running"
        assert harness.publications == []
    finally:
        harness.close()


def test_expired_lease_observes_then_revalidates_without_reproducing(tmp_path):
    harness = _Harness()
    try:
        candidate = {"fact": "late-return"}
        plan = harness.plan(harness.action())

        produced = []

        def produce(_context):
            produced.append(1)
            harness.clock.now += plan.limits.lease_seconds + 1
            return CandidateResult(candidate, ("candidate-late",))

        harness.register(produce=produce)
        outcome = harness.execute(plan)
        assert outcome["state"] != "committed"
        saved = _saved_action(harness)
        assert json.loads(saved["result_json"])["candidate"] == candidate
        assert json.loads(saved["validation_json"])["pending"] is True
        observations = _observation_events(harness)
        assert len(observations) == 1
        assert observations[0][0] == "running"
        resumed = harness.execute(plan)
        assert resumed["state"] == "committed"
        assert produced == [1]
        assert harness.publications == [candidate]
    finally:
        harness.close()


def test_old_owner_observation_cannot_overwrite_new_owner_candidate(tmp_path):
    harness = _Harness()
    try:
        plan = harness.plan(harness.action())
        action = plan.actions[0]
        repository = harness.repository
        repository.save_plan(plan, harness.clock())
        repository.acquire(plan, "old-owner", harness.clock())
        action_id = repository.begin_action(plan, action, "old-owner", harness.clock())
        harness.clock.now += plan.limits.lease_seconds + 1
        repository.acquire(plan, "new-owner", harness.clock())
        assert repository.begin_action(plan, action, "new-owner", harness.clock()) == action_id

        new_candidate = {"fact": "new-owner"}
        old_candidate = {"fact": "old-owner"}
        repository.observe_candidate(
            plan, action, "new-owner", harness.clock(),
            candidate=new_candidate, artifact_ids=("new-artifact",),
        )
        repository.observe_candidate(
            plan, action, "old-owner", harness.clock(),
            candidate=old_candidate, artifact_ids=("old-artifact",),
        )
        saved = _saved_action(harness)
        assert json.loads(saved["result_json"])["candidate"] == new_candidate
        observations = _observation_events(harness)
        assert len(observations) == 2
        assert all(state == "running" for state, _ in observations)
        payloads = [payload for _, payload in observations]
        assert payloads[0]["result"]["candidate"] == new_candidate
        assert payloads[1]["result"]["candidate"] == old_candidate
        assert payloads[1]["owner_digest"] == digest("old-owner")
    finally:
        harness.close()


def test_unregistered_owner_cannot_write_candidate_observation(tmp_path):
    harness = _Harness()
    try:
        plan = harness.plan(harness.action())
        action = plan.actions[0]
        repository = harness.repository
        repository.save_plan(plan, harness.clock())
        repository.acquire(plan, "registered-owner", harness.clock())
        repository.begin_action(plan, action, "registered-owner", harness.clock())

        with pytest.raises(RecoveryConflict, match="LEASE_LOST"):
            repository.observe_candidate(
                plan, action, "forged-owner", harness.clock(),
                candidate={"fact": "forged"}, artifact_ids=("forged-artifact",),
            )
        saved = _saved_action(harness)
        assert saved["result_json"] is None
        assert _observation_events(harness) == []
    finally:
        harness.close()
