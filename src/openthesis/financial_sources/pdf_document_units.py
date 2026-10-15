"""Bounded native text prepass for explicit statement-wide unit evidence."""
from __future__ import annotations

import multiprocessing as mp
import time
from concurrent.futures import CancelledError
from dataclasses import dataclass
from typing import Any

from ..disclosure_index import cached_page_texts, remember_page_texts
from ..vision_financials import _run_pdfium_isolated
from .document_units import DocumentUnitResolution, discover_document_units
from .statement_periods import StatementPeriodDeclaration, StatementPeriodGap, statement_period_declarations, statement_period_gaps


@dataclass(frozen=True, slots=True)
class PdfDocumentContext:
    units: DocumentUnitResolution = DocumentUnitResolution()
    periods: tuple[StatementPeriodDeclaration, ...] = ()
    period_gaps: tuple[StatementPeriodGap, ...] = ()
    page_texts: tuple[str, ...] = ()
    diagnostic_code: str = ""


def _document_context_from_texts(texts) -> PdfDocumentContext:
    return PdfDocumentContext(discover_document_units(texts), statement_period_declarations(texts),
                              statement_period_gaps(texts), tuple(texts))


def _document_context_worker(path: str) -> PdfDocumentContext:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    texts = []
    try:
        for number in range(len(document)):
            page = document[number]
            try:
                text_page = page.get_textpage()
                try:
                    texts.append(str(text_page.get_text_range() or ""))
                finally:
                    text_page.close()
            finally:
                page.close()
    finally:
        document.close()
    remember_page_texts(path, texts)
    return _document_context_from_texts(texts)


def read_document_context(path: str, *, cancel_check: Any = None, timeout_seconds: float = 45.0,
                          deadline: float | None = None) -> PdfDocumentContext:
    if cancel_check is not None and cancel_check():
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_CANCELLED")
    if deadline is not None:
        timeout_seconds = min(timeout_seconds, max(0.0, deadline - time.monotonic()))
    if timeout_seconds <= 0:
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_TIMEOUT")
    texts = cached_page_texts(path)
    if texts is not None:
        return _document_context_from_texts(texts)
    if cancel_check is not None and cancel_check():
        return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_CANCELLED")
    if mp.current_process().daemon:
        # The existing per-filing process is already killable; never nested spawn.
        try:
            return _document_context_worker(path)
        except Exception:
            return PdfDocumentContext(diagnostic_code="DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE")
    result, error = _run_pdfium_isolated(
        _document_context_worker, path, timeout_seconds=timeout_seconds, cancel_check=cancel_check,
    )
    if error is None and isinstance(result, PdfDocumentContext):
        # A spawned worker's cache is process-local. Transfer the original text
        # to the caller so qualitative indexing does not reread the whole PDF.
        remember_page_texts(path, result.page_texts)
        return result
    code = {"PDFIUM_WORKER_TIMEOUT": "DOCUMENT_CONTEXT_TIMEOUT",
            "PDFIUM_WORKER_CANCELLED": "DOCUMENT_CONTEXT_CANCELLED"}.get(
                error, "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE")
    return PdfDocumentContext(diagnostic_code=code)


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
