import json
import sys
import time

import pytest

from openthesis.gateway_events import GatewayEventReader, GatewayFrameError
from openthesis.gateway_events import receive_gateway_process
from openthesis.model_activity import ModelActivityLedger
from openthesis.providers import ProviderError


CONTEXT = {"protocol": "model-events-v1", "request_id": "request:1", "run_id": "run",
           "stage_id": "stage", "generation": 1, "attempt_id": "owner"}


def frame(seq, kind, data, context=None):
    return (json.dumps({"protocol": "model-events-v1", "context": context or CONTEXT,
                        "seq": seq, "kind": kind, "data": data}, ensure_ascii=False) + "\n").encode()


def test_incremental_utf8_identity_terminal_and_no_reasoning_leak():
    activities = []
    reader = GatewayEventReader(CONTEXT, activities.append)
    wire = (frame(1, "activity", {"state": "reasoning", "bytes": 100, "thinking": "not UI"})
            + frame(2, "content", {"text": '{"分析":"公司"'})
            + frame(3, "terminal", {"response": {"ok": False}, "partial_content": '{"分析":"公司"'}))
    for byte in wire:
        reader.push(bytes([byte]))
    assert reader.finish() == {"ok": False}
    assert reader.partial_content == '{"分析":"公司"'
    assert activities == [{"state": "reasoning", "bytes": 100, "seq": 1}]


@pytest.mark.parametrize("context,seq", [({**CONTEXT, "generation": 2}, 1),
                                       ({**CONTEXT, "attempt_id": "other"}, 1), (CONTEXT, 2)])
def test_stale_or_out_of_order_frames_rejected(context, seq):
    with pytest.raises(GatewayFrameError, match="IDENTITY"):
        GatewayEventReader(CONTEXT).push(frame(seq, "activity", {"state": "content"}, context))


def test_partial_without_terminal_never_promoted_even_if_valid_json():
    reader = GatewayEventReader(CONTEXT)
    reader.push(frame(1, "content", {"text": '{"complete_looking":true}'}))
    assert reader.partial_content
    with pytest.raises(GatewayFrameError, match="INTERRUPTED"):
        reader.finish()


def test_legacy_response_still_supported_and_terminal_cannot_be_overwritten():
    reader = GatewayEventReader(CONTEXT)
    reader.push(b'{"ok":true,"result":{}}')
    assert reader.finish()["ok"] is True
    reader = GatewayEventReader(CONTEXT)
    reader.push(frame(1, "terminal", {"response": {"ok": True}}))
    with pytest.raises(GatewayFrameError):
        reader.push(frame(2, "terminal", {"response": {"ok": False}}))


def test_real_subprocess_reports_activity_before_completion_and_keeps_partial_on_cancel():
    observed = []
    # The subprocess is an uncredentialed local CLI peer, not a provider mock
    # returning a final dict. It writes real fragmented bytes and then stalls.
    script = ("import sys,time; sys.stdin.buffer.read(); "
              f"sys.stdout.buffer.write({frame(1, 'content', {'text': '{body'})!r}); sys.stdout.flush(); "
              f"sys.stdout.buffer.write({frame(2, 'activity', {'state': 'content', 'bytes': 5})!r}); sys.stdout.flush(); "
              "time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(GatewayFrameError, match="CANCELLED") as caught:
        receive_gateway_process([sys.executable, "-c", script], b"{}", 5, 0, CONTEXT,
                                observed.append, lambda: bool(observed))
    assert caught.value.partial_content == "{body"
    assert observed == [{"state": "content", "bytes": 5, "seq": 2}]
    assert time.monotonic() - started < 4


def test_heartbeat_does_not_reset_meaningful_idle_and_late_event_cannot_reopen():
    now = [0.0]
    ledger = ModelActivityLedger(lambda: now[0])
    ledger.begin(CONTEXT)
    ledger.update(CONTEXT, {"state": "waiting_response", "seq": 1})
    now[0] = 31
    ledger.update(CONTEXT, {"state": "heartbeat", "seq": 2})
    row = ledger.snapshot("run")[0]
    assert row["state"] == "heartbeat"
    assert row["remote_status_unconfirmed"] and row["idle_seconds"] == 31
    ledger.update(CONTEXT, {"state": "reasoning", "bytes": 9, "seq": 3})
    assert not ledger.snapshot("run")[0]["remote_status_unconfirmed"]
    now[0] = 65
    ledger.update(CONTEXT, {"state": "heartbeat", "seq": 4})
    row = ledger.snapshot("run")[0]
    assert row["state"] == "heartbeat" and row["idle_seconds"] == 34
    assert row["reasoning_bytes"] == 9 and row["remote_status_unconfirmed"]
    assert row["phase_seconds"]["reasoning"] == 34
    ledger.update(CONTEXT, {"state": "cancelled"})
    ledger.update(CONTEXT, {"state": "content", "bytes": 999, "seq": 5})
    row = ledger.snapshot("run")[0]
    assert row["state"] == "cancelled" and row["content_bytes"] == 0


def test_partial_durable_record_is_not_a_candidate_and_still_blocks_resubmission(tmp_path):
    from test_model_request_journal import _store, _journal, _registry, _request
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "material")["analysis"]
    seen = []
    def interrupted():
        seen.append(journal.transport_context())
        raise ProviderError("interrupted", code="MODEL_STREAM_INTERRUPTED",
                            partial_content='{"analysis":"keep", "api_key":"secret"')
    with pytest.raises(ProviderError):
        journal.execute(_request(), interrupted, ("analysis", generation))
    assert journal.transport_context() is None
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
        chunks = db.execute("SELECT content FROM model_response_chunks ORDER BY ordinal").fetchall()
    assert row["state"] == "unknown" and row["candidate_json"] is None
    assert row["response_received"] == 0
    assert json.loads(row["receipt_json"])["incomplete"] is True
    partial = "".join(row[0] for row in chunks)
    assert '"analysis":"keep"' in partial and "secret" not in partial
    assert seen[0]["generation"] == generation
    other = _journal(store, config="changed-model")
    gen2 = other.prepare(_registry(("analysis", ())), "material")["analysis"]
    with pytest.raises(ProviderError, match="unresolved"):
        other.execute(_request(), lambda: pytest.fail("no second wire"), ("analysis", gen2))


