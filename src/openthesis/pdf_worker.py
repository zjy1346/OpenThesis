"""Killable PDF IPC. Runtime evidence is separate from financial validation.

This module deliberately has no business-service or native-library imports.
Both parsers use this boundary; a failed spawn never runs native work in RPC.
"""
from __future__ import annotations

import multiprocessing as mp
import re
import threading
import time
import sys
from typing import Any, Callable

_local = threading.local()
_runtime_failures = {}
_runtime_lock = threading.Lock()


class PdfWorkerFailure(RuntimeError):
    def __init__(self, code, diagnostic):
        self.code, self.diagnostic = code, dict(diagnostic)
        super().__init__(code)


def current_runtime_failure() -> bool:
    """Only errors actually observed in this process/build, not old run labels."""
    prefix = (sys.executable, getattr(sys, '_MEIPASS', ''))
    with _runtime_lock:
        return any(key[:2] == prefix for key in _runtime_failures)


def pdf_phase(phase: str, *, backend: str = "", version: str = "") -> None:
    _local.phase = phase
    sender = getattr(_local, "sender", None)
    if sender is not None:
        sender.send(("phase", {"phase": phase, "backend": backend, "version": version}))


def _exception_code(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
        return code
    if isinstance(exc, (ImportError, ModuleNotFoundError)) or (
            isinstance(exc, OSError) and getattr(_local, 'phase', '') == 'import'):
        return "PDF_BACKEND_IMPORT_FAILED"
    if isinstance(exc, FileNotFoundError):
        return "PDF_SOURCE_NOT_FOUND"
    if isinstance(exc, PermissionError):
        return "PDF_SOURCE_ACCESS_DENIED"
    if getattr(_local, 'phase', '') == 'open':
        return "PDF_SOURCE_INVALID"
    if getattr(_local, 'phase', '') == 'extract':
        return "PDF_TEXT_EXTRACTION_FAILED"
    return "PDFIUM_WORKER_ERROR"


def _entry(worker, args, sender):
    _local.sender = sender
    _local.phase = 'ready'
    try:
        sender.send(("ready", None))
        result = worker(*args)
        pdf_phase("serialize")
        sender.send(("result", result))
    except BaseException as exc:
        # Never send str(exc): native errors can contain paths, URLs or source
        # text. These controlled fields suffice to distinguish loader faults.
        detail = {"exception_type": type(exc).__name__, "errno": getattr(exc, "errno", None)}
        module = getattr(exc, "name", "")
        if isinstance(module, str) and re.fullmatch(r"(?:pypdfium2(?:_raw)?|pypdf)(?:\.[a-zA-Z0-9_]+)*", module):
            detail["module"] = module[:120]
        try:
            sender.send(("error", {"code": _exception_code(exc), **detail}))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()
        _local.sender = None


def run_pdf_worker(worker: Callable[..., Any], *args: Any, timeout_seconds=45., cancel_check=None,
                   diagnostic_callback=None, operation="", backend="pdfium", page_range=None):
    started = time.monotonic()
    diagnostic = {"operation": operation or worker.__name__, "phase": "startup", "backend": backend,
        "raw_error_code": "", "exception_type": "", "exit_code": None, "ready_received": False,
        "artifact_received": False, "page_range": page_range}
    process = None
    receiver = sender = None
    result = None
    error = None
    # A process-local cache cannot outlive a repaired install/restart. Keyed by
    # frozen runtime identity, not source hash: a missing backend affects every
    # file. Source errors and timeouts are never cached here.
    runtime_key = (sys.executable, getattr(sys, '_MEIPASS', ''), backend)
    if getattr(sys, 'frozen', False):
        with _runtime_lock:
            cached = _runtime_failures.get(runtime_key) or _runtime_failures.get(runtime_key[:2] + ('spawn',))
        if cached:
            diagnostic.update(cached, runtime_failure_cached=True)
            if diagnostic_callback:
                diagnostic_callback(dict(diagnostic))
            return None, cached['raw_error_code']
    def receive(message):
        nonlocal result, error
        kind, value = message
        if kind == "ready":
            diagnostic.update(phase="ready", ready_received=True)
            _local.phase = 'ready'
        elif kind == "phase":
            diagnostic.update({key: val for key, val in value.items() if val})
        elif kind == "error":
            error = value.pop("code")
            diagnostic.update(value)
            return True
        elif kind == "result":
            result = value
            diagnostic["artifact_received"] = True
            return True
        else:
            error = "PDFIUM_WORKER_PROTOCOL_ERROR"
            return True
        return False
    try:
        if mp.current_process().daemon:
            # Already inside the per-window killable child, never the UI/RPC.
            diagnostic.update(phase="ready", ready_received=True)
            try:
                result = worker(*args)
                diagnostic["artifact_received"] = True
            except BaseException as exc:
                error = _exception_code(exc)
                diagnostic["exception_type"] = type(exc).__name__
            return result, error
        try:
            context = mp.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_entry, args=(worker, tuple(args), sender), name="pdf-native-worker", daemon=True)
            process.start()
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            diagnostic["exception_type"] = type(exc).__name__
            error = "PDFIUM_WORKER_START_FAILED"
            return None, error
        sender.close()
        deadline = time.monotonic() + max(.05, float(timeout_seconds))
        while True:
            if cancel_check and cancel_check():
                error = "PDFIUM_WORKER_CANCELLED"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "PDFIUM_WORKER_TIMEOUT"
                break
            if receiver.poll(min(.05, remaining)):
                try:
                    if receive(receiver.recv()):
                        break
                except (EOFError, OSError, ValueError, TypeError):
                    error = "PDFIUM_WORKER_EXIT"
                    break
            elif not process.is_alive():
                error = "PDFIUM_WORKER_EXIT"
                break
        # A received artifact is not an abnormal exit just because native
        # cleanup lasts over 500 ms. The remaining deadline bounds cleanup.
        if diagnostic["artifact_received"]:
            process.join(timeout=min(2., max(0., deadline - time.monotonic())))
            if not process.is_alive() and process.exitcode not in (0, None):
                result, error = None, "PDFIUM_WORKER_EXIT"
        return result, error
    finally:
        if process is not None:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(1.)
                if process.is_alive():
                    process.kill()
                    process.join(1.)
                diagnostic["exit_code"] = getattr(process, 'exitcode', None)
            except (AssertionError, OSError, ValueError, TypeError, AttributeError):
                pass
        for pipe in (sender, receiver):
            if pipe is not None:
                pipe.close()
        diagnostic.update(raw_error_code=error or "", elapsed_seconds=round(time.monotonic() - started, 4))
        if getattr(sys, 'frozen', False) and error in {'PDF_BACKEND_IMPORT_FAILED', 'PDFIUM_WORKER_START_FAILED'}:
            key = runtime_key[:2] + ('spawn',) if error == 'PDFIUM_WORKER_START_FAILED' else runtime_key
            with _runtime_lock:
                _runtime_failures[key] = {key: diagnostic[key] for key in
                    ('raw_error_code', 'exception_type', 'phase', 'ready_received', 'artifact_received', 'exit_code')}
        if diagnostic_callback:
            diagnostic_callback(dict(diagnostic))
