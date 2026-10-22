"""Known optional model usage counters stay unknown instead of blocking a response."""
import json

import pytest

from openthesis.research_execution.responses import received_response, response_record
from openthesis.resilience.contracts import candidate_document_json, candidate_json


def test_nested_usage_is_normalized_and_bad_fields_do_not_erase_answer():
    answer = {"analysis": "received answer remains usable"}
    response = received_response(answer, {
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": 7,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 4, "answer_tokens": 5},
            "provider_token_hint": "private-counter-hint",
            "prompt_tokens_estimate": 900,
            "output_token_estimate": "unknown-from-provider",
            "api_key": "receipt-secret-must-not-persist",
        }
    })

    assert response["analysis"] == answer["analysis"]
    record, _receipt = response_record(response)
    assert record["content"] == answer
    metadata = record["receipt"]["metadata"]
    assert metadata["input_tokens"] is None
    assert metadata["output_tokens"] == 7
    assert metadata["cache_read_input_tokens"] == 3
    assert metadata["cache_write_input_tokens"] == 2
    assert metadata["reasoning_tokens"] == 4
    assert metadata["answer_tokens"] == 5
    assert "receipt-secret-must-not-persist" not in json.dumps(record)
    assert "private-counter-hint" not in json.dumps(record)
    codes = {item["code"] for item in record["diagnostics"]}
    assert "MODEL_RESPONSE_SECRET_FIELD_ISOLATED" in codes
    assert "MODEL_RESPONSE_USAGE_FIELD_ISOLATED" in codes
    # The typed receipt may persist unknown known counters, while its answer remains intact.
    candidate_document_json(record, max_bytes=16 * 1024 * 1024)

    invalid = received_response(answer, {"usage": {"output_tokens": "not-a-count"}})
    assert invalid["analysis"] == answer["analysis"]
    assert "output_tokens" not in invalid.receipt["metadata"]
    assert any(item["code"] == "MODEL_RESPONSE_META_TYPE_INVALID" for item in invalid.diagnostics)


@pytest.mark.parametrize("usage", [
    {"input_tokens": None},
    {"provider_token_hint": 5},
    {"answer_tokens": "not-a-count"},
])
def test_untyped_usage_payload_does_not_gain_receipt_null_or_extension_exceptions(usage):
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        candidate_json({"usage": usage})


def test_legacy_response_meta_null_usage_counters_remain_readable_without_receipt_mirror():
    legacy_candidate = {
        "stage": "candidate",
        "_response_meta": {
            "cache_read_input_tokens": None,
            "cache_write_input_tokens": None,
            "reasoning_tokens": None,
            "answer_tokens": None,
        },
    }
    encoded = candidate_json(legacy_candidate)
    assert json.loads(encoded) == legacy_candidate


@pytest.mark.parametrize("budget", [4096, None])
def test_typed_receipt_output_budget_counter_is_storage_safe(budget):
    response = received_response({"analysis": "answer"}, {"output_budget_tokens": budget})
    record, _receipt = response_record(response)

    assert record["receipt"]["metadata"]["output_budget_tokens"] == budget
    candidate_document_json(record, max_bytes=16 * 1024 * 1024)


@pytest.mark.parametrize("candidate", [
    {"_response_meta": {"api_key": "do-not-store"}},
    {"_response_meta": {"provider_token_hint": "opaque-secret"}},
    {"usage": {"provider_token_hint": "opaque-secret"}},
])
def test_sensitive_fields_stay_blocked_around_optional_usage_metadata(candidate):
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        candidate_json(candidate)


@pytest.mark.parametrize("candidate", [
    {"usage": {"output_budget_tokens": 4096}},
    {"metadata": {"output_budget_tokens": None}},
])
def test_output_budget_counter_exception_is_typed_receipt_only(candidate):
    with pytest.raises(ValueError, match="RECOVERY_CANDIDATE_SECRET_FIELD"):
        candidate_json(candidate)


def test_nested_usage_survives_real_recovery_receipt_persistence_and_restart(tmp_path):
    from openthesis.storage import Storage
    from test_model_request_journal import _store
    from test_text_format_recovery import OfflineWire, execute, format_rejection

    store = _store(tmp_path)
    response = received_response({"analysis": "recovered answer"}, {
        "usage": {
            "input_tokens": None,
            "output_tokens": 6,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 3, "answer_tokens": None},
        }
    })

    recovered = execute(store, OfflineWire([format_rejection(), response]))
    assert recovered["analysis"] == "recovered answer"
    assert "_response_receipt" not in recovered

    persisted = Storage(store.data_dir)
    calls = persisted.model_call_status("journal-run")
    repair = next(call for call in calls if ":repair:" in call["request_id"])
    assert repair["state"] == "saved"
    assert repair["usage"] == {
        "input_tokens": None,
        "output_tokens": 6,
        "cache_read_input_tokens": 2,
        "cache_write_input_tokens": None,
        "reasoning_tokens": 3,
        "answer_tokens": None,
    }

    with persisted.connect() as db:
        row = db.execute("SELECT candidate_json,receipt_json,response_received FROM model_request_journal").fetchone()
    assert row["response_received"] == 1
    receipt = json.loads(row["receipt_json"])
    assert receipt["physical_call_state"] == "rejected"
    assert isinstance(receipt["physical_call_receipt"], dict)
    assert receipt["metadata"]["input_tokens"] is None
    assert receipt["metadata"]["output_tokens"] == 6
    assert "receipt-secret" not in row["candidate_json"]
