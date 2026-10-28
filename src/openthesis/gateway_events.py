"""Versioned CLI framing, independent of any provider's network protocol.

Raw answer deltas stay in this bounded transport buffer, never in progress/UI
events. Reasoning text is not part of this protocol. A terminal is required to
promote a response; partial JSON is not repaired or treated as a candidate.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Callable

PROTOCOL = "model-events-v1"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_FRAME_BYTES = 7 * MAX_BODY_BYTES
MAX_WIRE_BYTES = 8 * MAX_BODY_BYTES
_STATES = frozenset({"connecting", "waiting_response", "connected", "content", "reasoning", "heartbeat"})


class GatewayFrameError(ValueError):
    def __init__(self, code: str, partial_content: str = ""):
        super().__init__(code)
        self.partial_content = partial_content


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
        result[key] = value
    return result


class GatewayEventReader:
    def __init__(self, context: dict, activity: Callable[[dict], None] | None = None):
        self.context = dict(context)
        self.activity = activity
        self.buffer = bytearray()
        self.total = 0
        self.seq = 0
        self.terminal: dict | None = None
        self._pieces: list[str] = []
        self._body_bytes = 0
        self._legacy: dict | None = None

    @property
    def partial_content(self) -> str:
        return "".join(self._pieces)

    def push(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.total > MAX_WIRE_BYTES:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_LIMIT")
        self.buffer.extend(chunk)
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                if len(self.buffer) > MAX_FRAME_BYTES:
                    raise GatewayFrameError("MODEL_GATEWAY_EVENT_LIMIT")
                return
            line = bytes(self.buffer[:newline])
            del self.buffer[:newline + 1]
            self._line(line)

    def _line(self, line: bytes) -> None:
        if not line.strip():
            return
        if len(line) > MAX_FRAME_BYTES or self.terminal is not None or self._legacy is not None:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
        try:
            value = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID") from exc
        if not isinstance(value, dict):
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
        if "protocol" not in value:
            if self.seq or not isinstance(value.get("ok"), bool):
                raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
            self._legacy = value
            return
        seq = value.get("seq")
        if (value.get("protocol") != PROTOCOL or value.get("context") != self.context
                or type(seq) is not int or seq != self.seq + 1):
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_IDENTITY")
        self.seq = seq
        kind, data = value.get("kind"), value.get("data")
        if not isinstance(data, dict):
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
        if kind == "activity":
            state, count = data.get("state"), data.get("bytes", 0)
            if state not in _STATES or type(count) is not int or not 0 <= count <= MAX_BODY_BYTES:
                raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
            if self.activity:
                try:
                    self.activity({"state": state, "bytes": count, "seq": seq})
                except Exception:
                    # UI bookkeeping cannot interrupt an already paid wire.
                    pass
        elif kind == "content":
            text = data.get("text")
            if not isinstance(text, str):
                raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
            self._append(text)
        elif kind == "terminal":
            if not isinstance(data.get("response"), dict):
                raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
            partial = data.get("partial_content")
            if partial is not None:
                if not isinstance(partial, str):
                    raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")
                # Terminal carries the authoritative partial after a decoder
                # error. It replaces, not duplicates, earlier content deltas.
                self._pieces, self._body_bytes = [], 0
                self._append(partial)
            self.terminal = data["response"]
        else:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_INVALID")

    def _append(self, text: str) -> None:
        self._body_bytes += len(text.encode("utf-8"))
        if self._body_bytes > MAX_BODY_BYTES:
            raise GatewayFrameError("MODEL_GATEWAY_EVENT_LIMIT")
        self._pieces.append(text)

    def finish(self) -> dict:
        if self.buffer.strip():
            self._line(bytes(self.buffer))
            self.buffer.clear()
        response = self.terminal if self.terminal is not None else self._legacy
        if response is None:
            raise GatewayFrameError("MODEL_STREAM_INTERRUPTED")
        return response


def receive_gateway_process(args: list[str], encoded: bytes, timeout: float, creationflags: int,
                            context: dict, activity=None, cancel_check=None):
    """Incremental bounded read with independently supervised stdin and stdout.

    Neither a pipe write nor a blocked read can hide cancellation. The deadline
    is a fixed total budget; remote heartbeats never extend it. No retry here.
    """
    started = time.monotonic()
    reader = GatewayEventReader(context, activity)
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=creationflags)
    chunks: queue.Queue = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def put(value):
        while not stopped.is_set():
            try:
                chunks.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def write_input():
        try:
            process.stdin.write(encoded)
            process.stdin.close()
        except (OSError, ValueError):
            put(GatewayFrameError("MODEL_GATEWAY_PROTOCOL_ERROR"))

    def read_output():
        try:
            while not stopped.is_set():
                chunk = process.stdout.read1(8192)
                if not chunk:
                    break
                put(chunk)
        except (OSError, ValueError):
            put(GatewayFrameError("MODEL_STREAM_INTERRUPTED"))
        finally:
            put(None)

    writers = threading.Thread(target=write_input, daemon=True, name="gateway-input")
    workers = threading.Thread(target=read_output, daemon=True, name="gateway-events")
    writers.start()
    workers.start()
    try:
        eof = False
        while not eof or process.poll() is None:
            if cancel_check and cancel_check():
                raise GatewayFrameError("MODEL_CANCELLED")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise GatewayFrameError("MODEL_TIMEOUT")
            try:
                item = chunks.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if isinstance(item, Exception):
                raise item
            if item is None:
                eof = True
            else:
                reader.push(item)
        response = reader.finish()
        completed = subprocess.CompletedProcess(args, int(process.returncode or 0),
            json.dumps(response, ensure_ascii=False).encode("utf-8"), b"")
        completed.partial_content = reader.partial_content
        return completed
    except GatewayFrameError as exc:
        exc.partial_content = reader.partial_content
        raise
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        workers.join(timeout=1)
        writers.join(timeout=1)
        if not workers.is_alive():
            process.stdout.close()
