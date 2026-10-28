"""Bounded native text prepass for explicit statement-wide unit evidence."""
from __future__ import annotations

import multiprocessing as mp
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass
from typing import Any

from ..disclosure_index import cached_page_texts, remember_page_texts
from ..vision_financials import _run_pdfium_isolated
from ..pdf_worker import pdf_phase
from .document_units import DocumentUnitResolution, discover_document_units
from .statement_periods import StatementPeriodDeclaration, StatementPeriodGap, statement_period_declarations, statement_period_gaps


@dataclass(frozen=True, slots=True)
class PdfDocumentContext:
    units: DocumentUnitResolution = DocumentUnitResolution()
    periods: tuple[StatementPeriodDeclaration, ...] = ()
    period_gaps: tuple[StatementPeriodGap, ...] = ()
    page_texts: tuple[str, ...] = ()
    diagnostic_code: str = ""
    completed_pages: int = 0
    worker_diagnostics: tuple[dict[str, Any], ...] = ()


def _document_context_from_texts(texts) -> PdfDocumentContext:
    return PdfDocumentContext(discover_document_units(texts), statement_period_declarations(texts),
                              statement_period_gaps(texts), tuple(texts), completed_pages=len(texts))


def _page_count_worker(path, alternate=False):
    pdf_phase("import", backend="pypdf" if alternate else "pdfium")
    if alternate:
        import pypdf
        pdf_phase("open", backend="pypdf", version=pypdf.__version__)
        with open(path, "rb") as source:
            return len(pypdf.PdfReader(source).pages)
    import pypdfium2 as pdfium
    pdf_phase("open", backend="pdfium", version=str(pdfium.PYPDFIUM_INFO))
    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        document.close()


def _text_window_worker(path, start, stop, alternate=False):
    pdf_phase("import", backend="pypdf" if alternate else "pdfium")
    if alternate:
        import pypdf
        pdf_phase("open", backend="pypdf", version=pypdf.__version__)
        with open(path, "rb") as source:
            reader = pypdf.PdfReader(source)
            pdf_phase("extract")
            return tuple(str(reader.pages[index].extract_text() or "") for index in range(start, stop))
    import pypdfium2 as pdfium
    pdf_phase("open", backend="pdfium", version=str(pdfium.PYPDFIUM_INFO))
    texts = []
    document = pdfium.PdfDocument(path)
    try:
        pdf_phase("extract")
        for index in range(start, stop):
            page = document[index]
            try:
                text = page.get_textpage()
                try:
                    texts.append(str(text.get_text_range() or ""))
                finally:
                    text.close()
            finally:
                page.close()
    finally:
        document.close()
    return tuple(texts)


