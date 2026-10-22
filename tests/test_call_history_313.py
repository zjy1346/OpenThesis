import json
import pytest
from unittest.mock import patch
from test_assertion_retention_312 import setup


def test_call_history_pages_receipts_without_rebuilding_candidates(tmp_path):
    journal = setup(tmp_path)
    store = journal.store
    with store.connect() as db:
        for index in range(5):
            db.execute("""INSERT INTO model_request_journal(request_id,run_id,stage_id,generation,input_digest,
                configuration_fingerprint,state,owner_id,lease_until,created_at,updated_at,purpose,candidate_json)
                VALUES(?,?,'business',1,'input','config','candidate','owner',10,1,2,'generation',?)""",
                (f"request:{index}", journal.run_id, json.dumps({"raw_candidate": "NEVER_IN_A_STATUS_PAGE"})))
    store.model_call_status(journal.run_id)
    all_calls = []
    cursor = ""
    with patch.object(store, "_build_model_call_status", side_effect=AssertionError("rebuilt candidate history")):
        for _ in range(3):
            page = store.model_call_history(journal.run_id, stage_id="business", after=cursor, limit=2)
            assert len(page["calls"]) <= 2
            assert "NEVER_IN_A_STATUS_PAGE" not in json.dumps(page)
            all_calls.extend(page["calls"])
            cursor = page["next_cursor"] or ""
    assert len({(call["request_id"], call["attempt_id"]) for call in all_calls}) == 5
    assert not cursor
    with pytest.raises(ValueError):
        store.model_call_history(journal.run_id, stage_id="business", after='[1,2]')
    with pytest.raises(KeyError):
        store.model_call_history("missing-run", stage_id="business")
    assert store.model_call_status("missing-run") == []
