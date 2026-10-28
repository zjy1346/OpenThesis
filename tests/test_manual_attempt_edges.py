"""Manual authorization edges for unresolved model format actions.

These tests use the formal request journal and recovery coordinator path.  A
manual grant is a one-time binding for an unresolved physical wire; it never
rewrites the old request/action history or authorizes an active/cancelled or
wrong-stage action.
"""

from __future__ import annotations

import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.research_execution.manual_attempts import authorize_new_attempt
from openthesis.research_execution.requests import ModelRequestJournal
from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_text_format_recovery import OfflineWire, format_rejection


def _bound_execute(store, provider, journal, *, generation=None):
    generation = generation or journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    request = ModelStageRequest("analysis", "完整的系统提示", "complete source evidence")
    with journal.bind("analysis", generation):
        return ModelStageExecutor(
            provider, ImmediateScheduler(), request_journal=journal,
        ).execute(request)


def _replacement_journal(store, *, grant, config="config-v2", now=1001.0):
    return ModelRequestJournal(
        store, "journal-run", config, timeout_seconds=2,
        cancel_check=lambda: False, clock=lambda: now,
        manual_authorization_id=grant,
    )


def _unknown_format_action(store):
    journal = _journal(store)
    provider = OfflineWire([
        format_rejection(400),
        ProviderError("response status unknown", code="MODEL_TIMEOUT", retryable=True),
    ])
    with pytest.raises(ProviderError) as raised:
        _bound_execute(store, provider, journal)
    assert raised.value.code == "MODEL_SUBMISSION_UNKNOWN"
    return journal


def test_unknown_format_action_requires_one_explicit_grant_and_binds_replacement(tmp_path):
    store = _store(tmp_path)
    _unknown_format_action(store)

    grant = authorize_new_attempt(store, "journal-run", ("analysis",),
                                  "config-v2", "visible-plan", now=1001)
    replacement = _replacement_journal(store, grant=grant)
    candidate = {"text": "replacement candidate", "structured_output_valid": True}
    result = _bound_execute(store, OfflineWire([candidate]), replacement)
    assert result == candidate

    with store.connect() as db:
        requests = db.execute(
            "SELECT request_id,state,authorization_id FROM model_request_journal ORDER BY created_at,request_id"
        ).fetchall()
        actions = db.execute(
            "SELECT state,external_intent,result_json FROM recovery_actions ORDER BY action_id"
        ).fetchall()
        grant_row = db.execute("SELECT consumed_request_id FROM model_manual_attempt_grants").fetchone()
    assert len(requests) == 2
    assert requests[0]["state"] == "rejected"  # original format rejection is immutable
    assert requests[1]["state"] == "candidate" and requests[1]["authorization_id"] == grant
    # RecoveryState uses ``needs_action`` for an unresolved external action;
    # read_wire_states exposes its semantic ``unknown`` flag without rewriting
    # the durable state.
    assert actions and actions[0]["state"] == "needs_action"
    assert actions[0]["result_json"] is None  # unknown action history is not rewritten
    assert grant_row["consumed_request_id"] == requests[1]["request_id"]

    # The same explicit authorization cannot be used for a second physical
    # wire; the durable candidate is simply reused on restart.
    assert _bound_execute(store, OfflineWire([]), replacement) == candidate


@pytest.mark.parametrize("mutation, expected", [
    ("active", "MODEL_REQUEST_IN_PROGRESS"),
    ("cancelled", "MODEL_NEW_ATTEMPT_NOT_REQUIRED"),
    ("wrong-stage", "MODEL_NEW_ATTEMPT_NOT_REQUIRED"),
])
def test_manual_grant_does_not_authorize_active_cancelled_or_wrong_stage_action(tmp_path, mutation, expected):
    store = _store(tmp_path)
    _unknown_format_action(store)
    with store.connect() as db:
        if mutation == "active":
            db.execute("UPDATE recovery_actions SET state='running', lease_until=5000")
        elif mutation == "cancelled":
            db.execute("UPDATE recovery_actions SET state='cancelled', lease_until=0")
    stage = "other-stage" if mutation == "wrong-stage" else "analysis"
    with pytest.raises(ProviderError) as raised:
        authorize_new_attempt(store, "journal-run", (stage,), "config-v2", "visible-plan", now=1001)
    assert raised.value.code == expected
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_manual_attempt_grants").fetchone()[0] == 0


def test_stale_after_grant_cannot_bind_replacement_and_old_history_remains(tmp_path):
    store = _store(tmp_path)
    _unknown_format_action(store)
    grant = authorize_new_attempt(store, "journal-run", ("analysis",),
                                  "config-v2", "visible-plan", now=1001)
    with store.connect() as db:
        db.execute("UPDATE recovery_actions SET state='cancelled', lease_until=0")
    replacement = _replacement_journal(store, grant=grant)
    with pytest.raises(ProviderError) as raised:
        _bound_execute(store, OfflineWire([{"text": "must not be sent"}]), replacement)
    assert raised.value.code == "MODEL_NEW_ATTEMPT_STALE"
    with store.connect() as db:
        rows = db.execute("SELECT state,authorization_id FROM model_request_journal ORDER BY created_at,request_id").fetchall()
        grant_row = db.execute("SELECT consumed_request_id FROM model_manual_attempt_grants").fetchone()
    assert rows[0]["state"] == "rejected"
    assert len(rows) == 1
    assert grant_row["consumed_request_id"] is None
