"""Uncredentialed loopback HTTP -> real Rust CLI -> Python journal contract."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from openthesis.providers import ModelConfig, ProviderError, RustModelGatewayProvider
from test_model_request_journal import _store, _journal, _registry, _request


EXE = os.environ.get("OPENTHESIS_TEST_GATEWAY_EXE", "")
pytestmark = pytest.mark.skipif(not EXE, reason="explicit current-worktree Rust CLI required")


@pytest.mark.parametrize("mode", ["complete", "incomplete", "cancel_call", "http400_stalled",
                                  "rate429", "heartbeat_timeout", "long_reasoning"])
def test_actual_rust_gateway_stream_and_journal(tmp_path, monkeypatch, mode):
    root = tmp_path / "model-center"
    root.mkdir()
    monkeypatch.setenv("OPENTHESIS_DATA_DIR", str(root.resolve()))
    bootstrap = {"operation":"generate","configured_model_id":"missing","system_prompt":"system",
                 "user_prompt":"full evidence","json_mode":True,"wire_contract":"text-single-request-v1"}
    first = subprocess.run([EXE,"--model-gateway"], input=json.dumps(bootstrap).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    assert json.loads(first.stdout)["error"]["code"] == "MODEL_CONFIGURATION_ERROR"
    requests, observed = [], []
    stopped = threading.Event()
    body = json.dumps({"analysis":"完整中文研究正文"},ensure_ascii=False)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if mode == "http400_stalled":
                self.send_response(400)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.flush()
                stopped.wait(6)
                return
            if mode == "rate429":
                payload = b'{"error":{"code":"rate_limit"}}'
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type","application/x-ndjson")
            self.end_headers()
            chunks = [{"message":{"thinking":"private reasoning text"},"done":False},
                      {"message":{"content":body},"done":False}]
            if mode in {"complete", "long_reasoning"}:
                chunks.append({"message":{"content":""},"done":True,"done_reason":"stop",
                               "prompt_eval_count":20,"eval_count":10,"prompt_eval_cached_count":7})
            try:
                if mode == "heartbeat_timeout":
                    while not stopped.wait(.05):
                        self.wfile.write(b'\n')
                        self.wfile.flush()
                    return
                if mode == "long_reasoning":
                    for _ in range(20):
                        self.wfile.write(b'{"message":{"thinking":"private reasoning text"},"done":false}\n')
                        self.wfile.flush()
                        if stopped.wait(.05):
                            return
                for chunk in chunks:
                    self.wfile.write((json.dumps(chunk,ensure_ascii=False)+"\n").encode())
                    self.wfile.flush()
                    time.sleep(.03)
                if mode == "cancel_call":
                    time.sleep(1)
            except (BrokenPipeError,ConnectionResetError,OSError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    worker = threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    db = sqlite3.connect(root/"model-center.db")
    db.execute("""INSERT INTO provider_connections(connection_id,provider_id,display_name,region,endpoint,has_secret)
        VALUES('local','ollama','Loopback fixture','local',?,0)""",(f"http://127.0.0.1:{server.server_port}",))
    db.execute("""INSERT INTO configured_models(configured_model_id,connection_id,model_id,alias,capabilities_json,billing_class,timeout_seconds)
        VALUES('local.fixture','local','offline-fixture','Loopback fixture','["text_chat","structured_json"]','local',5)""")
    db.commit()
    db.close()
    store = _store(tmp_path)
    journal = _journal(store)
    generation = journal.prepare(_registry(("analysis", ())),"material")["analysis"]
    def invoke():
        context = journal.transport_context()
        def activity(event):
            observed.append(event)
            store.model_activity.update(context,event)
            if mode == "cancel_call" and event["state"] == "content":
                assert store.model_activity.request_cancel("journal-run",context["request_id"],context["attempt_id"])
        provider = RustModelGatewayProvider(ModelConfig(configured_model_id="local.fixture",connection_id="local",timeout_seconds=5),gateway_path=EXE)
        provider = provider.for_model_events(context,activity,lambda:store.model_activity.is_cancel_requested(context))
        return provider.generate_once("full system","full original evidence")
    try:
        started = time.monotonic()
        if mode in {"complete", "long_reasoning"}:
            candidate = journal.execute(_request(),invoke,("analysis",generation))
            assert candidate["analysis"] == "完整中文研究正文"
            assert candidate["_response_meta"]["cache_read_input_tokens"] == 7
            assert candidate["_response_meta"]["cache_write_input_tokens"] is None
        elif mode in {"http400_stalled", "rate429", "heartbeat_timeout"}:
            with pytest.raises(ProviderError) as caught:
                journal.execute(_request(),invoke,("analysis",generation))
            expected = {"http400_stalled":"MODEL_HTTP_ERROR", "rate429":"MODEL_RATE_LIMITED",
                        "heartbeat_timeout":"MODEL_TIMEOUT"}[mode]
            assert caught.value.code == expected
            with store.connect() as connection:
                row = connection.execute("SELECT state,candidate_json FROM model_request_journal").fetchone()
            assert row["state"] == ("unknown" if mode == "heartbeat_timeout" else "rejected")
            assert row["candidate_json"] is None or mode == "rate429"
            if mode == "http400_stalled":
                assert time.monotonic() - started < 3, "error-body stall hid already received HTTP rejection"
            if mode == "heartbeat_timeout":
                assert time.monotonic() - started < 7, "heartbeats extended the fixed total deadline"
        else:
            with pytest.raises(ProviderError) as caught:
                journal.execute(_request(),invoke,("analysis",generation))
            assert caught.value.code == ("MODEL_CALL_CANCELLED" if mode == "cancel_call" else "MODEL_STREAM_INTERRUPTED")
            with store.connect() as connection:
                row = connection.execute("SELECT state,candidate_json,receipt_json FROM model_request_journal").fetchone()
            assert row["state"] == "unknown" and row["candidate_json"] is None
            assert json.loads(row["receipt_json"])["incomplete"] is True
        assert len(requests) == 1
        assert requests[0]["stream"] is True and "stream_options" not in requests[0]
        assert requests[0]["messages"][1]["content"] == "full original evidence"
        assert {item["state"] for item in observed} >= {"connecting","waiting_response"}
        if mode in {"complete", "incomplete", "cancel_call", "long_reasoning"}:
            assert {item["state"] for item in observed} >= {"reasoning","content"}
        assert "private reasoning" not in str(store.model_call_status("journal-run"))
        if mode == "complete" and os.environ.get("OPENTHESIS_EVENT_EVIDENCE_PATH"):
            Path(os.environ["OPENTHESIS_EVENT_EVIDENCE_PATH"]).write_text(json.dumps({
                "entry":"real Rust CLI -> Python model_request_journal -> status projection",
                "network":"loopback only","provider_calls":len(requests),"cloud_calls":0,
                "model_calls":store.model_call_status("journal-run"),"observed":observed},ensure_ascii=False,indent=2),encoding="utf-8")
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
