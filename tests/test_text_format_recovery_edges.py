"""Durable edges around the single-wire text format compensation lane."""

from threading import Event, Thread
import json

import pytest

from openthesis.providers import ProviderError
from openthesis.research_execution.model import ModelStageCancelled, ModelStageExecutor, ModelStageRequest
from openthesis.research_execution.requests import ModelRequestJournal

from test_model_request_journal import _journal, _registry, _store
from test_model_stage_executor import ImmediateScheduler
from test_text_format_recovery import OfflineWire, execute, format_rejection


def _bound_execute(store, provider, journal, *, cancel_check=None, generation=None):
    """Execute through the real stage seam while retaining the journal handle."""

    cancel_check = cancel_check or (lambda: False)
    if generation is None:
        generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    request = ModelStageRequest("analysis", "完整的系统提示", "complete source evidence")
    with journal.bind("analysis", generation):
        return ModelStageExecutor(
            provider,
            ImmediateScheduler(),
            cancel_check=cancel_check,
            request_journal=journal,
        ).execute(request)


def _journal_rows(store):
    with store.connect() as db:
        request = db.execute(
            "SELECT state,diagnostic_code,candidate_json FROM model_request_journal"
        ).fetchone()
        actions = db.execute(
            "SELECT state,external_intent,reservation_json,usage_json,validation_json,result_json FROM recovery_actions"
        ).fetchall()
    return request, actions


def test_compensation_wire_observes_persisted_rejection_intent_and_one_call_reservation(tmp_path):
    store = _store(tmp_path)
    observed = {}
    candidate = {"claims": [{"text": "supported finding"}], "structured_output_valid": True}

    def fallback_response():
        with store.connect() as db:
            request = db.execute(
                "SELECT state,diagnostic_code FROM model_request_journal"
            ).fetchone()
            action = db.execute(
                "SELECT external_intent,reservation_json FROM recovery_actions"
            ).fetchone()
        observed.update(
            request_state=request["state"],
            request_code=request["diagnostic_code"],
            external_intent=action["external_intent"],
            reserved_calls=json.loads(action["reservation_json"])["calls"],
        )
        return candidate

    assert execute(store, OfflineWire([format_rejection(400), fallback_response])) == candidate
    assert observed == {
        "request_state": "rejected",
        "request_code": "MODEL_RESPONSE_FORMAT_UNSUPPORTED",
        "external_intent": 1,
        "reserved_calls": 1,
    }


def test_invalid_fallback_candidate_is_audited_but_never_published_or_retried(tmp_path):
    store = _store(tmp_path)
    invalid = {"claims": [{"text": "unstructured"}], "structured_output_valid": False}
    provider = OfflineWire([format_rejection(), invalid])

    with pytest.raises(ProviderError) as raised:
        execute(store, provider)

    assert raised.value.code == "RECOVERY_CANDIDATE_REJECTED"
    assert len(provider.wires) == 2
    request, actions = _journal_rows(store)
    assert request["state"] == "rejected"
    assert request["candidate_json"] is not None
    assert len(actions) == 1
    assert actions[0]["result_json"] is not None
    assert json.loads(actions[0]["result_json"])["candidate"]["result"] == invalid


@pytest.mark.parametrize("tamper", ["proof_json", "proof_hash"])
def test_tampered_format_proof_cannot_open_compensation_after_restart(tmp_path, tamper):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    request = ModelStageRequest("analysis", "完整的系统提示", "complete source evidence")
    with journal.bind("analysis", generation):
        with pytest.raises(ProviderError) as first:
            journal.execute(request, lambda: (_ for _ in ()).throw(format_rejection(400)),
                            journal.capture_scope())
    assert first.value.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
    with store.connect() as db:
        if tamper == "proof_json":
            db.execute(
                "UPDATE model_request_journal SET candidate_json=? WHERE input_digest=?",
                ('{"schema":"model-format-rejection-v1","code":"MODEL_RESPONSE_FORMAT_UNSUPPORTED","http_status":422}',
                 request.input_digest),
            )
        else:
            db.execute(
                "UPDATE model_request_journal SET candidate_digest=? WHERE input_digest=?",
                ("0" * 64, request.input_digest),
            )

    restarted = _journal(store)
    provider = OfflineWire([{"claims": [{"text": "must not be sent"}], "structured_output_valid": True}])
    with pytest.raises(ProviderError) as raised:
        _bound_execute(store, provider, restarted)
    assert raised.value.code == "MODEL_SUBMISSION_UNKNOWN"
    assert provider.wires == []
    request_row, actions = _journal_rows(store)
    assert request_row["state"] == "rejected"
    assert actions == []


