"""Bounded in-memory progress projections; durable outcomes stay in journals."""
from __future__ import annotations

from copy import deepcopy
import threading
import time

TERMINAL = frozenset({"saved", "cancelled", "rejected", "rate_limited", "remote_unknown", "local_failure"})


class ModelActivityLedger:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.RLock()
        self._rows = {}
        self.revision = 0

    def begin(self, context):
        with self._lock:
            self.revision += 1
            now = self.clock()
            key = (context["request_id"], context["attempt_id"])
            self._rows[key] = {**context, "state": "queued", "started": now,
                "changed": now, "last_substantive": now, "first_content": None, "first_response": None,
                "content_bytes": 0, "reasoning_bytes": 0, "phase_seconds": {}, "seq": 0}
            # Only terminal historical observations may be evicted. Active
            # calls are bounded separately by the scheduler, not by UI limits.
            if len(self._rows) > 1024:
                for old_key, row in list(self._rows.items()):
                    if len(self._rows) <= 1024:
                        break
                    if row["state"] in TERMINAL:
                        del self._rows[old_key]

    def update(self, context, event):
        with self._lock:
            row = self._rows.get((context["request_id"], context["attempt_id"]))
            if not row or row["state"] in TERMINAL:
                return
            seq = event.get("seq")
            if seq is not None:
                if type(seq) is not int or seq <= row["seq"]:
                    return
                row["seq"] = seq
            state = event["state"]
            if isinstance(event.get("usage"), dict):
                self.revision += 1
                row["usage"] = {name: value for name, value in event["usage"].items()
                    if name in {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                                "reasoning_tokens", "answer_tokens"}
                    and (value is None or type(value) is int and value >= 0)}
            now = self.clock()
            if state == "connected" and row["first_response"] is None:
                row["first_response"] = now
            # Heartbeats change the observable connection state, not evidence of
            # substantive progress. Only content/reasoning bytes reset idle.
            self.revision += 1
            if state in {"content", "reasoning"}:
                count = event.get("bytes", 0)
                if type(count) is int and count > 0:
                    row[state + "_bytes"] += count
                    row["last_substantive"] = now
                    if state == "content" and row["first_content"] is None:
                        row["first_content"] = now
            if state != row["state"]:
                old = row["state"]
                row["phase_seconds"][old] = row["phase_seconds"].get(old, 0) + max(0, now - row["changed"])
                row["state"], row["changed"] = state, now

    def describe(self, context, metadata):
        """Host-owned non-secret model identity, never prompts or credentials."""
        with self._lock:
            self.revision += 1
            row = self._rows.get((context["request_id"], context["attempt_id"]))
            if row is None:
                return
            for name in ("configured_model_id", "model_id", "alias", "provider_id"):
                value = metadata.get(name)
                if isinstance(value, str) and value and len(value) <= 256 and all(ord(c) >= 32 for c in value):
                    row[name] = value

    def snapshot(self, run_id):
        with self._lock:
            now = self.clock()
            result = []
            for row in self._rows.values():
                if row["run_id"] != run_id:
                    continue
                terminal = row["state"] in TERMINAL
                end = row["changed"] if terminal else now
                phases = deepcopy(row["phase_seconds"])
                if not terminal:
                    phases[row["state"]] = phases.get(row["state"], 0) + max(0, now - row["changed"])
                result.append({key: row[key] for key in ("request_id", "attempt_id", "stage_id", "generation",
                    "state", "content_bytes", "reasoning_bytes")} | {
                    "cancel_requested": bool(row.get("cancel_requested")),
                    "elapsed_seconds": max(0, end - row["started"]),
                    "idle_seconds": max(0, end - row["last_substantive"]),
                    "first_content_seconds": None if row["first_content"] is None else row["first_content"] - row["started"],
                    "first_response_seconds": None if row["first_response"] is None else row["first_response"] - row["started"],
                    "phase_seconds": phases,
                    "usage": deepcopy(row.get("usage", {})),
                    **{name: row[name] for name in ("configured_model_id", "model_id", "alias", "provider_id") if name in row},
                    "remote_status_unconfirmed": not terminal and now - row["last_substantive"] >= 30})
            return result

    def request_cancel(self, run_id, request_id, attempt_id):
        with self._lock:
            row = self._rows.get((request_id, attempt_id))
            if not row or row["run_id"] != run_id or row["state"] in TERMINAL:
                return False
            row["cancel_requested"] = True
            self.revision += 1
            return True

    def is_cancel_requested(self, context):
        with self._lock:
            row = self._rows.get((context["request_id"], context["attempt_id"]))
            return bool(row and row.get("cancel_requested"))
