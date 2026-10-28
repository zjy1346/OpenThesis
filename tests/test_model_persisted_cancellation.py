"""Database cancellation protects requests even without this process's Event."""
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageCancelled, ModelStageExecutor, ModelStageRequest
from test_model_request_journal import _journal, _registry, _store
from test_text_format_recovery import OfflineWire, format_rejection
from test_model_stage_executor import ImmediateScheduler


def cancel_in_database(store):
    with store.connect() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?",
                                        ("journal-run",)).fetchone()[0])
        payload["status"] = "cancelled"
        db.execute("UPDATE research_runs SET status='cancelled',payload_json=? WHERE run_id=?",
                   (json.dumps(payload), "journal-run"))


def bound(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "all-source-material")["analysis"]
    request = ModelStageRequest("analysis", "system", "all original evidence")
    return store, journal, request, ("analysis", generation)


def test_database_cancel_blocks_required_wire_before_intent(tmp_path):
    store, journal, request, scope = bound(tmp_path)
    cancel_in_database(store)
    with pytest.raises(ProviderError) as caught:
        journal.execute(request, lambda: pytest.fail("cancelled wire was sent"), scope)
    assert caught.value.code == "MODEL_CANCELLED"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 0


def test_database_cancel_preserves_late_return_without_promoting_it(tmp_path):
    store, journal, request, scope = bound(tmp_path)
    candidate = {"finding": "exact returned candidate"}

    def invoke():
        # Follow the production dispatch seam: reservation precedes external
        # intent, unlike the legacy direct callback that skipped both hooks.
        journal.record_wire_diagnostics(request.system_prompt, request.user_prompt,
            provider=OfflineWire([]), purpose='generation')
        journal.before_dispatch()
        cancel_in_database(store)
        return candidate

    with pytest.raises(ProviderError) as caught:
        journal.execute(request, invoke, scope)
    assert caught.value.code == "MODEL_CANCELLED"
    with store.connect() as db:
        row = db.execute("SELECT state,candidate_json FROM model_request_journal").fetchone()
    assert row["state"] == "late_candidate"
    assert json.loads(row["candidate_json"]) == candidate


def rejected(tmp_path):
    store, journal, request, scope = bound(tmp_path)
    with pytest.raises(ProviderError):
        journal.execute(request, lambda: (_ for _ in ()).throw(format_rejection()), scope)
    return store, journal, request, scope


def test_database_cancel_blocks_format_compensation(tmp_path):
    store, journal, request, scope = rejected(tmp_path)
    cancel_in_database(store)
    with pytest.raises(ProviderError) as caught:
        journal.recover_format(request, lambda: pytest.fail("cancelled repair wire was sent"), scope)
    assert caught.value.code == "MODEL_CANCELLED"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0


def test_database_cancel_preserves_returned_format_observation_without_committing(tmp_path):
    store, journal, request, scope = rejected(tmp_path)
    candidate = {"finding": "exact returned repair", "structured_output_valid": True}

    def invoke():
        journal.record_wire_diagnostics(request.system_prompt, request.user_prompt,
            provider=OfflineWire([]), purpose='format_repair')
        journal.before_dispatch()
        cancel_in_database(store)
        return candidate

    with pytest.raises(ProviderError) as caught:
        journal.recover_format(request, invoke, scope)
    assert caught.value.code == "MODEL_CANCELLED"
    with store.connect() as db:
        request_row = db.execute("SELECT state FROM model_request_journal").fetchone()
        action = db.execute("SELECT state,result_json,validation_json FROM recovery_actions").fetchone()
    assert request_row["state"] == "rejected"
    assert action["state"] != "committed"
    saved = json.loads(action["result_json"])
    assert saved["candidate"]["result"] == candidate
    proof = json.loads(action["validation_json"])
    assert proof["passed"] is False
    assert proof["pending"] is True


def test_stage_executor_maps_database_cancel_to_cancellation_not_provider_failure(tmp_path):
    store, journal, request, scope = bound(tmp_path)
    cancel_in_database(store)
    provider = OfflineWire([])
    with journal.bind(*scope), pytest.raises(ModelStageCancelled):
        ModelStageExecutor(provider, ImmediateScheduler(), cancel_check=lambda: False,
                           request_journal=journal).execute(request)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_request_journal").fetchone()[0] == 0