@pytest.mark.parametrize("code,status", [("MODEL_UNAUTHORIZED",401), ("MODEL_QUOTA_EXHAUSTED",429), ("MODEL_HTTP_ERROR",400)])
def test_known_provider_rejections_are_not_mislabeled_as_unknown_submission(tmp_path, code, status):
    from test_model_request_journal import _store, _journal, _registry, _request
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "material")["analysis"]
    def rejected():
        raise ProviderError("safe error", code=code, http_status=status)
    with pytest.raises(ProviderError):
        journal.execute(_request(), rejected, ("analysis", generation))
    with store.connect() as db:
        row = db.execute("SELECT * FROM model_request_journal").fetchone()
    assert row["state"] == "rejected" and row["diagnostic_code"] == code
    # A changed model can address a proven 4xx rejection, but not an unknown
    # submitted stream. No automatic network retry is made by the journal.
    other = _journal(store, config="new-model")
    gen2 = other.prepare(_registry(("analysis", ())), "material")["analysis"]
    assert other.execute(_request(), lambda: {"analysis":"new candidate"}, ("analysis",gen2))["analysis"]


def test_provider_usage_receipt_survives_restart_and_missing_counts_stay_unknown(tmp_path):
    from test_model_request_journal import _store, _journal, _registry, _request
    from openthesis.research_execution.responses import received_response
    from openthesis.storage import Storage
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "material")["analysis"]
    journal.execute(_request(), lambda: received_response({"analysis":"candidate"}, {
        "input_tokens":123,"output_tokens":8,"cache_read_input_tokens":0,"cache_write_input_tokens":None}), ("analysis",generation))
    assert store.model_call_status("journal-run")[0]["usage"] == {
        "input_tokens":123,"output_tokens":8,"cache_read_input_tokens":0,"cache_write_input_tokens":None,
        "reasoning_tokens":None,"answer_tokens":None}
    restarted = Storage(store.data_dir)
    assert restarted.model_call_status("journal-run")[0]["usage"] == store.model_call_status("journal-run")[0]["usage"]


def test_interrupted_format_repair_retains_partial_without_clearing_unknown_guard(tmp_path):
    from test_model_request_journal import _store
    from test_text_format_recovery import OfflineWire, execute, format_rejection
    from openthesis.resilience.payloads import load_payload
    store = _store(tmp_path)
    provider = OfflineWire([format_rejection(), ProviderError("interrupted", code="MODEL_STREAM_INTERRUPTED",
        partial_content='{"analysis":"partial repair", "api_key":"private-secret"')])
    for _ in range(2):
        with pytest.raises(ProviderError, match="MODEL_SUBMISSION_UNKNOWN"):
            execute(store, provider)
    assert len(provider.wires) == 2
    with store.connect() as db:
        row = db.execute("SELECT * FROM recovery_actions").fetchone()
        events = [load_payload(db, event[0]) for event in db.execute("SELECT payload_json FROM recovery_events")]
    partials = [event for event in events if event.get("code") == "RECOVERY_RESPONSE_INCOMPLETE"]
    assert len(partials) == 1
    assert '"analysis":"partial repair"' in partials[0]["partial_content"]
    assert "private-secret" not in partials[0]["partial_content"]
    assert row["response_received"] == 0 and row["external_intent"] == 1
    assert row["result_json"] is None


