"""3.0.2 G01-G03: preserve Rust gateway responses through Python journal.

The response JSON files mirror the serialized ``GatewayResponseMeta`` and
``GatewayGenerateResult`` structs in ``model_gateway.rs``.  The subprocess
boundary is replaced only by a ``CompletedProcess`` fixture; the real
``RustModelGatewayProvider`` and real ``ModelRequestJournal`` are exercised.
"""
from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

from openthesis.providers import ModelConfig, ProviderError, RustModelGatewayProvider
import openthesis.research_execution.requests as request_module
from openthesis.research_execution.model import ModelStageExecutor, ModelStageRequest
from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gateway" / "3.0.2"
_SYSTEM = "offline system: preserve the complete gateway response"
_USER = "offline user: retain business_model and token_economics"


def _response(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _completed(name: str) -> CompletedProcess[bytes]:
    return CompletedProcess(
        ["offline-gateway-fixture", "--model-gateway"],
        0,
        json.dumps(_response(name), ensure_ascii=False).encode("utf-8"),
        b"",
    )


def _request() -> ModelStageRequest:
    return ModelStageRequest("analysis", _SYSTEM, _USER)


def _latest_request_row(store):
    with store.connect() as db:
        return db.execute(
            "SELECT * FROM model_request_journal ORDER BY created_at DESC LIMIT 1"
        ).fetchone()


def _persisted_candidate(row):
    """Decode the durable response envelope, not the compatibility body view."""
    return json.loads(row["candidate_json"])


def _persisted_receipt(row):
    value = row["receipt_json"]
    return json.loads(value) if value else {}


def _execute(store, provider, *, journal=None):
    journal = journal or _journal(store)
    request = _request()
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    with journal.bind("analysis", generation):
        return ModelStageExecutor(
            provider,
            ImmediateScheduler(),
            cancel_check=journal.cancel_check,
            request_journal=journal,
        ).execute(request)


def _provider() -> RustModelGatewayProvider:
    return RustModelGatewayProvider(ModelConfig(configured_model_id="offline-model"))


def _wire(provider, fixture: str):
    return patch.object(provider, "_validated_gateway_path", return_value=Path("offline-fixture.exe")), \
        patch("openthesis.gateway_events.receive_gateway_process", return_value=_completed(fixture))


def test_real_provider_and_journal_preserve_meta_body_and_reopen_without_wire(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-meta.json")[0], \
            _wire(provider, "gateway-response-meta.json")[1] as wire:
        first = _execute(store, provider)

    assert wire.call_count == 1
    assert first["business_model"]["summary"] == "offline business fixture"
    assert first["token_economics"] == {"prompt_tokens": 17, "completion_tokens": 9}

    reopened = _provider()
    replacement_journal = _journal(store)
    with patch.object(reopened, "_validated_gateway_path", return_value=Path("offline-fixture.exe")), \
            patch("openthesis.gateway_events.receive_gateway_process", side_effect=AssertionError("reopened request must use journal")) as wire2:
        second = _execute(store, reopened, journal=replacement_journal)

    assert second == first
    assert wire2.call_count == 0
    row = _latest_request_row(store)
    assert row["state"] == "candidate"
    persisted = _persisted_candidate(row)
    assert persisted["content"]["business_model"]["summary"] == "offline business fixture"
    assert persisted["content"]["token_economics"] == {
        "prompt_tokens": 17, "completion_tokens": 9,
    }
    assert persisted["receipt"]["response_received"] is True
    assert persisted["receipt"]["metadata"]["credential_version"] == 4
    assert row["response_received"] == 1


def test_secret_like_meta_isolated_without_erasing_independent_business_body(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-sensitive-meta.json")[0], \
            _wire(provider, "gateway-response-sensitive-meta.json")[1] as wire:
        result = _execute(store, provider)

    assert wire.call_count == 1
    assert result["business_model"]["summary"] == "正文必须独立保留"
    row = _latest_request_row(store)
    assert row["state"] == "candidate"
    persisted = _persisted_candidate(row)
    encoded = json.dumps(persisted, ensure_ascii=False).casefold()
    assert persisted["content"]["business_model"]["summary"] == "正文必须独立保留"
    for field in ("api_key", "authorization", "cookie"):
        assert field in encoded  # path-only diagnostic is retained for audit
    assert "fixture-only-secret" not in encoded
    assert "正文必须独立保留" in encoded


def test_bad_optional_meta_does_not_erase_business_body(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-bad-optional-meta.json")[0], \
            _wire(provider, "gateway-response-bad-optional-meta.json")[1] as wire:
        result = _execute(store, provider)

    assert wire.call_count == 1
    assert result["business_model"]["summary"] == "正文在可选meta损坏时仍保留"
    row = _latest_request_row(store)
    assert row["state"] == "candidate"
    persisted = _persisted_candidate(row)
    assert persisted["content"]["business_model"]["summary"] == "正文在可选meta损坏时仍保留"
    assert any(item["code"] == "MODEL_RESPONSE_META_TYPE_INVALID"
               for item in persisted["diagnostics"])


def test_configured_model_identity_mismatch_cannot_promote_response(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-identity-mismatch.json")[0], \
            _wire(provider, "gateway-response-identity-mismatch.json")[1] as wire:
        with pytest.raises(Exception) as caught:
            _execute(store, provider)

    assert wire.call_count == 1
    assert isinstance(caught.value, ProviderError)
    row = _latest_request_row(store)
    assert row["state"] != "candidate"
    assert row["response_received"] == 1
    assert row["diagnostic_code"] == "MODEL_RESPONSE_IDENTITY_MISMATCH"
    assert _persisted_receipt(row)["identity_conflict"] == "configured_model_id"


def test_token_economics_is_legitimate_business_output_not_a_secret(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-meta.json")[0], \
            _wire(provider, "gateway-response-meta.json")[1]:
        result = _execute(store, provider)

    assert result["token_economics"]["prompt_tokens"] == 17
    assert result["token_economics"]["completion_tokens"] == 9
    persisted = _persisted_candidate(_latest_request_row(store))
    assert persisted["content"]["token_economics"] == {
        "prompt_tokens": 17,
        "completion_tokens": 9,
    }


def test_received_unsupported_response_is_saved_without_erasing_body(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-unsupported-content.json")[0], \
            _wire(provider, "gateway-response-unsupported-content.json")[1] as wire:
        try:
            _execute(store, provider)
        except ProviderError:
            # The executor may attempt a bounded output repair; the original
            # received candidate must nevertheless remain durable.
            pass

    # An unsupported shape is preserved as a received candidate; this seam
    # does not authorize an extra generation just to satisfy a test fixture.
    assert wire.call_count == 1
    row = _latest_request_row(store)
    assert row["state"] == "candidate"
    persisted = _persisted_candidate(row)
    assert persisted["receipt"]["response_received"] is True
    assert persisted["content"]["structured_output_valid"] is False
    assert persisted["content"]["result"] == []


def test_gateway_envelope_serialization_failure_is_local_processing_failure(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with _wire(provider, "gateway-response-meta.json")[0], \
            _wire(provider, "gateway-response-meta.json")[1] as wire, \
            patch.object(request_module, "response_record",
                         side_effect=ValueError("MODEL_RESPONSE_ENVELOPE_INVALID")):
        with pytest.raises(ProviderError):
            _execute(store, provider)

    assert wire.call_count == 1
    row = _latest_request_row(store)
    assert row["state"] == "local_processing_failed"
    assert row["response_received"] == 1
    assert _persisted_receipt(row)["response_received"] is True
    assert row["diagnostic_code"] != "MODEL_SUBMISSION_UNKNOWN"


def test_remote_timeout_remains_unknown_and_has_no_response_receipt(tmp_path):
    store = _store(tmp_path)
    provider = _provider()
    with patch.object(provider, "_validated_gateway_path", return_value=Path("offline-fixture.exe")), \
            patch("openthesis.gateway_events.receive_gateway_process", side_effect=TimeoutExpired("offline-gateway", 3)) as wire:
        with pytest.raises(ProviderError) as caught:
            _execute(store, provider)

    assert wire.call_count == 1
    assert caught.value.code == "MODEL_TIMEOUT"
    row = _latest_request_row(store)
    assert row["state"] == "unknown"
    assert row["candidate_json"] is None
