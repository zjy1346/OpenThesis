"""Boundary cases for the durable HTTP-429 recovery lane.

These tests exercise the real journal/coordinator seam with an offline wire.
The returned dictionaries are protocol candidates only; they do not assert
research quality or provider billing beyond the persisted local usage record.
"""

import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageCancelled
from openthesis.resilience.contracts import RecoveryLimits, digest
from test_text_format_recovery import OfflineWire, execute
from test_text_transient_recovery import Clock, journal_for, limited
from test_model_request_journal import _journal, _registry, _store


def _request_row(store):
    with store.connect() as db:
        return db.execute(
            "SELECT * FROM model_request_journal ORDER BY updated_at DESC, request_id DESC LIMIT 1"
        ).fetchone()


def _action_row(store):
    with store.connect() as db:
        return db.execute(
            "SELECT * FROM recovery_actions ORDER BY updated_at DESC, action_id DESC LIMIT 1"
        ).fetchone()


def test_zero_incident_calls_never_opens_rate_compensation(tmp_path):
    store, clock = _store(tmp_path), Clock()
    journal = journal_for(store, clock, calls=0)
    provider = OfflineWire([limited(), {"structured_output_valid": True}])

    with pytest.raises(ProviderError) as raised:
        execute(store, provider, journal=journal)

    assert raised.value.code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"
    assert len(provider.wires) == 1
    assert _request_row(store)["state"] == "rejected"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


def test_cancel_during_429_wait_returns_model_stage_cancelled_without_second_wire(tmp_path):
    store = _store(tmp_path)

    class CancelClock(Clock):
        def sleep(self, seconds):
            super().sleep(seconds)
            with self.journal.store.connect() as db:
                payload = json.loads(db.execute(
                    "SELECT payload_json FROM research_runs WHERE run_id=?",
                    (self.journal.run_id,),
                ).fetchone()[0])
                payload["status"] = "cancelled"
                db.execute(
                    "UPDATE research_runs SET status='cancelled', payload_json=? WHERE run_id=?",
                    (json.dumps(payload, separators=(",", ":")), self.journal.run_id),
                )

    clock = CancelClock()
    journal = journal_for(store, clock, cancelled=lambda: False)
    clock.journal = journal
    provider = OfflineWire([limited(), {"structured_output_valid": True}])

    with pytest.raises(ModelStageCancelled):
        execute(store, provider, journal=journal)

    assert len(provider.wires) == 1
    assert _request_row(store)["state"] == "rejected"


def test_generation_change_during_429_wait_cannot_compensate_or_commit(tmp_path):
    store = _store(tmp_path)
    registry = _registry(("analysis", ()))
    changed = False

    class ReplaceClock(Clock):
        def sleep(self, seconds):
            nonlocal changed
            super().sleep(seconds)
            if not changed:
                changed = True
                self.journal.prepare(registry, "frozen-material", invalidated=("analysis",))

    clock = ReplaceClock()
    journal = journal_for(store, clock)
    clock.journal = journal
    provider = OfflineWire([limited(), {"structured_output_valid": True}])

    with pytest.raises(ProviderError) as raised:
        execute(store, provider, journal=journal)

    assert raised.value.code == "RECOVERY_PLAN_STALE"
    assert len(provider.wires) == 1
    assert _request_row(store)["state"] == "rejected"
    action = _action_row(store)
    assert action is not None and action["state"] != "committed"
    assert action["result_json"] is None


def test_unknown_rate_submission_blocks_a_different_model_configuration(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited(), ProviderError("unknown", code="MODEL_TIMEOUT", retryable=True)])

    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock, calls=4))
    assert first.value.code == "MODEL_SUBMISSION_UNKNOWN"

    changed_journal = _journal(store, clock=clock, config="different-model-config")
    changed_journal.repair_limits = RecoveryLimits(calls=8, transient_retries=1)
    changed_journal.recovery_sleeper = clock.sleep
    with pytest.raises(ProviderError) as second:
        execute(store, provider, journal=changed_journal)
    assert second.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert len(provider.wires) == 2
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 1
        assert db.execute(
            "SELECT generation FROM model_stage_request_heads WHERE run_id=? AND stage_id=?",
            ("journal-run", "analysis"),
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retry_after_seconds", True),
        ("retry_after_seconds", "3"),
        ("http_status", True),
        ("http_status", "429"),
    ],
)
def test_mutated_rate_rejection_proof_never_opens_a_retry(tmp_path, field, value):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited()])
    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock, calls=0))
    assert first.value.code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"

    row = _request_row(store)
    proof = json.loads(row["candidate_json"])
    proof[field] = value
    with store.connect() as db:
        db.execute(
            "UPDATE model_request_journal SET candidate_json=?, candidate_digest=? WHERE request_id=?",
            (json.dumps(proof, separators=(",", ":")), digest(proof), row["request_id"]),
        )

    with pytest.raises(ProviderError) as second:
        execute(store, provider, journal=journal_for(store, clock))
    assert second.value.code == "MODEL_RATE_LIMITED"
    assert len(provider.wires) == 1


