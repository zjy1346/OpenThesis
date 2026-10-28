"""Received-response storage is lossless, bounded and restart-verifiable."""
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from openthesis.research_execution.model_output import ModelOutputContract, OutputField
from openthesis.research_execution.responses import received_response
from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_text_output_recovery import OfflineWire


def test_large_received_response_roundtrips_all_text_and_receipt_after_reopen(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "materials")["analysis"]
    request = ModelStageRequest("analysis", "system", "materials")
    body = {"business_model": "完整研究内容" * 80_000, "structured_output_valid": True}
    response = received_response(body, {"configured_model_id": "model", "credential_version": 7})
    result = journal.execute(request, lambda: response, ("analysis", generation))
    assert result["business_model"] == body["business_model"]
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
        manifest = json.loads(row["candidate_json"])
        assert manifest["schema"] == "openthesis.model-response-chunks.v1"
        assert len(row["candidate_json"].encode()) < 1_048_576
        assert db.execute("SELECT COUNT(*) FROM model_response_chunks").fetchone()[0] > 1
    reopened = _journal(store)
    assert reopened.execute(request, lambda: pytest.fail("no repeated provider call"),
                            ("analysis", generation)) == result


def test_chunk_tampering_is_not_a_reason_to_resubmit_paid_request(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "materials")["analysis"]
    request = ModelStageRequest("analysis", "system", "materials")
    journal.execute(request, lambda: {"body": "x" * 1_200_000}, ("analysis", generation))
    with store.connect() as db:
        db.execute("UPDATE model_response_chunks SET content='tampered' WHERE ordinal=0")
    with pytest.raises(ProviderError, match="integrity") as caught:
        journal.execute(request, lambda: pytest.fail("do not resubmit"), ("analysis", generation))
    assert caught.value.code == "MODEL_REQUEST_CANDIDATE_CORRUPT"


def test_received_receipt_is_bound_to_candidate_digest(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "materials")["analysis"]
    request = ModelStageRequest("analysis", "system", "materials")
    journal.execute(request, lambda: received_response({"business_model": "kept"},
        {"configured_model_id": "model", "credential_version": 7}), ("analysis", generation))
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
        record = json.loads(row["candidate_json"])
        record["receipt"]["metadata"]["credential_version"] = 8
        db.execute("UPDATE model_request_journal SET candidate_json=?", (json.dumps(record),))
    with pytest.raises(ProviderError) as caught:
        journal.execute(request, lambda: pytest.fail("no retry"), ("analysis", generation))
    assert caught.value.code == "MODEL_REQUEST_CANDIDATE_CORRUPT"


def test_optional_field_repair_failure_returns_original_saved_candidate(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "materials")["analysis"]
    original = {"business_model": "already received analysis", "structured_output_valid": True}
    provider = OfflineWire([original, ProviderError("repair unavailable", code="MODEL_HTTP_503")])
    request = ModelStageRequest("analysis", "system", "materials", output_contract=ModelOutputContract(
        "fields-v1", (OutputField("business_model"), OutputField("financial_quality"))))
    with journal.bind("analysis", generation):
        result = ModelStageExecutor(provider, ImmediateScheduler(), request_journal=journal).execute(request)
    assert result["business_model"] == original["business_model"]
    assert "financial_quality" not in result
    assert result["_field_repair"]["state"] == "pending"
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
        assert row["state"] == "candidate"
    assert journal._read_candidate(row)["business_model"] == original["business_model"]