def test_generation_replacement_during_fallback_keeps_old_request_unpublished(tmp_path):
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]

    def replace_generation():
        journal.prepare(_registry(("analysis", ())), "replacement-material", invalidated=("analysis",))
        return {"claims": [{"text": "stale"}], "structured_output_valid": True}

    provider = OfflineWire([format_rejection(), replace_generation])
    with pytest.raises(ProviderError) as raised:
        _bound_execute(store, provider, journal, generation=generation)
    assert raised.value.code in {"RECOVERY_PLAN_STALE", "MODEL_REQUEST_GENERATION_STALE"}
    assert len(provider.wires) == 2
    request, actions = _journal_rows(store)
    assert request["state"] == "rejected"
    assert len(actions) == 1
    saved = json.loads(actions[0]["result_json"])
    assert saved["candidate"]["result"] == {"claims": [{"text": "stale"}], "structured_output_valid": True}
    assert saved["candidate"]["generation"] == generation
    validation = json.loads(actions[0]["validation_json"])
    assert validation["pending"] is True and validation["passed"] is False


def test_cancel_during_fallback_keeps_request_and_metering_without_publishing(tmp_path):
    store = _store(tmp_path)
    cancelled = Event()
    journal = _journal(store, cancelled=cancelled.is_set)
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]

    def cancel_then_return():
        cancelled.set()
        return {"claims": [{"text": "late"}], "structured_output_valid": True}

    provider = OfflineWire([format_rejection(), cancel_then_return])
    with pytest.raises(ModelStageCancelled):
        _bound_execute(store, provider, journal, cancel_check=cancelled.is_set, generation=generation)
    assert len(provider.wires) == 2
    request, actions = _journal_rows(store)
    assert request["state"] == "rejected"
    assert len(actions) == 1
    usage = json.loads(actions[0]["usage_json"])
    assert usage["calls"] == 1
    saved = json.loads(actions[0]["result_json"])
    assert saved["candidate"]["result"] == {"claims": [{"text": "late"}], "structured_output_valid": True}
    assert saved["candidate"]["generation"] == generation
    validation = json.loads(actions[0]["validation_json"])
    assert validation["pending"] is True and validation["passed"] is False


def test_two_threads_for_one_request_emit_at_most_one_fallback_wire(tmp_path):
    store = _store(tmp_path)
    class BlockingWire(OfflineWire):
        def __init__(self):
            super().__init__([
                format_rejection(),
                {"claims": [{"text": "only once"}], "structured_output_valid": True},
            ])
            self.fallback_started = Event()
            self.release_fallback = Event()

        def _invoke_gateway(self, request):
            self.wires.append(request)
            response = next(self.responses)
            if len(self.wires) == 2:
                self.fallback_started.set()
                assert self.release_fallback.wait(timeout=10)
            if callable(response):
                response = response()
            if isinstance(response, BaseException):
                raise response
            return response

    provider = BlockingWire()
    results = []

    def first_worker():
        try:
            results.append(("ok", execute(store, provider)))
        except ProviderError as exc:
            results.append(("first-error", exc.code))

    first = Thread(target=first_worker)
    first.start()
    assert provider.fallback_started.wait(timeout=10)

    second_result = []
    second_done = Event()

    def second_worker():
        try:
            execute(store, provider)
        except ProviderError as exc:
            second_result.append(exc.code)
        finally:
            second_done.set()

    second = Thread(target=second_worker)
    second.start()
    assert second_done.wait(timeout=10)
    assert second_result == ["MODEL_REQUEST_IN_PROGRESS"]
    provider.release_fallback.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert len(provider.wires) == 2
    assert results == [("ok", {"claims": [{"text": "only once"}], "structured_output_valid": True})]


def test_second_format_rejection_finishes_after_two_wires_without_loop(tmp_path):
    store = _store(tmp_path)
    provider = OfflineWire([format_rejection(400), format_rejection(422)])
    with pytest.raises(ProviderError) as raised:
        execute(store, provider)
    assert raised.value.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
    assert len(provider.wires) == 2
    request, actions = _journal_rows(store)
    assert request["state"] == "rejected"
    assert len(actions) == 1
    assert json.loads(actions[0]["usage_json"])["calls"] == 1


def test_format_fallback_lease_covers_single_180_second_wire(tmp_path):
    """A valid 31-second fallback must not expire the default 30-second lease."""

    store = _store(tmp_path)
    now = [1000.0]
    journal = ModelRequestJournal(
        store,
        "journal-run",
        "config-v1",
        timeout_seconds=180,
        cancel_check=lambda: False,
        clock=lambda: now[0],
    )
    generation = journal.prepare(_registry(("analysis", ())), "frozen-material")["analysis"]
    candidate = {"claims": [{"text": "delayed but valid"}], "structured_output_valid": True}

    def delayed_fallback():
        now[0] = 1031.0
        return candidate

    provider = OfflineWire([format_rejection(400), delayed_fallback])
    assert _bound_execute(store, provider, journal, generation=generation) == candidate
    assert len(provider.wires) == 2
    with store.connect() as db:
        incident = db.execute("SELECT usage_json FROM recovery_incidents").fetchone()
    usage = json.loads(incident["usage_json"])
    assert usage["calls"] == 1
    assert usage["seconds"] == 31.0