def test_known_bad_request_during_repair_settles_without_unknown_resubmission_block(tmp_path):
    from test_model_request_journal import _store
    from test_text_format_recovery import OfflineWire, execute, format_rejection
    store = _store(tmp_path)
    provider = OfflineWire([format_rejection(), ProviderError("bad request", code="MODEL_HTTP_ERROR", http_status=400)])
    with pytest.raises(ProviderError) as raised:
        execute(store, provider)
    assert raised.value.code == "MODEL_HTTP_ERROR"
    with store.connect() as db:
        row = db.execute("SELECT * FROM recovery_actions").fetchone()
    assert row["external_intent"] == 0 and row["response_received"] == 0
    assert len(provider.wires) == 2


def test_repair_usage_belongs_to_its_own_physical_call_after_restart(tmp_path):
    from test_model_request_journal import _store
    from test_text_format_recovery import OfflineWire, execute, format_rejection
    from openthesis.research_execution.responses import received_response
    from openthesis.storage import Storage
    store = _store(tmp_path)
    response = received_response({"analysis": "repaired response"}, {
        "configured_model_id": "offline-model", "alias": "Offline model",
        "input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5})
    execute(store, OfflineWire([format_rejection(), response]))
    calls = Storage(store.data_dir).model_call_status("journal-run")
    assert len(calls) == 2
    initial = next(call for call in calls if ":repair:" not in call["request_id"])
    repaired = next(call for call in calls if ":repair:" in call["request_id"])
    assert initial["state"] == "rejected" and initial["usage"]["input_tokens"] is None
    assert repaired["state"] == "saved" and repaired["usage"]["input_tokens"] == 10
    assert repaired["usage"]["cache_write_input_tokens"] is None
    assert repaired["alias"] == "Offline model"


def test_queued_call_can_cancel_without_waiting_for_peer_or_sending_wire(tmp_path):
    import threading
    from test_model_request_journal import _store, _journal, _registry, _request
    from test_model_stage_executor import RecordingProvider
    from openthesis.model_scheduler import ModelRequestScheduler
    from openthesis.research_execution.model import ModelStageExecutor
    store, provider = _store(tmp_path), RecordingProvider()
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())), "material")["analysis"]
    scheduler = ModelRequestScheduler(1)
    completed, errors = threading.Event(), []
    def run():
        try:
            with journal.bind("analysis", generation):
                ModelStageExecutor(provider, scheduler, request_journal=journal).execute(_request())
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()
    with scheduler.slot():
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        until = time.monotonic() + 3
        while not store.model_activity.snapshot("journal-run") and time.monotonic() < until:
            completed.wait(.01)
        call = store.model_activity.snapshot("journal-run")[0]
        assert store.model_activity.request_cancel("journal-run", call["request_id"], call["attempt_id"])
        try:
            assert completed.wait(1), "cancelled queued request still waited for its busy peer"
            assert len(errors) == 1 and errors[0].code == "MODEL_CALL_CANCELLED_BEFORE_SEND"
            assert scheduler.snapshot().active == 1
            assert not provider.requests
        finally:
            # Release peer's slot even when the red test reproduces the bug.
            store.model_activity.request_cancel("journal-run", call["request_id"], call["attempt_id"])
    worker.join(3)


def test_each_confirmed_rate_retry_has_a_distinct_attempt_and_restart_usage(tmp_path):
    from test_model_request_journal import _store
    from test_text_format_recovery import OfflineWire, execute
    from test_text_transient_recovery import Clock, limited, journal_for
    from openthesis.research_execution.responses import received_response
    from openthesis.storage import Storage
    store, clock, contexts = _store(tmp_path), Clock(), []
    class Wire(OfflineWire):
        def _invoke_gateway(self, request):
            contexts.append(dict(self._event_context))
            return super()._invoke_gateway(request)
    provider = Wire([limited(), limited(), received_response({"analysis": "complete"}, {
        "input_tokens": 30, "output_tokens": 2})])
    execute(store, provider, journal=journal_for(store, clock))
    assert len({item["attempt_id"] for item in contexts}) == 3
    calls = Storage(store.data_dir).model_call_status("journal-run")
    assert len(calls) == 3
    assert sum(call["state"] == "saved" for call in calls) == 1
    assert sum(call["usage"]["input_tokens"] or 0 for call in calls) == 30
