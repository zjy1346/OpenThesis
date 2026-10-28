"""Explicit replacement of unknown wires, never a blind automatic resend."""
import json
import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.manual_attempts import authorize_new_attempt, read_wire_states
from openthesis.research_execution.requests import ModelRequestJournal
from test_model_request_journal import _store, _registry, _journal, _request


def _unknown(store):
    journal = _journal(store)
    registry = _registry(("analysis", ()))
    generation = journal.prepare(registry, "material-v1")["analysis"]
    request = _request()
    with pytest.raises(TimeoutError):
        journal.execute(request, lambda: (_ for _ in ()).throw(TimeoutError()), ("analysis", generation))
    return registry, request


def _replacement(store, grant="", config="config-v2", now=1001):
    return ModelRequestJournal(store, "journal-run", config, timeout_seconds=2,
        cancel_check=lambda: False, clock=lambda: now, manual_authorization_id=grant)


def test_unapproved_replacement_stays_blocked_and_approved_attempt_is_once_and_audited(tmp_path):
    store = _store(tmp_path)
    registry, request = _unknown(store)
    blocked = _replacement(store)
    generation = blocked.prepare(registry, "material-v1")["analysis"]
    with pytest.raises(ProviderError, match="unresolved"):
        blocked.execute(request, lambda: pytest.fail("Must not resubmit"), ("analysis", generation))
    grant = authorize_new_attempt(store, "journal-run", ("analysis",), "config-v2", "visible-plan", now=1001)
    replacement = _replacement(store, grant)
    generation = replacement.prepare(registry, "material-v1")["analysis"]
    candidate = replacement.execute(request, lambda: {"text": "New returned candidate"}, ("analysis", generation))
    assert candidate["text"] == "New returned candidate"
    assert replacement.execute(request, lambda: pytest.fail("Cannot repeat consumed grant"),
                               ("analysis", generation)) == candidate
    # Restart with this consumed grant may read the accepted intent, not create
    # another generation. Its old unknown predecessor remains unchanged.
    reopened = _replacement(store, grant, now=4000)
    assert reopened.prepare(registry, "material-v1")["analysis"] == generation
    assert reopened.execute(request, lambda: pytest.fail("Restart is local"), ("analysis", generation)) == candidate
    with store.connect() as db:
        rows = db.execute("SELECT * FROM model_request_journal ORDER BY generation").fetchall()
        assert len(rows) == 2 and rows[0]["state"] == "unknown" and rows[1]["state"] == "candidate"
        assert rows[1]["authorization_id"] == grant
        assert json.loads(rows[1]["supersedes_json"])["requests"] == [rows[0]["request_id"]]
        assert db.execute("SELECT new_input_digest FROM model_manual_attempt_grants").fetchone()[0] == request.input_digest


@pytest.mark.parametrize("mutation", ["model", "head", "expired", "late_response"])
def test_stale_grant_does_not_authorize_a_wire(tmp_path, mutation):
    store = _store(tmp_path)
    registry, request = _unknown(store)
    grant = authorize_new_attempt(store, "journal-run", ("analysis",), "config-v2", "plan", now=1001)
    replacement = _replacement(store, grant, config="wrong-model" if mutation == "model" else "config-v2",
                               now=3000 if mutation == "expired" else 1001)
    if mutation == "head":
        with store.connect() as db:
            db.execute("UPDATE model_stage_request_heads SET generation=generation+1")
    if mutation == "late_response":
        generation = replacement.prepare(registry, "material-v1")["analysis"]
        with store.connect() as db:
            db.execute("UPDATE model_request_journal SET response_received=1,state='late_candidate'")
        with pytest.raises(ProviderError) as caught:
            replacement.execute(request, lambda: pytest.fail("No wire"), ("analysis", generation))
    else:
        with pytest.raises(ProviderError) as caught:
            replacement.prepare(registry, "material-v1")
    assert caught.value.code == "MODEL_NEW_ATTEMPT_STALE"


def test_active_wire_cannot_be_bypassed_by_authorization(tmp_path):
    store = _store(tmp_path)
    _unknown(store)
    with store.connect() as db:
        db.execute("UPDATE model_request_journal SET state='running',lease_until=5000")
        states = read_wire_states(db, "journal-run", 1001)
        assert states[0]["unknown"] and states[0]["active"]
    with pytest.raises(ProviderError) as caught:
        authorize_new_attempt(store, "journal-run", ("analysis",), "config-v2", "plan", now=1001)
    assert caught.value.code == "MODEL_REQUEST_IN_PROGRESS"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_manual_attempt_grants").fetchone()[0] == 0


def test_grant_allows_new_input_once_but_keeps_both_input_identities(tmp_path):
    store = _store(tmp_path)
    registry, old_request = _unknown(store)
    grant = authorize_new_attempt(store, "journal-run", ("analysis",), "config-v2", "plan", now=1001)
    replacement = _replacement(store, grant)
    generation = replacement.prepare(registry, "material-v2")["analysis"]
    new_request = _request(prompt="Updated complete material, not a truncated retry")
    replacement.execute(new_request, lambda: {"text": "Candidate"}, ("analysis", generation))
    with store.connect() as db:
        grant_row = db.execute("SELECT * FROM model_manual_attempt_grants").fetchone()
        assert json.loads(grant_row["source_wires_json"])[0]["input_digest"] == old_request.input_digest
        assert grant_row["new_input_digest"] == new_request.input_digest
    with pytest.raises(ProviderError) as caught:
        replacement.execute(_request(prompt="another input"), lambda: pytest.fail("Grant already consumed"),
                            ("analysis", generation))
    assert caught.value.code == "MODEL_NEW_ATTEMPT_STALE"
