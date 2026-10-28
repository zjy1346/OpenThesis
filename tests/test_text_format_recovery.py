"""Single-wire text format compensation uses the real durable recovery lane."""
from threading import Event
import json

import pytest

from openthesis.providers import ModelConfig, ProviderError, RustModelGatewayProvider
from openthesis.research_execution.model import ModelStageCancelled, ModelStageExecutor, ModelStageRequest
from openthesis.resilience.contracts import RecoveryLimits
from test_model_request_journal import _store, _registry, _journal
from test_model_stage_executor import ImmediateScheduler


class OfflineWire(RustModelGatewayProvider):
    def __init__(self, responses):
        super().__init__(ModelConfig(configured_model_id="offline-model"))
        self.responses = iter(responses)
        self.wires = []

    def _invoke_gateway(self, request):
        self.wires.append(request)
        response = next(self.responses)
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return response


def format_rejection(status=400):
    return ProviderError("response format not supported", code="MODEL_RESPONSE_FORMAT_UNSUPPORTED",
                         http_status=status)


def execute(store, provider, *, cancel_check=lambda: False, journal=None):
    journal = journal or _journal(store, cancelled=cancel_check)
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    with journal.bind("analysis", generation):
        return ModelStageExecutor(provider, ImmediateScheduler(), cancel_check=cancel_check,
                                  request_journal=journal).execute(
            ModelStageRequest("analysis", "完整的系统提示", "complete source evidence"))


@pytest.mark.parametrize("status", [400, 422])
def test_known_format_rejection_recovers_complete_request_once_and_reuses_candidate(tmp_path, status):
    store = _store(tmp_path)
    candidate = {"claims": [{"text": "supported finding"}], "structured_output_valid": True}
    provider = OfflineWire([format_rejection(status), candidate])
    assert execute(store, provider) == candidate
    assert execute(store, provider) == candidate
    assert len(provider.wires) == 2
    first, second = provider.wires
    assert first["system_prompt"] == second["system_prompt"] == "完整的系统提示"
    assert first["user_prompt"] == second["user_prompt"] == "complete source evidence"
    assert first["configured_model_id"] == second["configured_model_id"] == "offline-model"
    assert first["json_mode"] is True and second["json_mode"] is False
    assert first["wire_contract"] == second["wire_contract"] == "text-single-request-v1"
    with store.connect() as db:
        actions = db.execute("SELECT attempt,usage_json,state FROM recovery_actions").fetchall()
        request = db.execute("SELECT state FROM model_request_journal").fetchone()
    assert len(actions) == 1 and actions[0]["attempt"] == 1
    assert json.loads(actions[0]["usage_json"])["calls"] == 1
    assert actions[0]["state"] == "committed"
    assert request["state"] == "candidate"


def test_unknown_format_submission_is_not_repeated_after_journal_reconstruction(tmp_path):
    store = _store(tmp_path)
    provider = OfflineWire([format_rejection(), ProviderError("unknown", code="MODEL_TIMEOUT", retryable=True)])
    for _ in range(2):
        with pytest.raises(ProviderError, match="MODEL_SUBMISSION_UNKNOWN"):
            execute(store, provider)
    assert len(provider.wires) == 2
    with store.connect() as db:
        action = db.execute("SELECT attempt,external_intent FROM recovery_actions").fetchone()
    assert action["attempt"] == 1 and action["external_intent"] == 1


def test_unknown_compensation_cannot_be_erased_by_changing_model_configuration(tmp_path):
    store = _store(tmp_path)
    provider = OfflineWire([format_rejection(), ProviderError("unknown", code="MODEL_TIMEOUT")])
    with pytest.raises(ProviderError) as first:
        execute(store, provider)
    assert first.value.code == "MODEL_SUBMISSION_UNKNOWN"
    with pytest.raises(ProviderError) as changed:
        execute(store, provider, journal=_journal(store, config="different-config"))
    assert changed.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert len(provider.wires) == 2


def test_zero_additional_budget_stops_before_compensation_and_preserves_rejection(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    journal.repair_limits = RecoveryLimits(calls=0)
    provider = OfflineWire([format_rejection()])
    with pytest.raises(ProviderError) as raised:
        execute(store, provider, journal=journal)
    assert raised.value.code == "RECOVERY_INCIDENT_BUDGET_EXHAUSTED"
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT state FROM model_request_journal").fetchone()[0] == "rejected"


def test_cancel_after_first_known_rejection_cannot_send_the_compensation(tmp_path):
    store, cancelled = _store(tmp_path), Event()
    def reject_then_cancel():
        cancelled.set()
        return format_rejection()
    provider = OfflineWire([reject_then_cancel])
    with pytest.raises(ModelStageCancelled):
        execute(store, provider, cancel_check=cancelled.is_set)
    assert len(provider.wires) == 1


@pytest.mark.parametrize("error", [
    ProviderError("status missing", code="MODEL_RESPONSE_FORMAT_UNSUPPORTED"),
    ProviderError("server error", code="MODEL_RESPONSE_FORMAT_UNSUPPORTED", http_status=500),
    ProviderError("ordinary 400", code="MODEL_HTTP_400", http_status=400),
])
def test_unproven_format_rejection_never_opens_compensation_lane(tmp_path, error):
    store, provider = _store(tmp_path), OfflineWire([error])
    with pytest.raises(ProviderError) as raised:
        execute(store, provider)
    assert raised.value is error
    assert len(provider.wires) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0] == 0
