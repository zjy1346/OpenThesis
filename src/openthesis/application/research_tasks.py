"""One durable execution slot for research writers, not another workflow engine.

The store owns FIFO/atomic claims. This module owns one executor thread and
process liveness, and never interprets prompts, facts, or provider retries.
"""
from __future__ import annotations

import ctypes
import os
import threading
import uuid
from typing import Callable


class ResearchTaskYield(BaseException):
    """Unwind all local executors before releasing a user-input checkpoint.

This deliberately is not an ordinary adapter error: financial source fallbacks
must not swallow it and start a different upload route. Their finally blocks
still run, and the dispatcher releases its slot only after execute returns.
"""

    def __init__(self, reason: str, *, preview: dict | None = None, interrupted: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.preview = preview
        self.interrupted = interrupted


def _windows_process_birth(pid: int) -> tuple[bool | None, str]:
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = api.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        # Access denied is not proof that another execution owner is dead.
        return (False if ctypes.get_last_error() == 87 else None), ""
    try:
        if api.WaitForSingleObject(handle, 0) == 0:
            return False, ""
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None, ""
        return True, str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def process_identity(pid: int) -> tuple[bool | None, str]:
    if os.name == "nt":
        return _windows_process_birth(pid)
    try:
        # PID birth identity prevents reuse being mistaken for an old owner.
        with open(f"/proc/{pid}/stat", encoding="utf-8") as source:
            body = source.read().rsplit(")", 1)[1].split()
        return True, body[19]
    except FileNotFoundError:
        return False, ""
    except (OSError, IndexError):
        return None, ""


def new_execution_owner() -> str:
    _live, birth = process_identity(os.getpid())
    return f"{os.getpid()}:{birth or 'unknown'}:{uuid.uuid4().hex}"


def owner_may_be_alive(owner: str) -> bool:
    try:
        pid_text, birth, _nonce = owner.split(":", 2)
        pid = int(pid_text)
        if pid <= 0:
            return True
    except (ValueError, AttributeError):
        return True
    alive, actual_birth = process_identity(pid)
    if alive is False:
        return False
    if alive is None or not actual_birth or birth == "unknown":
        return True
    return birth == actual_birth


class ResearchTaskDispatcher:
    def __init__(self, storage, execute: Callable[[dict], None], *, on_failure: Callable[[dict, BaseException], None]):
        self.storage = storage
        self.execute = execute
        self.on_failure = on_failure
        self.owner = new_execution_owner()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def recover(self) -> dict:
        live = {owner for owner in self.storage.research_queue_owners() if owner_may_be_alive(owner)}
        return self.storage.recover_research_jobs(live)

    def wake(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._dispatch, name="openthesis-research-queue", daemon=True)
                self._thread.start()
            self._wake.set()

    def reopen_after_shutdown(self) -> None:
        """Explicitly re-enable dispatch after a prior shutdown has drained."""
        with self._lock:
            if not self._stop.is_set():
                return
            thread = self._thread
            if thread is not None and thread.is_alive():
                raise ValueError("RESEARCH_TASKS_STILL_STOPPING")
            self._thread = None
            self._wake.clear()
            self._stop.clear()

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            record = self.storage.claim_next_research_job(self.owner)
            if record is None:
                pending = self.storage.list_research_jobs(limit=1, states=["queued"])
                if pending["counts"]["queued"] and not pending["queue_paused"]:
                    # Another live sidecar owns the slot. This is the only
                    # idle case needing a bounded cross-process claim poll.
                    self._wake.wait(1.0)
                    continue
                with self._lock:
                    if self._wake.is_set():
                        continue
                    self._thread = None
                    return
            try:
                self.execute(record)
            except BaseException as error:
                try:
                    self.on_failure(record, error)
                except Exception:
                    # A broken notification/projection for one task must not
                    # retain its slot forever or replay it. Preserve the row
                    # for explicit repair; other queued tasks can still run.
                    snapshot = self.storage.get_research_job(record["job_id"])
                    if snapshot is None:
                        raise
                    snapshot.update(state="interrupted", blocked_reason="TASK_FINALIZATION_FAILED",
                                    error_code="TASK_FINALIZATION_FAILED", revision=snapshot.get("revision", 0) + 1)
                    self.storage.save_research_job(snapshot)
            finally:
                # A visible terminal checkpoint is not proof that its writer
                # has returned. The durable owner stays until this finally.
                self.storage.release_research_job(record["job_id"], self.owner)

    def close(self, *, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()