def _load_text_window(directory, identity, start, stop):
    try:
        data = json.loads((directory / f"{identity}-{start}-{stop}.json").read_text(encoding="utf-8"))
        texts = data["texts"]
        if (data.get("identity") != identity or data.get("start") != start or data.get("stop") != stop
                or not isinstance(texts, list) or len(texts) != stop - start
                or any(not isinstance(text, str) for text in texts)
                or hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest() != data.get("sha256")):
            return None
        return tuple(texts)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_text_window(directory, identity, start, stop, texts):
    directory.mkdir(parents=True, exist_ok=True)
    data = {"identity": identity, "start": start, "stop": stop, "texts": list(texts),
        "sha256": hashlib.sha256(json.dumps(list(texts), ensure_ascii=False).encode()).hexdigest()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / f"{identity}-{start}-{stop}.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _checkpointed_context(path, directory, source_hash, *, cancel_check, deadline, worker_timeout, progress=None):
    """Persist complete text windows before observing the next time slice.

    Source bytes are hashed by the ingestion caller before admission. No
    partial text pass is mistaken for a full-document unit declaration scan.
    """
    identity = hashlib.sha256((source_hash + ':native-text-windows-v1').encode()).hexdigest()
    diagnostics = []
    def invoke(worker, *args, operation, alternate=False, page_range=None):
        return _run_pdfium_isolated(worker, *args, timeout_seconds=min(worker_timeout, max(.05, deadline - time.monotonic())),
            cancel_check=cancel_check, diagnostic_callback=diagnostics.append, operation=operation,
            backend="pypdf" if alternate else "pdfium", page_range=page_range)
    count, error = invoke(_page_count_worker, path, operation="page_count")
    alternate = False
    if error and error not in {"PDFIUM_WORKER_CANCELLED", "PDFIUM_WORKER_START_FAILED"}:
        count, error = invoke(_page_count_worker, path, True, operation="page_count", alternate=True)
        alternate = not error
    if error or type(count) is not int or count <= 0:
        return PdfDocumentContext(diagnostic_code=_context_error(error), worker_diagnostics=tuple(diagnostics))
    texts = []
    for start in range(0, count, 16):
        stop = min(count, start + 16)
        window = _load_text_window(directory, identity, start, stop)
        if window is None:
            if cancel_check and cancel_check():
                return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_CANCELLED", completed_pages=len(texts), worker_diagnostics=tuple(diagnostics))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PdfDocumentContext(diagnostic_code="document_context_slice_exhausted_resumable", completed_pages=len(texts), worker_diagnostics=tuple(diagnostics))
            window, error = invoke(_text_window_worker, path, start, stop, alternate,
                operation="text_window", alternate=alternate, page_range=[start + 1, stop])
            if error and not alternate and error not in {"PDFIUM_WORKER_CANCELLED", "PDFIUM_WORKER_START_FAILED"} and deadline > time.monotonic():
                # A different existing parser is a legitimate changed recovery
                # path, not a repeat of an unknown model submission.
                window, error = invoke(_text_window_worker, path, start, stop, True,
                    operation="text_window", alternate=True, page_range=[start + 1, stop])
            if error or not isinstance(window, (list, tuple)) or len(window) != stop - start:
                code = ("DOCUMENT_CONTEXT_CANCELLED" if error == "PDFIUM_WORKER_CANCELLED" else
                    "document_context_slice_exhausted_resumable" if error == "PDFIUM_WORKER_TIMEOUT" and time.monotonic() >= deadline else
                    _context_error(error))
                return PdfDocumentContext(diagnostic_code=code, completed_pages=len(texts), worker_diagnostics=tuple(diagnostics))
            _save_text_window(directory, identity, start, stop, window)
        texts.extend(window)
        if progress:
            progress(start // 16 + 1, (count + 15) // 16, 'text-checkpointed')
    remember_page_texts(path, texts)
    from dataclasses import replace
    result = _document_context_from_texts(texts)
    return replace(result, worker_diagnostics=tuple(diagnostics),
        diagnostic_code="DOCUMENT_CONTEXT_OCR_REQUIRED" if not any(text.strip() for text in texts) else "")


def _document_context_worker(path: str, alternate=False) -> PdfDocumentContext:
    count = _page_count_worker(path, alternate)
    texts = _text_window_worker(path, 0, count, alternate)
    remember_page_texts(path, texts)
    return _document_context_from_texts(texts)


def _context_error(error):
    return {"PDFIUM_WORKER_TIMEOUT": "DOCUMENT_CONTEXT_TIMEOUT",
            "PDFIUM_WORKER_CANCELLED": "DOCUMENT_CONTEXT_CANCELLED",
            # Legacy code retained for compatibility, raw startup code lives in
            # worker_diagnostics and distinguishes runtime from financial gaps.
            "PDFIUM_WORKER_START_FAILED": "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE"}.get(error, error or "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE")


def read_document_context(path: str, *, cancel_check: Any = None, timeout_seconds: float = 45.0,
                          deadline: float | None = None, checkpoint_dir=None, source_hash=None, progress=None) -> PdfDocumentContext:
    if cancel_check is not None and cancel_check():
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_CANCELLED")
    if deadline is not None:
        timeout_seconds = min(timeout_seconds, max(0.0, deadline - time.monotonic()))
    if timeout_seconds <= 0:
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_TIMEOUT")
    texts = cached_page_texts(path)
    if texts is not None:
        from dataclasses import replace
        return replace(_document_context_from_texts(texts),
            diagnostic_code='DOCUMENT_CONTEXT_OCR_REQUIRED' if not any(text.strip() for text in texts) else '')
    if checkpoint_dir is not None and source_hash and not mp.current_process().daemon:
        context = _checkpointed_context(path, Path(checkpoint_dir), source_hash, cancel_check=cancel_check,
            deadline=deadline if deadline is not None else time.monotonic() + timeout_seconds,
            worker_timeout=min(45., timeout_seconds), progress=progress)
        # Runtime receipt survives a restart and contains no path/source text.
        # It is diagnostic evidence only, never a fact/parse-success cache.
        persist_runtime_receipt(checkpoint_dir, source_hash, raw_error_code=context.diagnostic_code,
            completed_pages=context.completed_pages, workers=list(context.worker_diagnostics))
        return context
    if cancel_check is not None and cancel_check():
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_CANCELLED")
    diagnostics = []
    operation_deadline = time.monotonic() + timeout_seconds
    result, error = _run_pdfium_isolated(
        _document_context_worker, path, timeout_seconds=timeout_seconds, cancel_check=cancel_check,
        diagnostic_callback=diagnostics.append, operation="document_text",
    )
    if error and error not in {"PDFIUM_WORKER_START_FAILED", "PDFIUM_WORKER_CANCELLED", "PDFIUM_WORKER_TIMEOUT"}:
        result, error = _run_pdfium_isolated(_document_context_worker, path, True,
            timeout_seconds=max(.05, operation_deadline - time.monotonic()), cancel_check=cancel_check, diagnostic_callback=diagnostics.append,
            operation="document_text", backend="pypdf")
    if error is None and isinstance(result, PdfDocumentContext):
        # A spawned worker's cache is process-local. Transfer the original text
        # to the caller so qualitative indexing does not reread the whole PDF.
        remember_page_texts(path, result.page_texts)
        from dataclasses import replace
        return replace(result, worker_diagnostics=tuple(diagnostics),
            diagnostic_code="DOCUMENT_CONTEXT_OCR_REQUIRED" if not any(text.strip() for text in result.page_texts) else "")
    return PdfDocumentContext(diagnostic_code=_context_error(error), worker_diagnostics=tuple(diagnostics))


def runtime_receipt_path(directory, source_hash):
    return Path(directory) / ('runtime-' + hashlib.sha256(str(source_hash).encode()).hexdigest() + '.json')


def persist_runtime_receipt(directory, source_hash, **receipt):
    from ..build_identity import load_build_info
    directory, temporary = Path(directory), None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({'source_hash': source_hash, 'build': load_build_info(), **receipt}, handle, ensure_ascii=False)
        os.replace(temporary, runtime_receipt_path(directory, source_hash))
        temporary = None
    except OSError:
        pass  # Optional diagnostic I/O cannot discard validated windows.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def read_document_pages(path: str, *, cancel_check: Any = None, deadline: float | None = None) -> tuple[str, ...]:
    """Share the killable prepass; unavailable text is not an empty disclosure.

    The caller supplies one absolute index budget. We do not start an unbounded
    pdfplumber fallback after a timeout, nor turn a cancelled task into a cache
    of an apparently successful, source-less company report.
    """
    if cancel_check is not None and cancel_check():
        raise CancelledError()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("DISCLOSURE_INDEX_BUDGET_EXHAUSTED")
    context = read_document_context(path, cancel_check=cancel_check, deadline=deadline)
    if cancel_check is not None and cancel_check():
        raise CancelledError()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("DISCLOSURE_INDEX_BUDGET_EXHAUSTED")
    if context.diagnostic_code == "DOCUMENT_CONTEXT_CANCELLED":
        raise CancelledError()
    if context.diagnostic_code == "DOCUMENT_CONTEXT_TIMEOUT":
        raise TimeoutError(context.diagnostic_code)
    if context.diagnostic_code:
        raise ValueError(context.diagnostic_code)
    if not context.page_texts:
        raise ValueError("DISCLOSURE_NATIVE_TEXT_UNAVAILABLE")
    return context.page_texts


def read_document_units(path: str, *, cancel_check: Any = None) -> DocumentUnitResolution:
    """Compatibility projection over the shared bounded document prepass."""
    return read_document_context(path, cancel_check=cancel_check).units