def test_rejected_digest_tampering_never_reopens_rate_compensation(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited()])
    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock, calls=0))
    assert first.value.code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"
    row = _request_row(store)
    with store.connect() as db:
        db.execute(
            "UPDATE model_request_journal SET candidate_digest=? WHERE request_id=?",
            ("0" * 64, row["request_id"]),
        )

    with pytest.raises(ProviderError) as raised:
        execute(store, provider, journal=journal_for(store, clock))
    assert raised.value.code == "MODEL_RATE_LIMITED"
    assert len(provider.wires) == 1


def test_invalid_structured_candidate_is_observed_but_never_promoted_or_looped(tmp_path):
    store, clock = _store(tmp_path), Clock()
    invalid = {"structured_output_valid": False, "raw": "protocol candidate"}
    provider = OfflineWire([limited(), invalid, {"structured_output_valid": True}])

    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock))
    assert first.value.code in {"RECOVERY_CANDIDATE_REJECTED", "MODEL_FORMAT_RECOVERY_UNAVAILABLE"}
    assert len(provider.wires) == 2
    action = _action_row(store)
    assert action is not None
    assert action["result_json"] is not None
    assert json.loads(action["result_json"])["candidate"]["result"] == invalid
    assert _request_row(store)["state"] == "rejected"

    with pytest.raises(ProviderError):
        execute(store, provider, journal=journal_for(store, clock))
    assert len(provider.wires) == 2


def test_publisher_failure_keeps_valid_observation_for_local_revalidation_without_wire(tmp_path):
    store, clock = _store(tmp_path), Clock()
    candidate = {"structured_output_valid": True, "raw": "protocol candidate"}
    provider = OfflineWire([limited(), candidate])
    with store.connect() as db:
        db.execute(
            """CREATE TRIGGER fail_text_candidate_publish
               BEFORE UPDATE OF state ON model_request_journal
               WHEN NEW.state='candidate'
               BEGIN SELECT RAISE(ABORT, 'INJECTED_PUBLISH_FAILURE'); END;"""
        )

    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock))
    assert first.value.code == "RECOVERY_INTERNAL_IntegrityError"
    assert len(provider.wires) == 2
    action = _action_row(store)
    assert action["result_json"] is not None
    assert json.loads(action["result_json"])["candidate"]["result"] == candidate
    assert _request_row(store)["state"] == "rejected"

    with store.connect() as db:
        db.execute("DROP TRIGGER fail_text_candidate_publish")
    resumed = execute(store, provider, journal=journal_for(store, clock))
    assert resumed == candidate
    assert len(provider.wires) == 2
    assert _request_row(store)["state"] == "candidate"


def test_shared_rate_incident_usage_cannot_reset_when_limits_change(tmp_path):
    store, clock = _store(tmp_path), Clock()
    provider = OfflineWire([limited(), limited(), limited()])

    with pytest.raises(ProviderError) as first:
        execute(store, provider, journal=journal_for(store, clock, calls=2))
    assert first.value.code == "MODEL_RATE_LIMITED"
    assert len(provider.wires) == 3

    with pytest.raises(ProviderError) as second:
        execute(store, provider, journal=journal_for(store, clock, calls=8))
    assert second.value.code in {"MODEL_RATE_LIMITED", "RECOVERY_ACTION_ALREADY_ATTEMPTED", "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"}
    assert len(provider.wires) == 3
    with store.connect() as db:
        incident = db.execute("SELECT usage_json FROM recovery_incidents").fetchone()
        assert json.loads(incident["usage_json"])["calls"] == 2

