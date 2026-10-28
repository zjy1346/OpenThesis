"""Same-head received bodies may be recovered locally; stale/unknown may not."""
import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageRequest
from test_model_request_journal import _store, _journal, _registry


def receive_late(tmp_path):
    store = _store(tmp_path)
    clock = [1000.0]
    journal = _journal(store, clock=lambda: clock[0])
    registry = _registry(("analysis", ()))
    generation = journal.prepare(registry, "source-snapshot")["analysis"]
    request = ModelStageRequest("analysis", "system", "source-bounded-prompt")
    def invoke():
        clock[0] += 25
        return {"body": "received analysis", "evidence_ids": ["filing:1"]}
    with pytest.raises(ProviderError, match="no longer current"):
        journal.execute(request, invoke, ("analysis", generation))
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
    assert row["state"] == "late_candidate" and row["response_received"]
    return store, clock, registry, generation, request, row["owner_id"]


def test_expired_received_response_restarts_under_new_owner_without_wire(tmp_path):
    store, clock, registry, generation, request, old_owner = receive_late(tmp_path)
    restored = _journal(store, clock=lambda: clock[0])
    assert restored.prepare(registry, "source-snapshot")["analysis"] == generation
    result = restored.execute(request, lambda: pytest.fail("must not regenerate"), ("analysis", generation))
    assert result["body"] == "received analysis"
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
        assert row["state"] == "candidate" and row["owner_id"] != old_owner
        assert row["diagnostic_code"] == "MODEL_LATE_CANDIDATE_LOCAL_RECOVERED"
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 1


def test_local_read_can_recover_same_head_but_not_changed_source_or_prompt(tmp_path):
    store, clock, registry, generation, request, _ = receive_late(tmp_path)
    restored = _journal(store, clock=lambda: clock[0])
    assert restored.saved_candidate(ModelStageRequest("analysis", "system", "different"), "analysis") is None
    restored.prepare(registry, "changed-source-snapshot")
    assert restored.saved_candidate(request, "analysis") is None
    assert restored.saved_generation_candidates() == {}


def test_cancelled_late_response_cannot_be_adopted(tmp_path):
    store, clock, _, _, request, _ = receive_late(tmp_path)
    restored = _journal(store, cancelled=lambda: True, clock=lambda: clock[0])
    assert restored.saved_candidate(request, "analysis") is None
    assert restored.saved_generation_candidates() == {}
